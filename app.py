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
CONNECTOR_API_KEY = os.environ.get("CONNECTOR_API_KEY", "")  # empty/unset = auth disabled

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
MAX_TOOL_ROUNDS = 12          # higher: pagination needs several tool round-trips
MAX_RESULT_CHARS = 60000     # per tool result returned to the model

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
        raise RuntimeError(f"Primavera token handshake failed ({r.status_code}): bad service credentials")
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"Primavera token handshake failed ({r.status_code}): {r.text[:300]}")

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
        "query": lambda a: {k: v for k, v in {
            "page_size": a.get("page_size", 200),
            "page_number": a.get("page_number", 1),
        }.items() if v is not None},
        "description": ("List Aconex Field issues (defects/punch items) within an area of a project. "
                        "Returns one page; call again with the next page_number for more."),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "area_id": {"type": "string", "description": "area id from aconex_field_list_areas"},
                "page_size": {"type": "integer", "description": "rows per page, default 200"},
                "page_number": {"type": "integer", "description": "1-based page index, default 1"},
            },
            "required": ["project_id", "area_id"],
        },
    },
    {
        "name": "aconex_mail_list_projects",
        "module": "aconex_mail",
        "path": "/api/projects",
        "query": lambda a: {},
        "accept": "application/xml",
        "description": "List Aconex projects available for Mail (use these ids for aconex_mail_list). Returns XML.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "aconex_mail_list",
        "module": "aconex_mail",
        "path": "/api/projects/{project_id}/mail",
        "query": lambda a: {k: v for k, v in {
            "mail_box": a.get("mail_box", "inbox"),
            "page_size": a.get("page_size", 250),
            "page_number": a.get("page_number", 1),
            "search_query": a.get("search_query"),
        }.items() if v is not None},
        "accept": "application/xml",   # Aconex Mail responds in XML, not JSON
        "description": ("List Aconex Mail items in a project mailbox (inbox or sentbox). Returns XML. "
                        "Returns one page; to see more, call again with the next page_number."),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "mail_box": {"type": "string", "description": "inbox or sentbox (default inbox)"},
                "page_size": {"type": "integer", "description": "rows per page, default 250"},
                "page_number": {"type": "integer", "description": "1-based page index, default 1"},
                "search_query": {"type": "string", "description": "optional Aconex search expression"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "primavera_test_connection",
        "module": "primavera",
        "path": "/api/restapi/util/testConnection",
        "query": lambda a: {},
        "description": "Verify the Primavera token/handshake works. Returns OK if authenticated.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "primavera_list_workspaces",
        "module": "primavera",
        "path": "/api/restapi/workspace",
        "query": lambda a: {},
        "description": "List Primavera Cloud workspaces. Workspace ids are needed to list projects.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "primavera_list_projects_by_workspace",
        "module": "primavera",
        "path": "/api/restapi/project/workspace/{workspaceId}",
        "query": lambda a: {},
        "description": "List Primavera Cloud projects within a workspace (id from primavera_list_workspaces).",
        "input_schema": {
            "type": "object",
            "properties": {"workspaceId": {"type": "string"}},
            "required": ["workspaceId"],
        },
    },
]

# --- Generic GET passthrough tools: full read coverage without 400+ definitions ---
# Each takes a relative `path` (+ optional `query`) and GETs it with the module's
# auth. The descriptions catalogue the available resource families so Claude can
# build correct paths. Curated tools above cover the common cases; these cover the
# long tail. READ-ONLY: only GET is performed regardless of path.
TOOLS += [
    {
        "name": "aconex_cost_get",
        "module": "aconex_cost",
        "dynamic": True,
        "prefix": COST,   # /cost/api/organizations/{org}
        "description": (
            "GET any Aconex Cost endpoint (read-only). `path` is relative to the "
            "org root /cost/api/organizations/{org}. Resource families: /projects, "
            "/projects/{projectId}, and under a project: contracts, control-accounts, "
            "control-elements, change-events, change-event-items, change-orders, "
            "contract-change-orders, pay-items, payment-applications, "
            "payment-application-items, period-actuals, variance-analyses, wbs, "
            "activities, reference-documents, mails, time-phased-data, settings. "
            "Org-level: /calendars, /currencies, /currency-exchange-rates, /eps, "
            "/obs, /users, /security-profiles, /wbs-templates, /tag-categories, "
            "/distribution-curves, /reporting-periods. Many list endpoints accept "
            "page/limit query params. Example path: /projects/717861/contracts"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the org root, e.g. /projects/717861/contracts"},
                "query": {"type": "object", "description": "Optional query params, e.g. {\"organizationRole\":\"UPSTREAM\"}"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "primavera_get",
        "module": "primavera",
        "dynamic": True,
        "prefix": "",     # model supplies full /api/restapi/... path
        "description": (
            "GET any Primavera Cloud REST endpoint (read-only). `path` is the full "
            "REST path beginning /api/restapi/. Families: project (and "
            "project/workspace/{workspaceId}), workspace, program, activity "
            "(activity/project/{projectId}), wbs (wbs/project/{projectId}), resource, "
            "assignment, relationship, calendar, resourceDemand, "
            "resourceRoleAssignment, baselineCategory, configuredField, cbs, "
            "portfolioProject. Example: /api/restapi/activity/project/12345"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path, e.g. /api/restapi/project/workspace/123"},
                "query": {"type": "object", "description": "Optional query params"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "aconex_field_get",
        "module": "aconex_field",
        "dynamic": True,
        "prefix": "/field-management/api",
        "description": (
            "GET any Aconex Field endpoint (read-only). `path` is relative to "
            "/field-management/api. Families: /projects, /projects/{projectId}/areas, "
            "/projects/{projectId}/areas/{areaId}/issues, and other Field resources. "
            "Example: /projects/1879048409/areas"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "object", "description": "Optional query params (e.g. page_size, page_number)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "aconex_mail_get",
        "module": "aconex_mail",
        "dynamic": True,
        "prefix": "",     # model supplies /api/...
        "accept": "application/xml",
        "description": (
            "GET any Aconex Mail/Connect endpoint (read-only, returns XML). `path` "
            "begins /api/. Examples: /api/projects (list projects), "
            "/api/projects/{projectId}/mail (list mail, supports page_number & "
            "page_size), /api/projects/{projectId}/mail/{mailId} (one mail item)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "object", "description": "Optional query params"},
            },
            "required": ["path"],
        },
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
    if tool.get("dynamic"):
        # Generic passthrough: path + optional query come from the model's args.
        rel = (args.get("path") or "").strip()
        if rel and not rel.startswith("/"):
            rel = "/" + rel
        path = tool.get("prefix", "") + rel
        params = args.get("query") or {}
    else:
        path = tool["path"].format(**args) if "{" in tool["path"] else tool["path"]
        params = tool.get("query", lambda a: {})(args)
    url = mod["base"].rstrip("/") + path
    r = await client.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.text[:MAX_RESULT_CHARS]


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
    if CONNECTOR_API_KEY and x_api_key != CONNECTOR_API_KEY:
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
