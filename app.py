"""
Storco Power BI -> Power Apps -> Claude chat backend.

A single POST /chat endpoint that the Power Platform custom connector calls.
- The Anthropic API key lives ONLY here (server-side env var).
- Power Apps authenticates with a separate shared secret (CONNECTOR_API_KEY)
  passed in the x-api-key header.
- The Claude Messages API is stateless: the canvas app sends the full
  conversation history every call, plus optional report filter context.

Run locally:
    pip install fastapi uvicorn httpx
    uvicorn app:app --host 0.0.0.0 --port 8000

Deploy on Render / reliablehosting.au with these env vars set in the dashboard:
    ANTHROPIC_API_KEY   - your real Anthropic key (never exposed to Power Apps)
    CONNECTOR_API_KEY   - a long random string; also pasted into the connector
"""

import os
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CONNECTOR_API_KEY = os.environ["CONNECTOR_API_KEY"]
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"   # swap to "claude-opus-4-8" for heavier reasoning
MAX_TOKENS = 1024

app = FastAPI(title="Storco Power BI Claude Bot")

# Power Apps / Power BI render from Microsoft-owned origins. Keep this open
# while testing, then tighten allow_origins to the specific hosts in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str        # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    report_context: Optional[str] = None   # current filters/selection from the report


class ChatResponse(BaseModel):
    reply: str


SYSTEM_BASE = (
    "You are Storco's project analytics assistant, embedded inside a Power BI "
    "report used by project managers and construction managers. Answer "
    "concisely and in plain language. When report context is supplied, ground "
    "your answer in it and say so if the context does not contain the answer."
)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


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
        # ---- GROUNDING HOOK -------------------------------------------------
        # To let Claude query Storco systems live, add your hosted MCP servers
        # here and use the beta mcp-client header. Example:
        # "mcp_servers": [
        #     {"type": "url", "name": "primavera",
        #      "url": "https://primavera-mcp.onrender.com/mcp"},
        # ],
        # (also send header "anthropic-beta": "mcp-client-2025-04-04")
        # ---------------------------------------------------------------------
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(ANTHROPIC_URL, json=payload, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic error: {r.text}")

    data = r.json()
    reply = "".join(
        block["text"] for block in data["content"] if block.get("type") == "text"
    )
    return ChatResponse(reply=reply or "(no text response)")
