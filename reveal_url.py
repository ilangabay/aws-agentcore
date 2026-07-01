"""
Reveal the real InvokeHarness URL + headers that boto3 hits on the wire.

The call will FAIL with AccessDenied (harness is JWT-only) — that's expected
and fine. We only care about the request line botocore logs BEFORE the response:
look for a line like:
    Making request for ... POST  https://bedrock-agentcore.us-east-1.amazonaws.com/...
and the headers dict, especially any key containing 'session-id'.

Run:
    python reveal_url.py
"""

import logging
import boto3

# Log every request botocore builds, including the full URL and headers.
logging.basicConfig(level=logging.DEBUG)
boto3.set_stream_logger("botocore", logging.DEBUG)

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:474668391771:harness/harness_mayeultest-ctS3wcwdvk"

c = boto3.client("bedrock-agentcore", region_name="us-east-1")

try:
    c.invoke_harness(
        harnessArn=HARNESS_ARN,
        runtimeSessionId="x" * 40,          # >= 33 chars
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
    )
except Exception as e:
    print("\n\n=== Call failed (expected) ===")
    print(type(e).__name__, str(e))
    print("\nScroll UP in the output above and find the line containing")
    print("  'POST' and 'https://'  ->  that is the invoke URL.")
    print("Also note any request header whose name contains 'session-id'.")