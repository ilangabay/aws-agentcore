"""
Demonstrator backend — Harness (JWT-only), with CLIENT-SIDE TOOL USE.

INSTRUMENTED build: logs the raw decoded events and the parsed result at every
round, so you can see exactly what the harness streams back and whether the
tool actually fires / the final number ever arrives.

The inline_function tools are registered ON THE HARNESS RESOURCE (via the
console), so they are NOT declared on each invoke.

Flow per user turn:
  1. POST the user message to /harnesses/invoke (bearer auth).
  2. Parse the streamed events. If a toolUse block appears -> run the local
     function, POST a toolResult content block (inside a user message) back on
     the SAME session, and continue.
  3. Loop until the stream stops with a reason other than tool_use.
     Text from EVERY round is accumulated, so a final-round number is never lost.

Run:
  uv pip install fastapi uvicorn requests botocore
  uv run uvicorn backend_http:app --reload --port 8080
"""

import os
import re
import json
import base64
import logging
import urllib.parse
import requests
from botocore.eventstream import EventStreamBuffer
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("harness")

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
MAX_TOOL_ROUNDS = 5      # safety cap on tool-use loop iterations
DEBUG_STREAM = True      # set False to silence raw-event logging
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# ============================================================
# CLIENT-SIDE TOOLS
# Keys MUST match the "Nom" of each Fonction personnalisee on the harness.
# ============================================================

def tool_add_numbers(args: dict):
    a = float(args.get("a", 0))
    b = float(args.get("b", 0))
    return {"result": a + b}

def tool_get_time(args: dict):
    import datetime
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}

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


def iter_events(raw: bytes):
    """Yield decoded JSON events from an event-stream byte blob."""
    buf = EventStreamBuffer()
    buf.add_data(raw)
    for event in buf:
        if not event.payload:
            continue
        try:
            yield json.loads(event.payload.decode("utf-8"))
        except Exception:
            yield {"_undecodable": event.payload[:200].decode("utf-8", "replace")}


def parse_stream(raw: bytes, tag: str = "") -> dict:
    """
    Decode the event-stream and extract:
      - accumulated text for THIS invoke,
      - a toolUse request (name, input, toolUseId),
      - the stopReason.
    Returns { text, toolUse, stopReason }.
    """
    text_parts = []
    tool_use = None
    stop_reason = None
    tool_input_json = ""

    events = list(iter_events(raw))
    if DEBUG_STREAM:
        log.info("---- RAW STREAM %s (%d events) ----", tag, len(events))
        for e in events:
            log.info("EVT %s", json.dumps(e, ensure_ascii=False)[:400])

    for evt in events:
        # content block start: toolUse header (name + id)
        start = evt.get("contentBlockStart", {}).get("start", {})
        if "toolUse" in start:
            tu = start["toolUse"]
            tool_use = {"name": tu.get("name"),
                        "toolUseId": tu.get("toolUseId"),
                        "input": {}}

        # content block delta: text or partial-json tool input
        delta = evt.get("contentBlockDelta", {}).get("delta", {})
        if isinstance(delta, dict):
            if "text" in delta:
                text_parts.append(delta["text"])
            if "toolUse" in delta:
                tool_input_json += delta["toolUse"].get("input", "") or ""

        # message stop
        if "messageStop" in evt:
            stop_reason = evt["messageStop"].get("stopReason")

    if tool_use is not None and tool_input_json:
        try:
            tool_use["input"] = json.loads(tool_input_json)
        except Exception:
            pass

    # fallback text recovery
    if not text_parts:
        txt = raw.decode("utf-8", errors="replace")
        for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', txt):
            try:
                text_parts.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                text_parts.append(m.group(1))

    parsed = {"text": "".join(text_parts), "toolUse": tool_use, "stopReason": stop_reason}
    if DEBUG_STREAM:
        log.info("PARSED %s -> stopReason=%s toolUse=%s text=%r",
                 tag, parsed["stopReason"],
                 parsed["toolUse"]["name"] if parsed["toolUse"] else None,
                 parsed["text"][:200])
    return parsed


@app.post("/chat")
def chat(req: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    actor_id = decode_claims(token).get("sub")
    if not actor_id:
        raise HTTPException(401, "Token has no 'sub' claim")

    # Accumulate text across ALL rounds so a final-round number is never lost.
    all_text = []

    # First round: user message. Tools live on the harness resource.
    body = {
        "actorId": actor_id,
        "messages": [{"role": "user", "content": [{"text": req.prompt}]}],
    }
    raw = post_to_harness(authorization, req.sessionId, body)
    parsed = parse_stream(raw, tag="round0")
    if parsed["text"]:
        all_text.append(parsed["text"])

    rounds = 0
    tools_used = []
    while parsed.get("toolUse") and parsed.get("stopReason") == "tool_use" \
            and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        tu = parsed["toolUse"]
        tools_used.append(tu["name"])
        result, status = handle_tool_call(tu["name"], tu.get("input", {}))
        log.info("TOOL CALL #%d name=%s input=%s -> %s (%s)",
                 rounds, tu["name"], tu.get("input"), result, status)

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
        parsed = parse_stream(raw, tag=f"round{rounds}")
        if parsed["text"]:
            all_text.append(parsed["text"])

    # Prefer the LAST non-empty round's text (usually the final answer),
    # but fall back to the full accumulation if the last round was empty.
    final_text = parsed["text"].strip() or "\n".join(t for t in all_text if t.strip())

    return {
        "result": final_text or "(no text in response)",
        "allText": all_text,                 # every round's text, for debugging
        "toolsUsed": tools_used,
        "stopReason": parsed.get("stopReason"),
        "toolRounds": rounds,
    }