# AWS Bedrock AgentCore — Hands-On Course

A practical, lab-driven introduction to Amazon Bedrock AgentCore. Each lab is
self-contained, builds on the previous one, and ends with a concrete "you'll
know it worked when…" checkpoint. The labs deliberately surface the real
gotchas — the things that don't appear in the marketing but bite you in
practice.

**Prerequisites**
- **Solid AWS familiarity is required**, not just nice-to-have. This course
  assumes you're comfortable with IAM (roles, policies, execution roles), the
  AWS console and CLI, S3, and the general model of AWS credentials and regions.
  Several labs fail in confusing ways if IAM concepts are shaky — the errors you
  hit are usually permission or identity problems, not AgentCore problems.
- An AWS account with Bedrock AgentCore access (us-east-1 recommended).
- AWS CLI and the `agentcore` CLI installed and authenticated (see **Lab 0**).
- Python 3.11+ and `uv` (or `pip`), Node.js, and access to an agent framework
  (Strands or LangChain).
- Basic familiarity with an agent framework (Strands or LangChain).

**A note on the two "shapes" of AgentCore.** Throughout the course you'll work
with two distinct ways of building an agent: the **code runtime** (you write and
deploy a container) and the **Harness** (a no-code agent AWS assembles from
configuration). Keeping this distinction clear is the single most important
mental model in the course — several labs exist specifically to make the
difference concrete.

---

## Lab 0 — Setup: AWS CLI, credentials (with & without MFA), and the AgentCore CLI

Before any agent work, you need a working authenticated CLI. This lab covers
installation and the two credential paths — **without MFA** (simple, long-lived
keys) and **with MFA** (temporary session tokens, common in corporate accounts).
Get this right first; most "it doesn't work" moments later trace back to expired
or misconfigured credentials.

### 0.1 — Install the AWS CLI

**macOS:** `brew install awscli` — or the official pkg installer.
**Windows:** download the MSI from the AWS CLI page, or `winget install Amazon.AWSCLI`.
**Linux:**
```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install
```

Verify:
```bash
aws --version
```

### 0.2 — Credentials, path A: **without MFA** (long-lived keys)

The simplest case. In the AWS console, create an IAM user access key
(IAM → Users → your user → Security credentials → Create access key). Then:

```bash
aws configure
# AWS Access Key ID:     AKIA...        <- long-lived key (starts with AKIA)
# AWS Secret Access Key: ...
# Default region name:   us-east-1
# Default output format: json
```

This writes `~/.aws/credentials` and `~/.aws/config`. Verify:

```bash
aws sts get-caller-identity --region us-east-1
```

If it returns your account/user, you're authenticated. These keys don't expire,
so nothing further is needed — but they're also higher-risk if leaked, which is
why many organizations require MFA (path B).

### 0.3 — Credentials, path B: **with MFA** (temporary session tokens)

Corporate accounts commonly require MFA, meaning you exchange your **long-lived
keys + an MFA code** for **temporary session credentials** that expire (typically
1–12 hours). The mechanics that trip people up:

1. Your **base (long-lived) keys** must be configured first (path A above) — the
   MFA call authenticates *using* them.
2. You call `get-session-token` with your MFA device ARN and a **fresh** code.
3. You export the returned temporary credentials into your environment.

**Find your MFA device ARN:** IAM → Users → your user → Security credentials →
"Assigned MFA device" (looks like `arn:aws:iam::<ACCOUNT>:mfa/<name>`).

**Get a session token** (use a brand-new code from your authenticator — codes are
single-use and expire in ~30s):

```bash
aws sts get-session-token \
  --serial-number arn:aws:iam::<ACCOUNT>:mfa/<your-mfa-name> \
  --token-code <6-DIGIT-CODE> \
  --duration-seconds 43200
```

This returns `AccessKeyId`, `SecretAccessKey`, and `SessionToken`. Export them
(temporary keys start with **ASIA**, not AKIA):

```bash
export AWS_ACCESS_KEY_ID="ASIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."
```

(PowerShell: `$env:AWS_ACCESS_KEY_ID="ASIA..."`, etc.)

