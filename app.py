"""
Storco all-in-one READ-ONLY data backend for the Power BI / Power Apps Claude bot.

Talks DIRECTLY to the underlying REST APIs (Primavera Cloud, Aconex Cost/Field/Mail)
using Storco SERVICE credentials held server-side. No MCP servers, no per-user login,
no enrolment sessions. Every tool is a GET. (Primavera performs one internal POST to
mint its bearer token during the auth handshake - that is not a user-facing write.)

ADDING A READ TOOL = append one entry to TOOLS (see the pattern block at the bottom).

Env vars (Render dashboard only, never in .env):
    ANTHROPIC_API_KEY
    CONNECTOR_API_KEY        - shared secret the Power Apps connector sends

    # Primavera (Basic -> primediscovery handshake -> bearer token)
    PRIMAVERA_BASE_URL       - e.g. https://primavera-au1.oraclecloud.com
    PRIMAVERA_USERNAME
    PRIMAVERA_PASSWORD
    PRIMAVERA_TOKEN_PATH     - optional, default /primediscovery/apitoken/request
    PRIMAVERA_API_SCOPE      - optional, default http://primavera-au1.oraclecloud.com/api

    # Aconex (host shared by Field/Mail/Cost REST; auth differs per module)
    ACONEX_BASE_URL          - e.g. https://au1.aconex.com
    ACONEX_COST_TOKEN        - static X-User-Access-Token (service)
    ACONEX_FIELD_BASIC       - "user:pass" for Field
    ACONEX_MAIL_BASIC        - "user:pass" for Mail
"""

import os
import time
import base64
import anthropic
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Callable

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CONNECTOR_API_KEY = os.environ["CONNECTOR_API_KEY"]

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 6

app = FastAPI(title="Storco read-only data backend")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["POST", "OPTIONS"], allow_headers=["*"],
)
aclient = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# AUTH - one resolver per module. Returns the headers for an upstream request.
# ---------------------------------------------------------------------------

def _basic_header(creds: Optional[str]) -> dict:
    if not creds:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


# Ported from the Primavera MCP server (index.ts mintToken). The scope is passed
# as a RAW query-string param (its colons/slashes are intentionally not encoded),
# the POST has Basic auth and no body, and the token may come back as raw text, a
# quoted string, or JSON under any of several field names.
PV_TOKEN_PATH = os.environ.get("PRIMAVERA_TOKEN_PATH", "/primediscovery/apitoken/request")
PV_API_SCOPE = os.environ.get("PRIMAVERA_API_SCOPE", "http://primavera-au1.oraclecloud.com/api")

_pv_token = {"value": None, "exp": 0.0}

async def _primavera_headers(client: httpx.AsyncClient) -> dict:
    """Basic-auth handshake -> bearer token, cached for ~50 min."""
    if _pv_token["value"] and time.time() < _pv_token["exp"]:
        return {"Authorization": f"Bearer {_pv_token['value']}"}

    basic = base64.b64encode(
        f"{os.environ['PRIMAVERA_USERNAME']}:{os.environ['PRIMAVERA_PASSWORD']}".encode()
    ).decode()
    # Primavera requires the scope's colons/slashes UN-encoded. httpx re-encodes a
    # query string by default, so set the raw query bytes explicitly to bypass that.
    base = os.environ["PRIMAVERA_BASE_URL"].rstrip("/")
    url = httpx.URL(base + PV_TOKEN_PATH).copy_with(query=f"scope={PV_API_SCOPE}".encode())
    # Oracle requires the OAuth client-credentials grant in a form body. Omitting it
    # returns 405 / PRM-001003010 ("missing required parameters").
    r = await client.post(
        url,
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
    )
    if r.status_code in (401, 403):
        raise RuntimeError(f"Primavera auth failed ({r.status_code}): bad service credentials")
    r.raise_for_status()

    text = r.text.strip()
    token = text
    if text.startswith("{"):
        try:
            j = r.json()
            token = (j.get("token") or j.get("access_token") or j.get("apitoken")
                     or j.get("apiToken") or j.get("bearerToken") or j.get("value") or text)
        except Exception:
            token = text
    else:
        token = text.strip('"')
    if not token:
        raise RuntimeError("Primavera token request returned an empty token")

    _pv_token["value"] = token
    _pv_token["exp"] = time.time() + 3000   # ~50 min; re-mint silently after
    return {"Authorization": f"Bearer {token}"}


