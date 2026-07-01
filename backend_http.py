"""
Demonstrator backend — Harness, JWT-only (Bearer) path.

Flow:
  browser -> (Cognito access token) -> this backend -> POST /harnesses/invoke
             with 'Authorization: Bearer <jwt>' -> Harness authorizer validates

This harness is JWT-ONLY (SigV4/boto3 returns 403 "requires OAuth Bearer token").
So we POST directly to the real invoke endpoint that boto3 revealed on the wire,
swapping its SigV4 Authorization header for the user's Cognito Bearer token.

The response is an AWS event-stream (content-type vnd.amazon.eventstream): binary
framing wrapping JSON 'contentBlockDelta' events. We decode it with botocore's
EventStreamBuffer and concatenate the text deltas. A regex fallback guarantees we
still surface text even if the framing parser is unhappy.

Run:
  uv pip install fastapi uvicorn requests botocore
  uv run uvicorn backend_http:app --reload --port 8000
"""

import os
import re
import json
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
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    prompt: str
    sessionId: str            # must be >= 33 chars (AgentCore constraint)


def extract_text(raw_bytes: bytes) -> str:
    """Decode the AWS event-stream and concatenate contentBlockDelta text."""
    parts = []

    # Primary: proper event-stream framing decode.
    try:
        buf = EventStreamBuffer()
        buf.add_data(raw_bytes)
        for event in buf:
            payload = event.payload
            if not payload:
                continue
            try:
                evt = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            delta = evt.get("delta") or evt.get("contentBlockDelta", {}).get("delta", {})
            if isinstance(delta, dict) and "text" in delta:
                parts.append(delta["text"])
    except Exception:
        pass

    if parts:
        return "".join(parts)

    # Fallback: pull every "text":"..." out of the raw bytes. The JSON payloads
    # are intact inside the binary frames, so this recovers the message even if
    # framing parsing failed.
    text = raw_bytes.decode("utf-8", errors="replace")
    for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', text):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            parts.append(m.group(1))
    return "".join(parts)


@app.post("/chat")
def chat(req: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")

    url = f"{HOST}/harnesses/invoke?harnessArn={urllib.parse.quote(HARNESS_ARN, safe='')}"
    headers = {
        "Authorization": authorization,            # forward the Cognito Bearer token
        "Content-Type": "application/json",
        SESSION_HEADER: req.sessionId,
    }
    body = {"messages": [{"role": "user", "content": [{"text": req.prompt}]}]}

    try:
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    except Exception as e:
        raise HTTPException(500, f"request failed: {e}")

    if r.status_code in (401, 403):
        raise HTTPException(r.status_code, f"authorizer rejected: {r.text}")
    if not r.ok:
        raise HTTPException(r.status_code, r.text)

    result = extract_text(r.content)
    return {"result": result or "(no text in response)"}