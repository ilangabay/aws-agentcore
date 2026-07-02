"""
Demonstrator backend — Harness (JWT-only), with CLIENT-SIDE TOOL USE.

IMPORTANT — stream shape:
This preview harness emits FLAT events, e.g.
    {"role": "assistant"}
    {"contentBlockIndex": 0, "delta": {"text": "Carr"}}
    {"contentBlockIndex": 1, "start": {"toolUse": {"name": ..., "toolUseId": ...}}}
    {"contentBlockIndex": 1, "delta": {"toolUse": {"input": "{\"a\":"}}}
    {"stopReason": "tool_use"}
i.e. the union members (delta / start / stopReason) sit at the TOP LEVEL, NOT
wrapped in contentBlockDelta / contentBlockStart / messageStop as the API
reference shows. parse_stream() reads the flat shape first and falls back to the
wrapped shape, so it works either way.

Inline_function tools are registered ON THE HARNESS RESOURCE (console), so they
are NOT declared on each invoke.

Run:
  uv pip install fastapi uvicorn requests botocore
  uv run uvicorn backend_http:app --reload --port 8080
"""

import os
import re
import json
import time
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
DEBUG_STREAM = True      # set False to silence diagnostic logging
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
        log.warning("DISPATCH MISS: harness asked for '%s'; known tools: %s",
                    name, list(TOOLS))
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
def post_to_harness(authorization: str, session_id: str, body: dict, tag: str = "") -> bytes:
    url = f"{HOST}/harnesses/invoke?harnessArn={urllib.parse.quote(HARNESS_ARN, safe='')}"
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        SESSION_HEADER: session_id,
    }
    if DEBUG_STREAM:
        log.info(">>> POST %s session=%s body=%s",
                 tag, session_id, json.dumps(body, ensure_ascii=False)[:600])
    t0 = time.time()
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    dt = (time.time() - t0) * 1000
    if DEBUG_STREAM:
        log.info("<<< POST %s status=%s latency=%.0fms bytes=%d",
                 tag, r.status_code, dt, len(r.content))
    if r.status_code in (401, 403):
        raise HTTPException(r.status_code, f"authorizer rejected: {r.text}")
    if not r.ok:
        raise HTTPException(r.status_code, r.text)
    return r.content


def iter_events(raw: bytes):
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
    Extract accumulated text, a toolUse request, and the stopReason from the
    stream. Handles BOTH the flat event shape this harness emits and the wrapped
    shape from the API reference.
    """
    text_parts = []
    tool_use = None
    stop_reason = None
    tool_input_json = ""

    events = list(iter_events(raw))

    if DEBUG_STREAM:
        log.info("---- STREAM %s: %d events ----", tag, len(events))
        for e in events:
            log.info("EVT %s", json.dumps(e, ensure_ascii=False)[:400])

    for evt in events:
        # Normalize: unwrap the wrapped shape if present, else use the event
        # itself (flat shape).
        start = evt.get("contentBlockStart", {}).get("start") \
            if "contentBlockStart" in evt else evt.get("start")
        delta = evt.get("contentBlockDelta", {}).get("delta") \
            if "contentBlockDelta" in evt else evt.get("delta")
        stop = evt.get("messageStop", {}).get("stopReason") \
            if "messageStop" in evt else evt.get("stopReason")

        # --- content block start: toolUse header (name + id) ---
        if isinstance(start, dict) and "toolUse" in start:
            tu = start["toolUse"]
            tool_use = {"name": tu.get("name"),
                        "toolUseId": tu.get("toolUseId"),
                        "input": {}}

        # --- content block delta: text or partial-json tool input ---
        if isinstance(delta, dict):
            if "text" in delta:
                text_parts.append(delta["text"])
            if "toolUse" in delta:
                tool_input_json += delta["toolUse"].get("input", "") or ""

        # --- stop reason ---
        if stop is not None:
            stop_reason = stop

    if tool_use is not None and tool_input_json:
        try:
            tool_use["input"] = json.loads(tool_input_json)
        except Exception:
            log.warning("STREAM %s: tool input JSON failed to parse. buffer=%r",
                        tag, tool_input_json[:300])

    # fallback text recovery — if this fires, structured parsing missed something
    if not text_parts:
        if DEBUG_STREAM:
            log.warning("STREAM %s: no text from structured deltas; regex fallback.", tag)
        txt = raw.decode("utf-8", errors="replace")
        for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', txt):
            try:
                text_parts.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                text_parts.append(m.group(1))

    parsed = {"text": "".join(text_parts), "toolUse": tool_use, "stopReason": stop_reason}
    if DEBUG_STREAM:
        log.info("PARSED %s -> stopReason=%s toolUse=%s input=%s text=%r",
                 tag, parsed["stopReason"],
                 parsed["toolUse"]["name"] if parsed["toolUse"] else None,
                 parsed["toolUse"]["input"] if parsed["toolUse"] else None,
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

    if DEBUG_STREAM:
        log.info("========== /chat prompt=%r session=%s ==========",
                 req.prompt, req.sessionId)

    all_text = []

    body = {
        "actorId": actor_id,
        "messages": [{"role": "user", "content": [{"text": req.prompt}]}],
    }
    raw = post_to_harness(authorization, req.sessionId, body, tag="round0")
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
        raw = post_to_harness(authorization, req.sessionId, body, tag=f"round{rounds}")
        parsed = parse_stream(raw, tag=f"round{rounds}")
        if parsed["text"]:
            all_text.append(parsed["text"])

    if rounds >= MAX_TOOL_ROUNDS and parsed.get("stopReason") == "tool_use":
        log.warning("Hit MAX_TOOL_ROUNDS (%d) still asking for tools.", MAX_TOOL_ROUNDS)

    final_text = parsed["text"].strip() or "\n".join(t for t in all_text if t.strip())

    if DEBUG_STREAM:
        log.info("========== /chat done rounds=%d toolsUsed=%s stop=%s result=%r ==========",
                 rounds, tools_used, parsed.get("stopReason"), final_text[:200])

    return {
        "result": final_text or "(no text in response)",
        "allText": all_text,
        "toolsUsed": tools_used,
        "stopReason": parsed.get("stopReason"),
        "toolRounds": rounds,
    }