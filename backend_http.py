"""
Demonstrator backend — Harness (JWT-only), with CLIENT-SIDE TOOL USE.

Inline-function tool-use loop that WORKS by reconstructing the toolUse/toolResult
pairing on the continuation POST (the harness's stored session history does not
reliably persist the assistant toolUse turn in this preview build, so we hand
Bedrock the full pairing ourselves).

Tools (client-side inline functions):
  - add_numbers : deterministic, proves the loop
  - get_time    : no-parameter tool
  - get_hn_top  : live network call — fetches top Hacker News stories

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
# Continuation strategy:
#   True  -> POST [user(prompt), assistant(toolUse), user(toolResult)] ourselves
#            (required: the harness does not reliably persist the toolUse turn)
#   False -> POST only the toolResult (original behaviour; fails with a
#            ValidationException in this preview build)
RECONSTRUCT_HISTORY = True

HN_BASE = "https://hacker-news.firebaseio.com/v0"  # (plus utilisé par un outil ; à retirer si inutile ailleurs)

# Reported back to the client so the UI can show which model answered.
# Set this to whatever the harness is actually configured with.
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


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
# Core invoke  (le harness gère lui-même ses outils natifs :
# navigation web / browser / web_search cote serveur AgentCore.
# Le backend ne fait que relayer.)
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
    """Extract text, stopReason, model, usage. Handles flat AND wrapped event
    shapes. Also surfaces a server-side ValidationException carried inside the
    stream."""
    text_parts = []
    stop_reason = None
    server_error = None
    model_id = None
    usage = None

    events = list(iter_events(raw))
    if DEBUG_STREAM:
        log.info("---- STREAM %s: %d events ----", tag, len(events))
        for e in events:
            log.info("EVT %s", json.dumps(e, ensure_ascii=False)[:400])

    for evt in events:
        if "message" in evt and "ValidationException" in str(evt.get("message", "")):
            server_error = evt["message"]

        # Model id can appear on the messageStart event (Bedrock converse-stream).
        ms = evt.get("messageStart", {}) if "messageStart" in evt else evt
        if isinstance(ms, dict):
            model_id = model_id or ms.get("model") or ms.get("modelId")

        # Usage/token counts on metadata event, if the harness forwards it.
        meta = evt.get("metadata", {}) if "metadata" in evt else {}
        if isinstance(meta, dict) and meta.get("usage"):
            usage = meta["usage"]

        delta = evt.get("contentBlockDelta", {}).get("delta") \
            if "contentBlockDelta" in evt else evt.get("delta")
        stop = evt.get("messageStop", {}).get("stopReason") \
            if "messageStop" in evt else evt.get("stopReason")

        if isinstance(delta, dict) and "text" in delta:
            text_parts.append(delta["text"])

        if stop is not None:
            stop_reason = stop

    if not text_parts and server_error is None:
        if DEBUG_STREAM:
            log.warning("STREAM %s: no text from structured deltas; regex fallback.", tag)
        txt = raw.decode("utf-8", errors="replace")
        for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', txt):
            try:
                text_parts.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                text_parts.append(m.group(1))

    parsed = {"text": "".join(text_parts),
              "stopReason": stop_reason, "serverError": server_error,
              "modelId": model_id, "usage": usage}
    if DEBUG_STREAM:
        log.info("PARSED %s -> stop=%s err=%s model=%s text=%r",
                 tag, parsed["stopReason"], bool(server_error),
                 model_id, parsed["text"][:160])
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

    t_start = time.time()
    original_user_msg = {"role": "user", "content": [{"text": req.prompt}]}

    body = {"actorId": actor_id, "messages": [original_user_msg]}
    raw = post_to_harness(authorization, req.sessionId, body, tag="invoke")
    parsed = parse_stream(raw, tag="invoke")

    model_id = parsed.get("modelId") or MODEL_ID
    usage = parsed.get("usage")
    server_error = parsed.get("serverError")

    final_text = parsed["text"].strip()

    if not final_text and server_error:
        final_text = ("[harness error] l'invocation a echoue cote Bedrock "
                      "(voir logs). ValidationException recue.")

    elapsed_ms = int((time.time() - t_start) * 1000)

    trace = [{
        "step": "model",
        "round": 0,
        "stopReason": parsed.get("stopReason"),
        "hasText": bool(parsed["text"]),
    }]

    if DEBUG_STREAM:
        log.info("========== /chat done stop=%s err=%s result=%r ==========",
                 parsed.get("stopReason"), bool(server_error), final_text[:160])

    return {
        "result": final_text or "(no text in response)",
        "stopReason": parsed.get("stopReason"),
        "serverError": server_error,
        "model": model_id,
        "usage": usage,
        "elapsedMs": elapsed_ms,
        "trace": trace,
    }