Verify:
```bash
aws sts get-caller-identity --region us-east-1
```

### The MFA gotchas (these waste the most time)

- **Codes are single-use and short-lived.** "Invalid MFA one time pass code"
  almost always means you reused a code or it expired. Wait for a fresh one, type
  the command first, then paste the code and run immediately.
- **The catch-22 of expiry.** `get-session-token` authenticates using your keys.
  If your *environment* still holds an **expired temporary session** (an `ASIA`
  key), it shadows your base keys and the refresh call itself fails with
  "ExpiredToken." Fix: clear the env vars so the CLI falls back to your permanent
  `AKIA` keys in `~/.aws/credentials`:
  ```bash
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  # PowerShell: Remove-Item Env:AWS_ACCESS_KEY_ID  (etc.)
  ```
  Then re-run `get-session-token` with a fresh code.
- **AKIA vs ASIA is your diagnostic.** `echo $env:AWS_ACCESS_KEY_ID` (or
  `$AWS_ACCESS_KEY_ID`): `AKIA` = permanent base key, `ASIA` = temporary session.
  If a refresh is failing, an `ASIA` value in your environment is the usual cause.
- **CloudShell sidesteps all of this.** The console's CloudShell (`>_` icon)
  authenticates off your *browser session* — no keys, no MFA dance, no expiry
  mid-task. If your local CLI credentials are fighting you, CloudShell is the
  reliable escape hatch (it has the AWS CLI, Docker, and your identity
  pre-loaded), unless your org disables it.

### 0.4 — Install the AgentCore CLI

The AgentCore CLI (`agentcore`) is a Python package:

```bash
uv pip install bedrock-agentcore-starter-toolkit
# or: pip install bedrock-agentcore-starter-toolkit
```

(Exact package name may vary — check current AWS docs. Verify with
`agentcore --help`.)

**Corporate environment note.** Behind an SSL-inspecting proxy, pip/uv installs
may fail on certificate verification. Point the installer at your corporate root
CA (e.g. `PIP_CERT` / `--cert`) if you hit cert errors — the same fix applies to
npm and any other package manager.

### Checkpoint

`aws sts get-caller-identity` returns your identity, and `agentcore --help` runs.
You understand whether your account uses long-lived keys (path A) or MFA session
tokens (path B), and how to refresh the latter.

---

## Lab 1 — Harness vs Runtime: build a code runtime with the AgentCore CLI

### Concept

AgentCore gives you two ways to run an agent on the same managed infrastructure:

- **Code runtime** — you author an agent in code (e.g. a Strands `Agent`),
  containerize it, and deploy it. *You* own the agent loop: when the model wants
  a tool, your code handles the call/observe/continue cycle. Maximum control,
  maximum wiring.
- **Harness** — a declarative, console-built agent. You toggle tools, memory,
  and identity; AWS runs the loop for you. Maximum speed, less control.

They share the same primitives (memory, gateway, identity, observability) but
are **invoked through different APIs** (`InvokeAgentRuntime` vs `InvokeHarness`)
and are not interchangeable. This lab builds the code runtime so you feel where
the control — and the wiring burden — lives.

### Lab

**1.1 — Write a minimal agent.** Create `my_agent.py`:

```python
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()
model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1")
agent = Agent(model=model)

@app.entrypoint
def handler(request):
    user_message = request.get("prompt", "Tell me a joke.")
    result = agent(user_message)
    return {"result": result.message}

if __name__ == "__main__":
    handler.run()
```

**1.2 — Deploy with the CLI.**

```bash
agentcore configure --entrypoint my_agent.py
agentcore launch
```

`configure` generates the container/config from your entrypoint; `launch` builds
and deploys to the runtime.

**1.3 — Invoke it.**

```bash
agentcore invoke '{"prompt": "Explain RAG in one sentence."}'
```

### Checkpoint

You get a JSON response with the model's answer. You've deployed a code runtime
and invoked it via the CLI.

### Discussion prompt

Notice the agent has *no tools and no memory* — it's a bare model call. Ask
yourself: to add a knowledge-base tool or memory, where does that logic go? (In
the code runtime, *you write it*. This is the control-vs-wiring trade the rest
of the course explores.)

