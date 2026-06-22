"""
Storco Power BI -> Power Apps -> Claude chat backend (MCP-grounded version).

Adds live querying of Storco systems (Primavera, Aconex) via remote MCP servers.
Service-identity model: the MCP servers authenticate with Storco's own
read-only credentials, so every report viewer's question runs through the same
read-only access. No per-user identity, no write paths.

Env vars (set in Render dashboard, never in .env):
    ANTHROPIC_API_KEY   - your Anthropic key
    CONNECTOR_API_KEY   - shared secret the Power Apps connector sends
    MCP_PRIMAVERA_TOKEN - bearer token your Primavera MCP server expects (optional)
    MCP_ACONEX_TOKEN    - bearer token your Aconex MCP server expects (optional)
"""

import os
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CONNECTOR_API_KEY = os.environ["CONNECTOR_API_KEY"]
MCP_PRIMAVERA_TOKEN = os.environ.get("MCP_PRIMAVERA_TOKEN")
MCP_ACONEX_TOKEN = os.environ.get("MCP_ACONEX_TOKEN")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048   # higher: tool-use answers are longer

app = FastAPI(title="Storco Power BI Claude Bot (MCP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


def build_mcp_servers():
    """Declare the read-only Storco MCP servers Claude may call.

    Each server is reached directly by the Anthropic API over HTTPS, so it must
    be publicly reachable and authenticated by a static/refreshable header
    token (no interactive OAuth). authorization_token is optional - omit it for
    servers that don't require auth.
    """
    servers = []

    primavera = {
        "type": "url",
        "name": "primavera",
        "url": "https://primavera-mcp.onrender.com/mcp",
    }
    if MCP_PRIMAVERA_TOKEN:
        primavera["authorization_token"] = MCP_PRIMAVERA_TOKEN
    servers.append(primavera)

    aconex = {
        "type": "url",
        "name": "aconex_cost",
        "url": "https://storcoaconex.reliablehosting.au/mcp",
    }
    if MCP_ACONEX_TOKEN:
        aconex["authorization_token"] = MCP_ACONEX_TOKEN
    servers.append(aconex)

    return servers


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
    "report used by project and construction managers. You can query live "
    "Storco data through the connected tools (Primavera for schedules, Aconex "
    "for cost and contracts). Use them when a question needs current data; "
    "otherwise answer directly. This is a read-only tool - never attempt to "
    "create, update, or delete anything. Reply in plain prose, no Markdown "
    "formatting, no asterisks or hash headers. Be concise. If report context "
    "is supplied, prefer it for questions about what's on screen, and say so "
    "if neither the context nor the tools contain the answer."
)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "mcp_servers": [s["name"] for s in build_mcp_servers()]}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, x_api_key: str = Header(default="")):
    if x_api_key != CONNECTOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    system = SYSTEM_BASE
    if req.report_context:
        system += f"\n\nCurrent report context:\n{req.report_context}"

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [m.model_dump() for m in req.messages],
        "mcp_servers": build_mcp_servers(),
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "mcp-client-2025-04-04",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as client:   # tool turns are slower
        r = await client.post(ANTHROPIC_URL, json=payload, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic error: {r.text}")

    data = r.json()
    # The API resolves MCP tool calls server-side and returns the final answer.
    # Collect every text block (there can be more than one across tool turns).
    reply = "".join(
        block["text"] for block in data.get("content", [])
        if block.get("type") == "text"
    )
    return ChatResponse(reply=reply or "(no text response)")
