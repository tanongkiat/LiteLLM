#!/usr/bin/env python3
"""
Test ChatGPT subscription (Codex CLI OAuth) auth directly against the ChatGPT
Codex backend — the "same way as Claude" test: use the per-user token the CLI
stored on this machine, sent per-request (what a forwarding gateway would do).

Claude vs Codex difference: Anthropic subscription auth hits the standard API
endpoint (api.anthropic.com) with a Bearer token. ChatGPT subscription auth
hits a different, non-public endpoint (chatgpt.com/backend-api/codex/responses)
and OpenAI checks that the request looks like the Codex CLI client.

This script sends three request shapes and reports which ones are accepted:
  A  bare        — Bearer token + minimal Responses payload
  B  codex-ish   — + account-id, originator, UA, OpenAI-Beta, session headers
  C  full mimic  — B + Codex base instructions + version header

Token discovery: ~/.codex/auth.json (respects $CODEX_HOME), or --token/--account-id.
Usage:
  python3 test_chatgpt_subscription.py                # try A/B/C, stop at first success
  python3 test_chatgpt_subscription.py --model gpt-5.5
"""

import argparse
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

CODEX_BACKEND = "https://chatgpt.com/backend-api/codex"
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
AUTH_FILE = os.path.join(CODEX_HOME, "auth.json")

CODex_UA_VERSION = "0.156.1"
ORIGINATOR = "codex_cli_rs"


def mask(token: str) -> str:
    if len(token) <= 20:
        return token[:4] + "…"
    return f"{token[:14]}…{token[-4:]} (len={len(token)})"


def load_auth(explicit_token, explicit_account):
    if explicit_token:
        return explicit_token, explicit_account, "argument"
    if not os.path.exists(AUTH_FILE):
        return None, None, None
    with open(AUTH_FILE) as f:
        auth = json.load(f)
    tokens = auth.get("tokens", {})
    if not tokens.get("access_token"):
        return None, None, None
    return tokens["access_token"], tokens.get("account_id"), AUTH_FILE


def jwt_claims(token: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    except Exception:
        return {}


def check_expiry(token: str):
    claims = jwt_claims(token)
    exp = claims.get("exp")
    if not exp:
        print("  (no exp claim — cannot check expiry)")
        return
    remaining_h = (exp - time.time()) / 3600
    if remaining_h <= 0:
        print(f"  !! access_token EXPIRED {abs(remaining_h):.1f}h ago — run `codex login` to refresh.")
    else:
        print(f"  access_token expires in {remaining_h:.1f}h "
              f"({datetime.datetime.fromtimestamp(exp):%Y-%m-%d %H:%M})")


def codex_headers(token: str, account_id: str, full: bool) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if not full:
        return headers
    headers.update({
        "chatgpt-account-id": account_id or "",
        "originator": ORIGINATOR,
        "user-agent": f"{ORIGINATOR}/{CODex_UA_VERSION} (Mac OS 15.6.0; arm64)",
        "openai-beta": "responses=experimental",
        "session_id": str(uuid.uuid4()),
        "version": CODex_UA_VERSION,
    })
    return headers


CODEX_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI "
    "on a user's computer."
)


def build_payload(model: str, prompt: str, full: bool) -> dict:
    payload = {
        "model": model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": prompt}]}],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "store": False,
        "stream": True,
    }
    if full:
        payload["instructions"] = CODEX_INSTRUCTIONS
        payload["include"] = ["reasoning.encrypted_content"]
        payload["prompt_cache_key"] = str(uuid.uuid4())
    return payload


def call(label: str, token: str, account_id: str, model: str, prompt: str, full: bool):
    print(f"\n[{label}]")
    req = urllib.request.Request(
        CODEX_BACKEND + "/responses",
        data=json.dumps(build_payload(model, prompt, full)).encode(),
        headers=codex_headers(token, account_id, full),
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        msg = body[:300]
        try:
            msg = json.loads(body).get("message") or json.loads(body).get("error", {}).get("message", msg)
        except Exception:
            pass
        print(f"  HTTP {e.code} in {time.time()-t0:.2f}s: {msg}")
        hints = {401: "token rejected → `codex login` to refresh.",
                 403: "client-shape enforcement or plan restriction.",
                 429: "rate limited by the ChatGPT plan."}
        if e.code in hints:
            print(f"  hint: {hints[e.code]}")
        return False

    # SSE: collect text deltas and the completed response
    text, usage, stop = [], {}, None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "response.output_text.delta":
            text.append(evt.get("delta", ""))
        elif evt.get("type") == "response.completed":
            stop = evt["response"].get("status")
            usage = evt["response"].get("usage", {})
        elif evt.get("type") == "response.failed":
            print(f"  response.failed: {json.dumps(evt.get('response', {}).get('error', {}))[:300]}")
            return False
    print(f"  200 OK in {time.time()-t0:.2f}s — reply: {''.join(text)!r}")
    if usage:
        print(f"  usage: in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
              f"cached={usage.get('input_tokens_details', {}).get('cached_tokens')}")
    print(f"  → request shape accepted: {label}")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token", help="explicit access_token (default: ~/.codex/auth.json)")
    p.add_argument("--account-id", help="chatgpt-account-id (default: from auth.json)")
    p.add_argument("--model", default="gpt-5.5", help="model slug (see ~/.codex/models_cache.json)")
    p.add_argument("--prompt", default="Reply with exactly: SUBSCRIPTION OK")
    p.add_argument("--only", choices=["bare", "codexish", "full"],
                   help="run only one request shape")
    args = p.parse_args()

    print("[1/2] Locating Codex CLI ChatGPT token")
    token, account_id, source = load_auth(args.token, args.account_id)
    if not token:
        sys.exit("No Codex auth found. Run `codex login` first, or pass --token.")
    print(f"  source     : {source}")
    print(f"  access_tok : {mask(token)}")
    print(f"  account_id : {account_id}")
    check_expiry(token)
    plan = jwt_claims(token).get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
    if plan:
        print(f"  plan       : {plan}")

    print("\n[2/2] POST backend-api/codex/responses — which request shapes are accepted?")
    shapes = {"bare": (False, "A bare"), "codexish": (False, "B codex-ish"), "full": (True, "C full mimic")}
    if args.only:
        full, label = shapes[args.only]
        ok = call(label, token, account_id, args.model, args.prompt, full)
    else:
        # escalate: bare → codex-ish → full mimic; stop at first success
        ok = call("A bare (Bearer only)", token, account_id, args.model, args.prompt, False)
        if not ok:
            ok = call("B codex-ish (headers, no instructions)", token, account_id, args.model, args.prompt, False)
        if not ok:
            ok = call("C full mimic (+ instructions, version)", token, account_id, args.model, args.prompt, True)
    print(f"\nRESULT: {'PASS — per-user ChatGPT forwarding works' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
