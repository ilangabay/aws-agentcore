"""
Demonstrator backend — Harness (JWT-only), with CLIENT-SIDE TOOL USE.

Tool-use loop: when the harness asks to call a client-side "Fonction
personnalisee" (inline_function), THIS backend executes it and returns the
result, looping until the harness produces a final answer.

The inline_function tools are registered ON THE HARNESS RESOURCE (via the
console), so they are NOT declared on each invoke — the harness already knows
them. This backend only needs to (a) run the local function when asked and
(b) send the result back.

Flow per user turn:
  1. POST the user message to /harnesses/invoke (bearer auth).
  2. Parse the streamed events. If a toolUse block appears -> run the local
     function, POST a toolResult *content block inside a user message* back in
     the SAME session, and continue.
  3. Loop until the stream stops with a reason other than tool_use.

Auth + per-user memory unchanged: bearer token forwarded, actorId = token 'sub'.

Run:
  uv pip install fastapi uvicorn requests botocore
  uv run uvicorn backend_http:app --reload --port 8080
"""

import os
import re
import json
import base64
import urllib.parse
import requests
from botocore.eventstream import EventStreamBuffer
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ----- Config -----
REGION      = "us-east-1"
HARNESS_ARN = os.environ.get(
    "HARNESS_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:474668391771:harness/harness_mayeultest-ctS3wcwdvk",
)
HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
ALLOWED_ORIGINS = [
    "https://main.d9gpnd3ah72nf.amplifyapp.com",
    "http://localhost:8080",
    "http://localhost:3000",
]
MAX_TOOL_ROUNDS = 5   # safety cap on tool-use loop iterations
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# ============================================================
# CLIENT-SIDE TOOLS
# Each tool is a Python function. The dispatcher maps the tool
# name to the function. The name/schema is declared ON THE HARNESS
# (console -> Fonctions personnalisees), NOT here — so the keys
# below MUST match the "Nom" of each Fonction personnalisee exactly.
# ============================================================

def tool_add_numbers(args: dict):
    """Deterministic, no external call — ideal to prove the loop works."""
    a = float(args.get("a", 0))
    b = float(args.get("b", 0))
    return {"result": a + b}

def tool_get_time(args: dict):
    import datetime
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}

# Map Harness tool names -> Python callables.
# NOTE: these keys must exactly match the "Nom" fields configured on the harness.
TOOLS = {
    "add_numbers": tool_add_numbers,
    "get_time": tool_get_time,
}


def handle_tool_call(name: str, tool_input: dict):
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'"}, "error"
    try:
        return fn(tool_input or {}), "success"
    except Exception as e:
        return {"error": str(e)}, "error"


# ============================================================
# Auth helpers
# ============================================================
def decode_claims(token: str) -> dict:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


class ChatRequest(BaseModel):
    prompt: str
    sessionId: str


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/me")
def me(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    claims = decode_claims(authorization.split(" ", 1)[1])
    return {"userId": claims.get("sub"),
            "username": claims.get("username") or claims.get("cognito:username")}


# ============================================================
# Core invoke + tool-use loop
# ============================================================
def post_to_harness(authorization: str, session_id: str, body: dict) -> bytes:
    """One POST to /harnesses/invoke. Returns raw event-stream bytes."""
    url = f"{HOST}/harnesses/invoke?harnessArn={urllib.parse.quote(HARNESS_ARN, safe='')}"
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        SESSION_HEADER: session_id,
    }
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    if r.status_code in (401, 403):
        raise HTTPException(r.status_code, f"authorizer rejected: {r.text}")
    if not r.ok:
        raise HTTPException(r.status_code, r.text)
    return r.content


def parse_stream(raw: bytes) -> dict:
    """
    Decode the event-stream and extract:
      - accumulated final text,
      - a toolUse request (name, input, toolUseId), reconstructed from the
        contentBlockStart header + partial-json input deltas,
      - the stopReason.
    Returns { text, toolUse, stopReason }.

    Note: toolUse only ever appears nested under contentBlockStart.start or
    contentBlockDelta.delta — never as a top-level event key.
    """
    text_parts = []
    tool_use = None
    stop_reason = None
    tool_input_json = ""   # toolUse input streams as partial-json string deltas

    def scan_event(evt):
        nonlocal tool_use, stop_reason, tool_input_json

        # --- content block start: may carry the toolUse header (name + id) ---
        start = evt.get("contentBlockStart", {}).get("start", {})
        if "toolUse" in start:
            tu = start["toolUse"]
            # start a fresh tool_use; input arrives via deltas below
            tool_use = {"name": tu.get("name"),
                        "toolUseId": tu.get("toolUseId"),
                        "input": {}}

        # --- content block delta: text or partial-json tool input ---
        delta = evt.get("contentBlockDelta", {}).get("delta", {})
        if isinstance(delta, dict):
            if "text" in delta:
                text_parts.append(delta["text"])
            if "toolUse" in delta:
                # delta.toolUse.input is a STRING fragment of JSON
                tool_input_json += delta["toolUse"].get("input", "") or ""

        # --- message stop: the reason we halted (tool_use / end_turn / ...) ---
        if "messageStop" in evt:
            stop_reason = evt["messageStop"].get("stopReason")

    try:
        buf = EventStreamBuffer()
        buf.add_data(raw)
        for event in buf:
            if not event.payload:
                continue
            try:
                scan_event(json.loads(event.payload.decode("utf-8")))
            except Exception:
                continue
    except Exception:
        pass

    # Reconstruct tool input from accumulated partial-json deltas.
    if tool_use is not None and tool_input_json:
        try:
            tool_use["input"] = json.loads(tool_input_json)
        except Exception:
            # leave input as {} rather than passing a raw string downstream
            pass

    # Fallback text recovery if the structured decode found nothing.
    if not text_parts:
        txt = raw.decode("utf-8", errors="replace")
        for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', txt):
            try:
                text_parts.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                text_parts.append(m.group(1))

    return {"text": "".join(text_parts), "toolUse": tool_use, "stopReason": stop_reason}


@app.post("/chat")
def chat(req: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    actor_id = decode_claims(token).get("sub")
    if not actor_id:
        raise HTTPException(401, "Token has no 'sub' claim")

    # First round: just the user message. Tools live on the harness resource.
    body = {
        "actorId": actor_id,
        "messages": [{"role": "user", "content": [{"text": req.prompt}]}],
    }
    raw = post_to_harness(authorization, req.sessionId, body)
    parsed = parse_stream(raw)

    # Tool-use loop: while the harness asks for a client-side tool, run it and reply.
    rounds = 0
    tools_used = []
    while parsed.get("toolUse") and parsed.get("stopReason") == "tool_use" \
            and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        tu = parsed["toolUse"]
        tools_used.append(tu["name"])
        result, status = handle_tool_call(tu["name"], tu.get("input", {}))

        # Return the result as a toolResult CONTENT BLOCK inside a user message,
        # on the same session, so the harness continues its reasoning.
        body = {
            "actorId": actor_id,
            "messages": [{
                "role": "user",
                "content": [{
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": json.dumps(result)}],
                        "status": status,
                    }
                }],
            }],
        }
        raw = post_to_harness(authorization, req.sessionId, body)
        parsed = parse_stream(raw)

    return {
        "result": parsed.get("text") or "(no text in response)",
        "toolsUsed": tools_used,   # so the frontend can show what fired
        "stopReason": parsed.get("stopReason"),
        "toolRounds": rounds,
    }