# base = URL prefix for the module; auth = async fn(client) -> headers dict
MODULES = {
    "primavera": {
        "base": os.environ.get("PRIMAVERA_BASE_URL", ""),
        "auth": _primavera_headers,
    },
    "aconex_cost": {
        "base": os.environ.get("ACONEX_BASE_URL", ""),
        "auth": lambda c: _async_const({"X-User-Access-Token": os.environ.get("ACONEX_COST_TOKEN", "")}),
    },
    "aconex_field": {
        "base": os.environ.get("ACONEX_BASE_URL", ""),
        "auth": lambda c: _async_const(_basic_header(os.environ.get("ACONEX_FIELD_BASIC"))),
    },
    "aconex_mail": {
        "base": os.environ.get("ACONEX_BASE_URL", ""),
        "auth": lambda c: _async_const(_basic_header(os.environ.get("ACONEX_MAIL_BASIC"))),
    },
}

async def _async_const(value):
    return value


# ---------------------------------------------------------------------------
# TOOL REGISTRY - each entry is one read endpoint exposed to Claude.
#   name         : unique, [a-zA-Z0-9_-], <=64 chars
#   module       : key into MODULES (decides base URL + auth)
#   path         : appended to base; may contain {placeholders} filled from args
#   query        : optional fn(args) -> dict of query-string params
#   input_schema : JSON schema for the args Claude must supply
# Paths/params below are best-effort starting points - CONFIRM each against your
# own API knowledge (you built the MCP servers, so you have the real shapes).
# ---------------------------------------------------------------------------

ACONEX_ORG_ID = "1476470689"   # from your Aconex Cost playbook
# Real Cost prefix (from your server: `${ACONEX_HOST}/cost/api/organizations/${ORG}${path}`)
COST = f"/cost/api/organizations/{ACONEX_ORG_ID}"

