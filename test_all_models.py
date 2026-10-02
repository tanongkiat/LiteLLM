#!/usr/bin/env python3
"""
Test ALL models registered on the LiteLLM gateway, each with the auth method
it needs, and print a pass/fail table.

Discovery:  GET /v1/models on the gateway (key from .env or --key)
Routing per model:
  *name contains 'claude-sub' or 'codex-sub' → subscription path:
      gateway key in x-litellm-api-key + user's own OAuth token in
      Authorization (Claude: keychain/credentials.json; Codex: auth.json).
      Claude requests also carry the Claude Code system prompt + beta headers
      (without them Anthropic returns the cloaked 429).
  *everything else → plain OpenAI chat completions with the gateway key only.

Also (if ~/.codex/auth.json exists) probes the /codex/responses passthrough.

Usage:
  python3 test_all_models.py                       # test everything
  python3 test_all_models.py --gateway http://localhost:4242
  python3 test_all_models.py --skip-subscription   # API-key models only
  python3 test_all_models.py --only elastic-chat claude-sub
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
CC_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
CC_BETAS = "claude-code-20250219,oauth-2025-04-20"
PROMPT = "Reply with exactly: ALL MODELS OK"


# --------------------------------------------------------------------------- helpers

def load_dotenv(path=os.path.join(DIR, ".env")) -> dict:
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def request(url, headers, payload=None, timeout=120):
    """Returns (status, raw_text, elapsed). Callers parse JSON/SSE themselves
    (the /codex passthrough answers with SSE, not JSON)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            raw = json.dumps(json.loads(raw))[:400]
        except json.JSONDecodeError:
            raw = raw[:400]
        return e.code, raw, time.time() - t0


def claude_oauth_token() -> str | None:
    """Same discovery as test_claude_subscription.py: env → file → macOS keychain."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok
    cred = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(cred):
        tok = json.load(open(cred)).get("claudeAiOauth", {}).get("accessToken")
        if tok:
            return tok
    try:
        blob = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        return json.loads(blob).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def codex_access_token() -> str | None:
    auth = os.path.expanduser("~/.codex/auth.json")
    if os.path.exists(auth):
        return json.load(open(auth)).get("tokens", {}).get("access_token")
    return None


# --------------------------------------------------------------------------- testers

def test_openai_chat(gateway, key, model):
    status, raw, dt = request(f"{gateway}/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                              {"model": model, "messages": [{"role": "user", "content": PROMPT}],
                               "max_completion_tokens": 32})
    if status != 200:
        return False, status, dt, raw[:120]
    body = json.loads(raw)
    reply = body["choices"][0]["message"].get("content", "")
    usage = body.get("usage", {})
    return True, status, dt, f"reply={reply!r} tokens={usage.get('total_tokens')}"


def test_claude_subscription(gateway, key, model, oauth_token):
    headers = {
        "x-litellm-api-key": f"Bearer {key}",          # gateway identity
        "Authorization": f"Bearer {oauth_token}",       # user's seat, forwarded upstream
        "anthropic-beta": CC_BETAS,
    }
    payload = {"model": model, "max_tokens": 32,
               "system": [{"type": "text", "text": CC_SYSTEM}],   # required: Claude Code shape
               "messages": [{"role": "user", "content": PROMPT}]}
    status, raw, dt = request(f"{gateway}/v1/messages", headers, payload)
    if status != 200:
        hint = " (token expired? run `claude` once to refresh)" if status == 429 else ""
        return False, status, dt, f"{raw[:120]}{hint}"
    body = json.loads(raw)
    reply = "".join(b.get("text", "") for b in body.get("content", []))
    return True, status, dt, f"reply={reply!r}"


def test_codex_passthrough(gateway, key, access_token):
    headers = {
        "x-litellm-api-key": f"Bearer {key}",
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": "",
        "accept": "text/event-stream",
    }
    payload = {"model": "gpt-5.5",
               "input": [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": PROMPT}]}],
               "store": False, "stream": True}
    status, raw, dt = request(f"{gateway}/codex/responses", headers, payload)
    if status != 200:
        hint = " (token expired? run `codex login`)" if status in (401, 429) else ""
        return False, status, dt, f"{raw[:120]}{hint}"
    text = []
    for line in raw.splitlines():
        if line.startswith("data:") and "output_text.delta" in line:
            try:
                text.append(json.loads(line[5:].strip()).get("delta", ""))
            except json.JSONDecodeError:
                pass
    return True, status, dt, f"reply={''.join(text)!r}"


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", default="http://localhost:4242")
    p.add_argument("--key", help="gateway key (default: LITELLM_MASTER_KEY from .env / env)")
    p.add_argument("--only", nargs="+", help="test only these model names")
    p.add_argument("--skip-subscription", action="store_true", help="skip OAuth-forwarded models")
    p.add_argument("--skip-passthrough", action="store_true", help="skip /codex/responses probe")
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args()

    key = args.key or os.environ.get("LITELLM_MASTER_KEY") or load_dotenv().get("LITELLM_MASTER_KEY")
    if not key:
        sys.exit("No gateway key: pass --key or put LITELLM_MASTER_KEY in .env")

    # 1. discover models
    status, raw, _ = request(f"{args.gateway}/v1/models", {"Authorization": f"Bearer {key}"})
    if status != 200:
        sys.exit(f"Gateway not reachable at {args.gateway} (HTTP {status}).\n"
                 f"Start it:  cd {DIR} && .venv-litellm/bin/litellm --config litellm_config.yaml --port 4242")
    models = [m["id"] for m in json.loads(raw)["data"]]
    if args.only:
        models = [m for m in models if m in set(args.only)]

    claude_tok = claude_oauth_token()
    codex_tok = codex_access_token()
    print(f"Gateway {args.gateway} — {len(models)} models; "
          f"claude token: {'yes' if claude_tok else 'NO'}; codex token: {'yes' if codex_tok else 'NO'}\n")

    # 2. test each model with the right auth path
    results = []
    for model in models:
        if model == "claude-sub" or model == "codex-sub":       # subscription-forwarded
            # (exact match: claude-sub-auto is the server-side-token variant,
            #  tested below as a plain virtual-key model)
            if args.skip_subscription:
                results.append((model, "SKIP", 0, "subscription tests disabled"))
                continue
            if not claude_tok:
                results.append((model, "FAIL", 0, "no Claude OAuth token on this machine"))
                continue
            ok, status, dt, info = test_claude_subscription(args.gateway, key, model, claude_tok)
        else:                                                   # plain API-key model
            ok, status, dt, info = test_openai_chat(args.gateway, key, model)
        results.append((model, "PASS" if ok else "FAIL", dt, f"HTTP {status} — {info}"))

    # 3. passthrough probe (not part of /v1/models)
    if not args.skip_passthrough and codex_tok:
        ok, status, dt, info = test_codex_passthrough(args.gateway, key, codex_tok)
        results.append(("(passthrough) /codex/responses", "PASS" if ok else "FAIL", dt, f"HTTP {status} — {info}"))

    # 4. report
    w = max(len(r[0]) for r in results) + 2
    print(f"{'MODEL'.ljust(w)}STATUS  TIME     DETAIL")
    print("-" * 100)
    for model, verdict, dt, info in results:
        print(f"{model.ljust(w)}{verdict.ljust(8)}{dt:6.2f}s  {info}")

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
