"""
Minimal demonstrator backend.

Flow:
  browser -> (Cognito JWT) -> this backend -> AgentCore InvokeAgentRuntime

This backend forwards the user's bearer token to AgentCore so the Harness's
JWT authorizer does the validation. That's the version that actually exercises
the inbound-auth you configured.

Run:
  pip install fastapi uvicorn boto3 python-jose[cryptography] requests
  uvicorn backend:app --reload --port 8000
"""

import os
import json
import boto3
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ----- Config: fill these with YOUR values -----
REGION = "us-east-1"
# The Harness runtime ARN (the agent007neverforgets page showed this shape;
# for the Harness, get its runtime ARN from its detail page / "code d'invocation").
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:474668391771:runtime/REPLACE_ME",
)
# -----------------------------------------------

app = FastAPI()

# CORS so the browser frontend (different origin) can call this backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your frontend origin for anything real
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    prompt: str
    sessionId: str = "demo-session-001"


@app.post("/chat")
def chat(req: ChatRequest, authorization: str = Header(None)):
    """
    Receives 'Authorization: Bearer <cognito-jwt>' from the frontend and
    forwards it to AgentCore. The Harness JWT authorizer validates the token.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    bearer_token = authorization.split(" ", 1)[1]

    # The data-plane client. NOTE: method name + how the bearer token is passed
    # is the version-sensitive bit. As of recent SDKs the call is
    # invoke_agent_runtime(...). To pass a JWT instead of SigV4, AgentCore expects
    # the token to ride in the request; depending on SDK version this is either:
    #   (a) a dedicated parameter, or
    #   (b) you call the runtime's HTTP invoke URL directly with an
    #       'Authorization: Bearer' header (see backend_http.py fallback).
    # Verify with: python -c "import boto3; c=boto3.client('bedrock-agentcore','us-east-1'); help(c.invoke_agent_runtime)"
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    payload = json.dumps({"prompt": req.prompt, "sessionId": req.sessionId}).encode()

    try:
        # Attempt the SDK path. If your SDK version exposes a bearer/token kwarg,
        # this is where it goes. If it does NOT, use backend_http.py instead.
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=req.sessionId,
            payload=payload,
            # If your SDK build supports it, something like:
            # bearerToken=bearer_token,
        )
    except Exception as e:
        # Surface the real error so we can see whether it's auth, ARN, or SDK shape.
        raise HTTPException(status_code=500, detail=f"invoke failed: {e}")

    # Response body is a streaming/bytes payload depending on SDK version.
    body = resp.get("response") or resp.get("payload")
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")

    return {"result": body}