TOOLS = [
    {
        "name": "aconex_cost_list_projects",
        "module": "aconex_cost",
        "path": COST + "/projects",
        "query": lambda a: {},
        "description": "List Aconex Cost projects for the Storco organisation.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "aconex_cost_get_project",
        "module": "aconex_cost",
        "path": COST + "/projects/{projectId}",
        "query": lambda a: {},
        "description": "Get one Aconex Cost project by its numeric id.",
        "input_schema": {
            "type": "object",
            "properties": {"projectId": {"type": "string", "description": "Cost project id"}},
            "required": ["projectId"],
        },
    },
    {
        "name": "aconex_field_list_projects",
        "module": "aconex_field",
        "path": "/field-management/api/projects",
        "query": lambda a: {},
        "description": "List Aconex Field projects.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "aconex_field_list_areas",
        "module": "aconex_field",
        "path": "/field-management/api/projects/{project_id}/areas",
        "query": lambda a: {},
        "description": "List the areas (locations) for an Aconex Field project. Areas are needed to reach issues.",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "aconex_field_list_issues",
        "module": "aconex_field",
        "path": "/field-management/api/projects/{project_id}/areas/{area_id}/issues",
        "query": lambda a: {},
        "description": "List Aconex Field issues (defects/punch items) within a given area of a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "area_id": {"type": "string", "description": "area id from aconex_field_list_areas"},
            },
            "required": ["project_id", "area_id"],
        },
    },
    {
        "name": "aconex_mail_list",
        "module": "aconex_mail",
        "path": "/api/projects/{project_id}/mail",
        "query": lambda a: {k: v for k, v in {
            "mail_box": a.get("mail_box", "inbox"),
            "page_size": a.get("page_size", 25),
            "search_query": a.get("search_query"),
        }.items() if v is not None},
        "accept": "application/xml",   # Aconex Mail responds in XML, not JSON
        "description": "List Aconex Mail items in a project mailbox (inbox or sentbox). Returns XML.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "mail_box": {"type": "string", "description": "inbox or sentbox (default inbox)"},
                "page_size": {"type": "integer", "description": "max rows, default 25"},
                "search_query": {"type": "string", "description": "optional Aconex search expression"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "primavera_list_projects",
        "module": "primavera",
        "path": "/api/restapi/project",                  # CONFIRM path
        "query": lambda a: {},
        "description": "List Primavera Cloud projects visible to the service account.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def anthropic_tools():
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in TOOLS
    ]


async def run_tool(name: str, args: dict, client: httpx.AsyncClient) -> str:
    tool = TOOLS_BY_NAME.get(name)
    if not tool:
        return f"Unknown tool {name}"
    mod = MODULES[tool["module"]]
    if not mod["base"]:
        return f"Module {tool['module']} is not configured (missing base URL/credentials)."
    headers = await mod["auth"](client)
    headers["Accept"] = tool.get("accept", "application/json")
    path = tool["path"].format(**args) if "{" in tool["path"] else tool["path"]
    url = mod["base"].rstrip("/") + path
    params = tool.get("query", lambda a: {})(args)
    r = await client.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.text[:20000]


# ---------------------------------------------------------------------------
# CHAT - tool-use loop. Identical contract to the connector: messages in, reply out.
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    report_context: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str


SYSTEM_BASE = (
    "You are Storco's project analytics assistant, embedded in a Power BI report for "
    "project and construction managers. You can read live Storco data through the "
    "connected tools (Primavera schedules, Aconex cost/field/mail). Use them when a "
    "question needs current data; otherwise answer directly. This is a strictly "
    "read-only tool. Reply in plain prose, no Markdown, no asterisks or hash headers. "
    "Be concise."
)


@app.get("/health")
async def health():
    configured = {m: bool(cfg["base"]) for m, cfg in MODULES.items()}
    return {"status": "ok", "model": MODEL, "tool_count": len(TOOLS), "modules_configured": configured}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, x_api_key: str = Header(default="")):
    if x_api_key != CONNECTOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    system = SYSTEM_BASE
    if req.report_context:
        system += f"\n\nCurrent report context:\n{req.report_context}"

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    tools = anthropic_tools()

    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await aclient.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=system, tools=tools, messages=messages,
            )
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text")
                return ChatResponse(reply=text or "(no text response)")

            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    try:
                        out = await run_tool(b.name, b.input or {}, client)
                    except httpx.HTTPStatusError as e:
                        out = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
                    except Exception as e:
                        out = f"Tool error: {e}"
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})

    return ChatResponse(reply="(stopped after maximum tool rounds)")


# ---------------------------------------------------------------------------
# PATTERN: to add a read tool, append a dict to TOOLS above. Example -
#
#   {
#       "name": "aconex_cost_list_contracts",
#       "module": "aconex_cost",
#       "path": "/cost/api/v1/contracts",
#       # organizationRole is mandatory or the endpoint 500s (per your playbook):
#       "query": lambda a: {"organizationId": ACONEX_ORG_ID,
#                            "projectId": a["projectId"],
#                            "organizationRole": a.get("role", "UPSTREAM")},
#       "description": "List contracts for an Aconex Cost project.",
#       "input_schema": {
#           "type": "object",
#           "properties": {
#               "projectId": {"type": "string"},
#               "role": {"type": "string", "description": "UPSTREAM or DOWNSTREAM"},
#           },
#           "required": ["projectId"],
#       },
#   },
#
# That's it - no other code changes. Keep everything GET. Bake mandatory params
# (like organizationRole) into the query fn so Claude can't omit them.
# ---------------------------------------------------------------------------