---

## Lab 2 — Create a Harness and explore tools in the playground

### Concept

The Harness is the "buy" end of the spectrum. Instead of writing the agent loop,
you configure it: pick a model, write a system prompt, and toggle capabilities.
The **Terrain de jeu Harness** (playground) lets you converse with the agent and
watch tools fire — the fastest way to build intuition without deploying anything.

### Lab

**2.1 — Create a Harness.** In the console: Bedrock AgentCore → Harness → create.
Give it a name, choose a model, and write a short system prompt (e.g. "You are a
helpful assistant. Answer concisely.").

**2.2 — Enable a built-in tool.** In the Tools section, toggle on **Code
Interpreter** and/or **Browser**. These are sandboxed, AWS-managed tools — no
setup required.

**2.3 — Open the playground.** Go to Terrain de jeu Harness. Start a session and
try prompts that *force* a tool:

- Code Interpreter: "Compute the 20th Fibonacci number and show the code."
- Browser: "What's the top story on a news site right now?" (needs Browser on)

**2.4 — Watch the trace.** The playground shows the **trace automatically** as you
converse — you'll see the tool invocation and its result inline, alongside the
answer. Look for the tool-call step in the conversation view; you don't need to
open anything. (Separately, the **"Afficher l'observabilité"** button on the
Harness home page opens operational **metrics** — invocations, latency, error
rates — *not* per-turn traces. Two different things: metrics dashboard vs. the
live trace in the playground.)

### Checkpoint

You see a tool actually fire in the trace (a tool-call span with input and
result), distinguishing "the model answered from training" from "the model used
a tool."

### Gotcha (important)

**Prompt phrasing decides whether a tool fires.** A *capability question* ("do
you have a calculator?") makes the model answer introspectively ("yes!") without
acting. A *task* ("compute X") makes it use the tool. If a tool won't fire,
rephrase as a direct imperative task before assuming anything is broken.

---

## Lab 3 — Knowledge Bases and Gateways

### Concept

- A **Bedrock Knowledge Base (KB)** is managed RAG: point it at data → it
  chunks, embeds, stores vectors, and exposes a `retrieve` operation.
- A **Gateway** turns capabilities (APIs, Lambdas, OpenAPI specs, MCP servers,
  KBs) into **MCP tools** an agent can call. It's a tool *broker*.
- The **KB-via-Gateway** pattern is agentic RAG: the agent decides *when* to
  retrieve, calls the Gateway tool, gets chunks back, and grounds its answer.

### Lab

**3.1 — Create a KB.** Bedrock → Knowledge Bases → create. Use an S3 data source.
Upload a few documents to an S3 bucket first (drag-drop in the S3 console), then
point the KB's data source at that bucket.

**3.2 — Sync.** Select the data source → **Sync**. Ingestion is **sync-triggered,
never automatic** — files sitting in S3 are invisible until a sync runs.

**3.3 — Create a Gateway with a KB target.** Bedrock AgentCore → Passerelles →
create. Add a **Knowledge Base target** pointing at your KB.

**3.4 — Attach the Gateway to your Harness.** In the Harness Tools section, toggle
the Gateway on and select it.

**3.5 — Test retrieval.** In the playground, ask a **content question** whose
answer is only in your documents: "Search the knowledge base for X and summarize
what you find." Watch the trace for the Gateway/KB tool call and returned chunks.

### Checkpoint

The trace shows the KB tool firing, chunks returning, and the answer using them —
end-to-end agentic RAG.

### Gotcha

Use documents with **facts the model can't already know** (e.g. invented product
specs) so a correct answer *proves* retrieval rather than the model using general
knowledge. Real-world topics (famous countries, common facts) create ambiguity.

### Discussion prompt

Ingestion freshness is *your* responsibility — a KB doesn't auto-refresh. To keep
it current you build an EventBridge cron → `StartIngestionJob`, or an S3-event →
Lambda trigger. The managed service removes the RAG *internals*, not the
ingestion *orchestration*.

---

## Lab 4 — Long-term memory

### Concept

AgentCore memory has two horizons and three extraction strategies:

- **Short-term** — raw conversational turns, immediate, session-scoped.
- **Long-term** — distilled records, persistent, extracted asynchronously.
- **Strategies**: semantic facts (`/…/facts/`), session summaries
  (`/…/summaries/{sessionId}/`), user preferences (`/…/preferences/`).

The critical property: **long-term memory is scoped by `actorId`**, and
extraction is **asynchronous** (records appear seconds-to-minutes after the
conversation, not instantly).

### Lab

**4.1 — Attach memory to your Harness.** In the Harness Mémoire section, attach a
memory resource with the three strategies (or let the Harness auto-provision
one). Note its namespace convention (`/actors/{actorId}/...` for a
Harness-provisioned resource).

**4.2 — Teach a preference.** In a session, state a clear preference: "My favorite
country is Bolivia" or "Always answer in French."

**4.3 — Retrieve it directly (data plane).** After waiting for extraction, query
the store with the CLI to confirm the record exists:

```bash
echo '{"searchQuery":"favorite country"}' > q.json
aws bedrock-agentcore retrieve-memory-records \
  --memory-id <YOUR_MEMORY_ID> \
  --namespace "/actors/<ACTOR_ID>/preferences/" \
  --search-criteria file://q.json \
  --region us-east-1
```

**4.4 — Prove cross-session recall.** The key idea: long-term memory is scoped by
**`actorId`**, not by session — so the same actor recalls preferences across
different sessions. But note: **the playground does not expose an `actorId`
field** (it only lets you set a session ID). It uses its own actor internally, so
you can't deliberately control "same actor, new session" from the playground.

To test cross-session recall where you *control* the actor, use the CLI data
plane. First write a preference event under a chosen actor + session:

```bash
echo '[{"conversational":{"role":"USER","content":{"text":"My favorite country is Bolivia."}}},{"conversational":{"role":"ASSISTANT","content":{"text":"Noted."}}}]' > ev.json
aws bedrock-agentcore create-event \
  --memory-id <YOUR_MEMORY_ID> --actor-id student-01 \
  --session-id session-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1 \
  --event-timestamp $(date +%s) \
  --payload file://ev.json --region us-east-1
```

Wait for extraction (30–90s), then retrieve as the **same actor** — note the
retrieve command has no session ID, because the preference namespace is
actor-scoped, not session-scoped:

```bash
echo '{"searchQuery":"favorite country"}' > q.json
aws bedrock-agentcore retrieve-memory-records \
  --memory-id <YOUR_MEMORY_ID> \
  --namespace "/actors/student-01/preferences/" \
  --search-criteria file://q.json --region us-east-1
```

Getting the Bolivia preference back — from a query that never mentions the
original session — *is* the cross-session proof. (In a real app, the `actorId`
comes from the authenticated user's identity, e.g. the Cognito `sub` — that's
what makes memory per-user; see Lab 5 and Lab 6.)

### Checkpoint

`retrieve-memory-records` returns a distilled preference record, and a fresh
session recalls it.

### Gotcha

**Extraction latency causes false negatives.** If you query immediately after
stating a preference, the namespace looks empty — not because it's broken, but
because extraction hasn't run yet. Always wait, and verify with the direct
`retrieve-memory-records` call (the data-plane truth) before concluding anything
about the retrieval/injection layer. Also confirm the **`actorId`** you're
querying matches the one writes landed under (use `list-events` to check).

---

## Lab 5 — Inbound and outbound identity

### Concept

AgentCore Identity manages two directions:

- **Inbound** — who may call the agent. Default is IAM/SigV4. You can switch to a
  **JWT authorizer** (OAuth/OIDC via Cognito or Entra), which validates a bearer
  token instead of AWS credentials.
- **Outbound** — how the agent authenticates to downstream services: a
  credential vault holding API keys / OAuth tokens, or references to existing
  Secrets Manager ARNs.

### Lab

**5.1 — Observe the IAM default.** Your `agentcore invoke` calls have been
IAM-authorized all along (`sts get-caller-identity` shows the principal). No
setup — this is inbound identity by default.

**5.2 — Configure a Cognito JWT authorizer.** Edit your existing Harness (Harness →
your harness → Modifier). In its **inbound authentication** section, change the
type from IAM to **JWT**, and point it at a Cognito user pool: the discovery URL
(`https://cognito-idp.<region>.amazonaws.com/<POOL_ID>/.well-known/openid-configuration`)
and the allowed client ID. Save — you are modifying the Harness's inbound auth
configuration, which changes how *every* call to it is authenticated from now on.

**5.3 — Understand the consequence.** Enabling a JWT authorizer typically makes
the agent **JWT-only** — subsequent IAM/SigV4 calls are refused (403). The JWT
path and the SDK/SigV4 path become two distinct access routes.

**5.4 — Mint and present a token.** Create a Cognito user, set a permanent
password, and get an access token:

```bash
aws cognito-idp admin-set-user-password --user-pool-id <POOL> \
  --username testuser --password "TestPass1!" --permanent --region us-east-1

aws cognito-idp initiate-auth --client-id <CLIENT_ID> \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=testuser,PASSWORD="TestPass1!" --region us-east-1
```

**5.5 — Outbound (conceptual/optional).** Create an API-key credential provider in
AgentCore Identity; scope which agent can read it via the execution role's IAM
permissions on the provider ARN.

### Checkpoint

You can articulate the difference between IAM and JWT inbound auth, and you've
minted a Cognito access token. Bonus: an invoke with a valid token succeeds while
one without is rejected.

### Gotcha

The **access token vs ID token** distinction matters: a JWT authorizer that checks
the `client_id` claim wants the **access token** (the ID token carries the client
under `aud`). If auth is rejected with a "client_id mismatch," switch token types.

---

## Lab 6 — Deploy a full app: local frontend + backend calling the endpoint

### Concept

A realistic demo is three parts: a **browser frontend** (Cognito login → JWT), a
**thin backend** (receives the token, calls the agent), and the **agent** itself.
The browser can't call the agent API directly (no CORS, SigV4/bearer handling),
so the backend is structural. This lab keeps everything **local** — no CodeBuild,
no Amplify, no ECS — so you focus on the moving parts, not cloud plumbing.

### Lab

**6.1 — Backend (FastAPI).** A minimal service that forwards the bearer token to
the harness invoke endpoint. Core logic:

```python
# backend.py (essentials)
import os, json, urllib.parse, requests
from botocore.eventstream import EventStreamBuffer
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

REGION = "us-east-1"
HARNESS_ARN = os.environ["HARNESS_ARN"]
HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

class Req(BaseModel):
    prompt: str
    sessionId: str   # must be >= 33 chars

@app.post("/chat")
def chat(req: Req, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    url = f"{HOST}/harnesses/invoke?harnessArn={urllib.parse.quote(HARNESS_ARN, safe='')}"
    headers = {"Authorization": authorization, "Content-Type": "application/json",
               SESSION_HEADER: req.sessionId}
    body = {"messages": [{"role": "user", "content": [{"text": req.prompt}]}]}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    if not r.ok:
        raise HTTPException(r.status_code, r.text)
    # decode the event stream into text
    parts = []
    buf = EventStreamBuffer(); buf.add_data(r.content)
    for ev in buf:
        if not ev.payload: continue
        try:
            d = json.loads(ev.payload.decode())
            delta = d.get("delta") or d.get("contentBlockDelta", {}).get("delta", {})
            if "text" in delta: parts.append(delta["text"])
        except Exception: pass
    return {"result": "".join(parts)}
```

Run it:

```bash
uv pip install fastapi uvicorn requests botocore
export HARNESS_ARN="arn:aws:bedrock-agentcore:us-east-1:<ACCT>:harness/<NAME>"
uv run uvicorn backend:app --reload --port 8000
```

**6.2 — Frontend (single HTML file).** A complete, ready-to-use frontend is
provided as **`lab6_index.html`** (accompanying this course). It's a single file —
no build step, no framework — that:

- shows a login form and calls Cognito's `InitiateAuth` directly from the browser
  (no AWS SDK needed) to get an **access token**;
- sends that token as `Authorization: Bearer …` to your local backend's `/chat`;
- renders the agent's markdown replies in a simple chat UI;
- generates a valid (≥33 char) session ID automatically.

Before running, open `lab6_index.html` and fill in the two config values near the
bottom of the `<script>`:

```javascript
const CLIENT_ID = "REPLACE_WITH_YOUR_COGNITO_APP_CLIENT_ID";
const BACKEND   = "http://localhost:8000/chat";   // your local backend
```

Serve it over http (not `file://`, which breaks the fetch to localhost):

```bash
python -m http.server 8080
# open http://localhost:8080/lab6_index.html
```

**6.3 — Test end-to-end.** Log in with your Cognito user, send a message, see the
agent respond. The chain: browser → Cognito → access token → local backend →
harness → answer.

### Checkpoint

A locally-served page logs you in and chats with the deployed agent through your
local backend.

### Gotchas

- **Session ID ≥ 33 chars** — AgentCore rejects shorter `runtimeSessionId`.
  Generate a long one and pad to be safe.
- **The response is an AWS event stream** (`vnd.amazon.eventstream`), not plain
  JSON or SSE — decode it with `EventStreamBuffer`, or you'll see binary framing.
- **CORS** — the local backend needs permissive CORS for the browser to call it.

---

## Lab 7 (Bonus) — Inline functions and remote MCP

### Concept

Two ways to extend a Harness with *your own* tools:

- **Inline / client-side functions** ("Fonctions personnalisées") — you declare a
  tool (name + description + JSON schema) in the Harness; when the model calls it,
  **your application executes the function** and returns the result. This is a
  multi-round-trip loop within one user turn.
- **Remote MCP server** — you point the Harness at an external MCP server URL
  (with auth headers); its tools become available to the agent. The MCP server
  runs and executes the tools; the Harness just calls them.

### Lab

**7.1 — Declare an inline function.** In the Harness, add a Fonction
personnalisée named `add_numbers`, description "Adds two numbers," schema:

```json
{ "type": "object",
  "properties": { "a": {"type":"number"}, "b": {"type":"number"} },
  "required": ["a","b"] }
```

**7.2 — Understand the loop.** When the model decides to call `add_numbers`, the
invoke response emits a **tool-use event** (tool name, input, a `toolUseId`) and
stops. *Your code* runs the function, then sends a **tool-result** back in the
same session; the harness continues until it produces a final answer.

**7.3 — Implement the loop (backend).** Extend the Lab 6 backend: parse the stream
for a tool-use event, dispatch to a local function, POST the result back, loop
until done.

**7.4 — Test.** Ask a direct task: "What is 58 + 47?" (not "do you have a
calculator?"). The tool should fire and the agent should answer 105.

**7.5 — Remote MCP (optional).** In the Harness, enable "Serveur MCP distant,"
supply the server URL and any auth headers, and test a tool it exposes.

### Checkpoint

For inline: the tool-use loop completes — you see your function execute and the
agent's final answer incorporate the result.

### Gotcha (verify the wire protocol)

The exact **tool-use event shape** and the **tool-result reply format** over the
HTTP invoke path are the fiddly part and can differ by API version. Confirm them
against the boto3 service model rather than guessing:

```python
import boto3
c = boto3.client("bedrock-agentcore", region_name="us-east-1")
op = c.meta.service_model.operation_model("InvokeHarness")
for name, shape in op.input_shape.members.items():
    print(name, shape.type_name)
```

Inspect the output shape similarly to see how tool-use events are structured. As
always: phrase the test prompt as a **task**, not a capability question, or the
model won't attempt the tool.

---

## Course wrap-up — the mental model to leave with

1. **Two shapes, one infra.** Code runtime (you own the loop) vs Harness (AWS owns
   it). Same primitives underneath, different invoke APIs.
2. **Managed ≠ zero-work.** The KB removes RAG internals but not ingestion
   orchestration; memory removes extraction but is eventually-consistent; identity
   removes some auth code but not the wiring.
3. **The primitives are separable.** Adopt only what earns its place — you can use
   memory or gateway without buying into everything.
4. **Test with tasks, verify at the data plane.** Prompts must be imperative to
   trigger tools; and when behavior is surprising, check the underlying store /
   event stream directly rather than inferring from the chat.