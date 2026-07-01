"""
Demonstrator backend — Harness (JWT-only), with PER-USER memory segregation.

Key fix vs. earlier version:
  - Decodes the Cognito access token to get the user's stable 'sub' claim.
  - Passes it as actorId on invoke_harness, so long-term memory is segregated
    per user (each user gets their own /users/{actorId}/preferences/ namespace).
  - Returns the username so the frontend can show who's signed in.

Flow:
  browser -> (Cognito access token) -> this backend -> POST /harnesses/invoke
             with Authorization: Bearer + actorId derived from the token.

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
# ------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def decode_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying signature (the harness authorizer
    verifies; we only need the claims to derive actorId + username). Safe here
    because the harness independently validates the token before responding."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)   # pad base64
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


class ChatRequest(BaseModel):
    prompt: str
    sessionId: str            # must be >= 33 chars (AgentCore constraint)


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/me")
def me(authorization: str = Header(None)):
    """Return the signed-in user's identity (frontend uses this to show who's in)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    claims = decode_claims(authorization.split(" ", 1)[1])
    return {
        "userId": claims.get("sub"),
        "username": claims.get("username") or claims.get("cognito:username"),
    }


@app.post("/chat")
def chat(req: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]

    # Derive a STABLE per-user actorId from the token. 'sub' is the unique,
    # immutable Cognito user id — this is what segregates long-term memory.
    claims = decode_claims(token)
    actor_id = claims.get("sub")
    if not actor_id:
        raise HTTPException(401, "Token has no 'sub' claim; cannot identify user")

    url = f"{HOST}/harnesses/invoke?harnessArn={urllib.parse.quote(HARNESS_ARN, safe='')}"
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        SESSION_HEADER: req.sessionId,
    }
    body = {
        "actorId": actor_id,                         # <-- per-user memory scoping
        "messages": [{"role": "user", "content": [{"text": req.prompt}]}],
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    except Exception as e:
        raise HTTPException(500, f"request failed: {e}")

    if r.status_code in (401, 403):
        raise HTTPException(r.status_code, f"authorizer rejected: {r.text}")
    if not r.ok:
        raise HTTPException(r.status_code, r.text)

    return {"result": extract_text(r.content)}


def extract_text(raw_bytes: bytes) -> str:
    parts = []
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
    text = raw_bytes.decode("utf-8", errors="replace")
    for m in re.finditer(r'"delta":\{"text":"((?:[^"\\]|\\.)*)"\}', text):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            parts.append(m.group(1))
    return "".join(parts) or "(no text in response)"