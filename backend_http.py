"""
Demonstrator backend — Harness (JWT-only), with CLIENT-SIDE TOOL USE.

DECISIVE TEST BUILD
-------------------
Prior finding: with a valid tool schema, round0 is now a clean toolUse-ONLY
assistant turn (no interleaved text), yet round1 still fails with:
    ValidationException: The number of toolResult blocks at messages.1.content
    exceeds the number of toolUse blocks of previous turn.
This means the harness's OWN session-history assembly is not persisting the
assistant toolUse turn between our two POSTs — Bedrock sees "previous turn had
0 toolUse blocks" and rejects our (correct) toolResult.

This build tests the hypothesis by NOT trusting the harness's stored history:
on the continuation POST we RECONSTRUCT the full pairing ourselves —
    [ user(prompt), assistant(toolUse), user(toolResult) ]
in a single messages array.

  - If this SUCCEEDS where the toolResult-only body failed  -> confirmed: the
    harness lost the toolUse turn; the workaround is to send the pair ourselves.
  - If this STILL fails                                     -> genuine harness /
    Strands preview bug; the reliable path is owning the loop against Runtime.

Toggle RECONSTRUCT_HISTORY to compare both behaviours in one build.

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
MAX_TOOL_ROUNDS = 5
DEBUG_STREAM = True

# THE decisive switch.
#   True  -> continuation POST carries [user, assistant(toolUse), user(toolResult)]
#            (do NOT rely on harness-stored history)
#   False -> continuation POST carries only the toolResult (original behaviour)
RECONSTRUCT_HISTORY = True
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# ============================================================
# CLIENT-SIDE TOOLS  (keys MUST match the harness "Nom" fields)
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
        log.warning("DISPATCH MISS: harness asked for '%s'; known: %s", name, list(TOOLS))
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
                 tag, session_id, json.dumps(body, ensure_ascii=False)[:900])
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
    """Extract text, toolUse, stopReason. Handles flat AND wrapped event shapes.
    Also surfaces a server-side ValidationException carried inside the stream."""
    text_parts = []
    tool_use = None
    stop_reason = None
    tool_input_json = ""
    server_error = None

    events = list(iter_events(raw))
    if DEBUG_STREAM:
        log.info("---- STREAM %s: %d events ----", tag, len(events))
        for e in events:
            log.info("EVT %s", json.dumps(e, ensure_ascii=False)[:400])

    for evt in events:
        # a stream-embedded error event (this is how the harness reports the
        # Bedrock ValidationException — as a normal event, not an HTTP error)
        if "message" in evt and "ValidationException" in str(evt.get("message", "")):
            server_error = evt["message"]

        start = evt.get("contentBlockStart", {}).get("start") \
            if "contentBlockStart" in evt else evt.get("start")
        delta = evt.get("contentBlockDelta", {}).get("delta") \
            if "contentBlockDelta" in evt else evt.get("delta")
        stop = evt.get("messageStop", {}).get("stopReason") \
            if "messageStop" in evt else evt.get("stopReason")

        if isinstance(start, dict) and "toolUse" in start:
            tu = start["toolUse"]
            tool_use = {"name": tu.get("name"),
                        "toolUseId": tu.get("toolUseId"),
                        "input": {}}

        if isinstance(delta, dict):
            if "text" in delta:
                text_parts.append(delta["text"])
            if "toolUse" in delta:
                tool_input_json += delta["toolUse"].get("input", "") or ""

        if stop is not None:
            stop_reason = stop

    if tool_use is not None and tool_input_json:
        try:
            tool_use["input"] = json.loads(tool_input_json)
        except Exception:
            log.warning("STREAM %s: tool input JSON failed to parse: %r",
                        tag, tool_input_json[:300])

    if not text_parts and server_error is None:
        if DEBUG_STREAM:
            log.warning("STREAM %s: no text from structured deltas; regex fallback.", tag)
        txt = raw.decode("utf-8", errors="replace")
        for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', txt):
            try:
                text_parts.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                text_parts.append(m.group(1))

    parsed = {"text": "".join(text_parts), "toolUse": tool_use,
              "stopReason": stop_reason, "serverError": server_error}
    if DEBUG_STREAM:
        log.info("PARSED %s -> stop=%s toolUse=%s input=%s err=%s text=%r",
                 tag, parsed["stopReason"],
                 parsed["toolUse"]["name"] if parsed["toolUse"] else None,
                 parsed["toolUse"]["input"] if parsed["toolUse"] else None,
                 bool(server_error), parsed["text"][:160])
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
        log.info("========== /chat prompt=%r session=%s reconstruct=%s ==========",
                 req.prompt, req.sessionId, RECONSTRUCT_HISTORY)

    all_text = []
    original_user_msg = {"role": "user", "content": [{"text": req.prompt}]}

    body = {"actorId": actor_id, "messages": [original_user_msg]}
    raw = post_to_harness(authorization, req.sessionId, body, tag="round0")
    parsed = parse_stream(raw, tag="round0")
    if parsed["text"]:
        all_text.append(parsed["text"])

    rounds = 0
    tools_used = []
    server_error = parsed.get("serverError")

    while parsed.get("toolUse") and parsed.get("stopReason") == "tool_use" \
            and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        tu = parsed["toolUse"]
        tools_used.append(tu["name"])
        result, status = handle_tool_call(tu["name"], tu.get("input", {}))
        log.info("TOOL CALL #%d name=%s input=%s -> %s (%s)",
                 rounds, tu["name"], tu.get("input"), result, status)

        tool_result_block = {
            "toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"text": json.dumps(result)}],
                "status": status,
            }
        }

        if RECONSTRUCT_HISTORY:
            # Do NOT trust the harness-stored history. Hand Bedrock the full
            # pairing ourselves: user(prompt) -> assistant(toolUse) -> user(result).
            assistant_tooluse_msg = {
                "role": "assistant",
                "content": [{
                    "toolUse": {
                        "toolUseId": tu["toolUseId"],
                        "name": tu["name"],
                        "input": tu.get("input", {}),
                    }
                }],
            }
            messages = [
                original_user_msg,
                assistant_tooluse_msg,
                {"role": "user", "content": [tool_result_block]},
            ]
        else:
            # Original behaviour: rely on the session to carry the toolUse turn.
            messages = [{"role": "user", "content": [tool_result_block]}]

        body = {"actorId": actor_id, "messages": messages}
        raw = post_to_harness(authorization, req.sessionId, body, tag=f"round{rounds}")
        parsed = parse_stream(raw, tag=f"round{rounds}")
        if parsed.get("serverError"):
            server_error = parsed["serverError"]
        if parsed["text"]:
            all_text.append(parsed["text"])

    final_text = parsed["text"].strip() or "\n".join(t for t in all_text if t.strip())

    # Clean degradation: if the loop died on a server-side validation error,
    # say so instead of returning an empty bubble.
    if not final_text and server_error:
        final_text = ("[harness error] la continuation d'outil a échoué côté "
                      "Bedrock (voir logs). ValidationException reçue.")

    if DEBUG_STREAM:
        log.info("========== /chat done rounds=%d tools=%s stop=%s err=%s result=%r ==========",
                 rounds, tools_used, parsed.get("stopReason"), bool(server_error),
                 final_text[:160])

    return {
        "result": final_text or "(no text in response)",
        "allText": all_text,
        "toolsUsed": tools_used,
        "stopReason": parsed.get("stopReason"),
        "toolRounds": rounds,
        "reconstructHistory": RECONSTRUCT_HISTORY,
        "serverError": server_error,   # None if clean; the ValidationException text if not
    }