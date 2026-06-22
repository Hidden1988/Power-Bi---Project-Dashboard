"""
Storco Power BI -> Power Apps -> Claude chat backend.
Auth-broker / MCP-client version.

WHY THIS SHAPE:
The Anthropic Messages API, when given an `mcp_servers` block, connects to those
servers DIRECTLY and can only send `Authorization: Bearer`. Storco's MCP servers
each want a different caller header (X-User-Access-Token, Basic, or none), so the
API path can't authenticate to them. Instead this backend acts as the MCP *client*:
it connects to each server with the correct header, lists their tools, and runs the
tool-use loop itself. The backend is now in the path, so it controls auth.

Service-identity model: one set of Storco read-only credentials, held server-side,
used for every report viewer. View-only.

Env vars (set in Render dashboard, never in .env):
    ANTHROPIC_API_KEY     - Anthropic key
    CONNECTOR_API_KEY     - shared secret the Power Apps connector sends
    ENABLED_SERVERS       - comma list, e.g. "aconex_cost" (start with one)
    ACONEX_COST_TOKEN     - value for the X-User-Access-Token header
    ACONEX_MAIL_BASIC     - "user:pass" for Aconex Mail (encoded to Basic)
    ACONEX_FIELD_BASIC    - "user:pass" for Aconex Field
    PRIMAVERA_BASIC       - "user:pass" for Primavera handshake (optional)
"""

import os
import re
import json
import base64
import anthropic
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CONNECTOR_API_KEY = os.environ["CONNECTOR_API_KEY"]
ENABLED = [s.strip() for s in os.environ.get("ENABLED_SERVERS", "aconex_cost").split(",") if s.strip()]

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 6   # safety cap on the agentic loop


def _basic(creds: Optional[str]) -> dict:
    """Turn 'user:pass' into an Authorization: Basic header."""
    if not creds:
        return {}
    token = base64.b64encode(creds.encode()).decode()
    return {"Authorization": f"Basic {token}"}


# Per-server config: URL + the header THIS server expects from its caller.
SERVER_CONFIG = {
    "primavera": {
        "url": "https://primavera-mcp.onrender.com/mcp",
        "headers": _basic(os.environ.get("PRIMAVERA_BASIC")),  # often {} - self-auths
    },
    "aconex_cost": {
        "url": "https://storcoaconex.reliablehosting.au/mcp",
        "headers": ({"X-User-Access-Token": os.environ["ACONEX_COST_TOKEN"]}
                    if os.environ.get("ACONEX_COST_TOKEN") else {}),
    },
    "aconex_mail": {
        "url": "https://aconex-mail.onrender.com/mcp",
        "headers": _basic(os.environ.get("ACONEX_MAIL_BASIC")),
    },
    "aconex_field": {
        "url": "https://aconex-field.onrender.com/mcp",
        "headers": _basic(os.environ.get("ACONEX_FIELD_BASIC")),
    },
}

# Short prefixes keep Anthropic tool names within the 64-char / charset limit.
PREFIX = {"primavera": "pv", "aconex_cost": "ac", "aconex_mail": "am", "aconex_field": "af"}

app = FastAPI(title="Storco Power BI Claude Bot (MCP broker)")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["POST", "OPTIONS"], allow_headers=["*"],
)
aclient = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def _mcp_client(server: str) -> Client:
    cfg = SERVER_CONFIG[server]
    return Client(StreamableHttpTransport(cfg["url"], headers=cfg["headers"]))


def _safe_name(server: str, tool: str) -> str:
    name = f"{PREFIX[server]}_{tool}"
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name[:64]


async def discover_tools():
    """Connect to each enabled server, list tools, build the Anthropic tools array,
    a routing map from anthropic-name -> (server, real_tool_name), and a dict of
    any servers that failed to connect (skipped, not fatal)."""
    tools, routing, skipped = [], {}, {}
    for server in ENABLED:
        if server not in SERVER_CONFIG:
            continue
        try:
            async with _mcp_client(server) as client:
                server_tools = await client.list_tools()
        except Exception as e:
            skipped[server] = str(e)   # one bad server shouldn't kill the rest
            continue
        for t in server_tools:
            aname = _safe_name(server, t.name)
            routing[aname] = (server, t.name)
            tools.append({
                "name": aname,
                "description": (t.description or "")[:1000],
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            })
    return tools, routing, skipped


def _result_text(result) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        txt = getattr(block, "text", None)
        if txt:
            parts.append(txt)
    if parts:
        return "\n".join(parts)
    sc = getattr(result, "structured_content", None) or getattr(result, "data", None)
    return json.dumps(sc, default=str) if sc is not None else "(empty result)"


async def call_tool(server: str, tool: str, args: dict) -> str:
    async with _mcp_client(server) as client:
        result = await client.call_tool(tool, args or {})
    return _result_text(result)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    report_context: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str


SYSTEM_BASE = (
    "You are Storco's project analytics assistant, embedded inside a Power BI "
    "report used by project and construction managers. You can query live Storco "
    "data through the connected tools. Use them when a question needs current "
    "data; otherwise answer directly. This is a read-only tool - never attempt to "
    "create, update, or delete anything. Reply in plain prose, no Markdown, no "
    "asterisks or hash headers. Be concise."
)


@app.get("/health")
async def health():
    try:
        tools, _, skipped = await discover_tools()
        status = "ok" if tools else "degraded"
        return {"status": status, "model": MODEL, "enabled": ENABLED,
                "tool_count": len(tools), "skipped": skipped}
    except Exception as e:
        return {"status": "degraded", "enabled": ENABLED, "error": str(e)}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, x_api_key: str = Header(default="")):
    if x_api_key != CONNECTOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    tools, routing, _ = await discover_tools()

    system = SYSTEM_BASE
    if req.report_context:
        system += f"\n\nCurrent report context:\n{req.report_context}"

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    for _ in range(MAX_TOOL_ROUNDS):
        resp = await aclient.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system,
            tools=tools, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return ChatResponse(reply=text or "(no text response)")

        # Execute every tool the model asked for, feed results back, loop.
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        tool_results = []
        for b in resp.content:
            if b.type == "tool_use":
                server, real = routing.get(b.name, (None, None))
                if server is None:
                    out = f"Unknown tool {b.name}"
                else:
                    try:
                        out = await call_tool(server, real, b.input)
                    except Exception as e:
                        out = f"Tool error: {e}"
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000]})
        messages.append({"role": "user", "content": tool_results})

    return ChatResponse(reply="(stopped after maximum tool rounds)")
