#!/usr/bin/env python3
"""
Test the claude-sub model THROUGH the LiteLLM gateway using a virtual key
(LITELLM_VIRTUAL_KEY in .env, or --key). The user's Claude subscription OAuth
token is attached automatically (keychain / credentials.json / env).

Tests:
  [1] key check        — GET /v1/models with the virtual key
  [2] native path      — POST /v1/messages with dual auth (this is what
                         Claude Code does: gateway key + own OAuth token)
  [3] openai-compat    — POST /v1/chat/completions with dual auth (what
                         OpenAI-SDK-style clients would do)
  [4] plain-SDK control— POST /v1/chat/completions with ONLY the virtual key
                         in Authorization (what an IDE like ZCode/Cursor sends
                         when you just give it base URL + API key).
                         EXPECTED TO FAIL — claude-sub needs the per-user
                         OAuth token; this documents why.

Usage:
  python3 test_claude_sub_litellm.py
  python3 test_claude_sub_litellm.py --stream
  python3 test_claude_sub_litellm.py --key sk-...
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
PROMPT = "Reply with exactly: LITELLM CLAUDE OK"


def load_dotenv():
    env = {}
    path = os.path.join(DIR, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def claude_oauth_token():
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


def call(url, headers, payload, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json", **headers}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, r.read().decode(errors="replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return False, e.code, e.read().decode(errors="replace")[:200], time.time() - t0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", default="http://localhost:4242")
    p.add_argument("--key", help="virtual key (default: LITELLM_VIRTUAL_KEY from .env)")
    p.add_argument("--stream", action="store_true")
    args = p.parse_args()

    env = load_dotenv()
    vk = args.key or os.environ.get("LITELLM_VIRTUAL_KEY") or env.get("LITELLM_VIRTUAL_KEY")
    if not vk:
        sys.exit("No virtual key: pass --key or set LITELLM_VIRTUAL_KEY in .env")
    oauth = claude_oauth_token()
    print(f"gateway http://localhost:4242   virtual key: {vk[:8]}…   oauth token: {'yes' if oauth else 'NO'}\n")

    dual = {"x-litellm-api-key": f"Bearer {vk}",     # gateway identity
            "Authorization": f"Bearer {oauth}",       # per-user seat, forwarded upstream
            "anthropic-beta": CC_BETAS}

    # [1] key check
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{args.gateway}/v1/models", headers={"Authorization": f"Bearer {vk}"}), timeout=30) as r:
            models = [m["id"] for m in json.loads(r.read())["data"]]
            print(f"[1] GET  /v1/models          : {r.status} — key can access: {models}")
    except urllib.error.HTTPError as e:
        print(f"[1] GET  /v1/models          : HTTP {e.code} — {e.read().decode(errors='replace')[:100]}")

    # [2] native /v1/messages
    payload = {"model": "claude-sub", "max_tokens": 32,
               "system": [{"type": "text", "text": CC_SYSTEM}],
               "messages": [{"role": "user", "content": PROMPT}]}
    ok, status, raw, dt = call(f"{args.gateway}/v1/messages", dual, payload)
    reply = "".join(b.get("text", "") for b in json.loads(raw).get("content", [])) if ok else ""
    print(f"[2] POST /v1/messages        : HTTP {status} in {dt:.2f}s — {reply!r}" if ok
          else f"[2] POST /v1/messages        : HTTP {status} in {dt:.2f}s — {raw[:110]}")

    # [3] openai-compat /v1/chat/completions
    payload = {"model": "claude-sub", "max_tokens": 32, "stream": args.stream,
               "messages": [{"role": "system", "content": CC_SYSTEM},
                            {"role": "user", "content": PROMPT}]}
    ok, status, raw, dt = call(f"{args.gateway}/v1/chat/completions", dual, payload)
    if ok and args.stream:
        text = "".join(json.loads(l[5:]).get("choices", [{}])[0].get("delta", {}).get("content", "") or ""
                       for l in raw.splitlines() if l.startswith("data:") and l[5:].strip() != "[DONE]")
        print(f"[3] POST /v1/chat/completions: HTTP {status} in {dt:.2f}s (stream) — {text!r}")
    elif ok:
        d = json.loads(raw)
        print(f"[3] POST /v1/chat/completions: HTTP {status} in {dt:.2f}s — "
              f"{d['choices'][0]['message'].get('content','')!r}")
    else:
        print(f"[3] POST /v1/chat/completions: HTTP {status} in {dt:.2f}s — {raw[:110]}")

    # [4] plain-SDK control (single Authorization header, no OAuth) — expected to FAIL
    only_vk = {"Authorization": f"Bearer {vk}"}
    payload = {"model": "claude-sub", "max_tokens": 32,
               "messages": [{"role": "system", "content": CC_SYSTEM},
                            {"role": "user", "content": PROMPT}]}
    ok, status, raw, dt = call(f"{args.gateway}/v1/chat/completions", only_vk, payload)
    verdict = "fails as expected (no OAuth token upstream)" if not ok else "unexpectedly PASSED"
    print(f"[4] plain-SDK control        : HTTP {status} in {dt:.2f}s — {verdict}")

    print("\nNOTE [4] is what an IDE that only takes base-URL + API-key (e.g. ZCode) sends;")
    print("claude-sub needs the per-user OAuth token, hence the dual-header setup or a")
    print("server-side-token model entry. See README.")


if __name__ == "__main__":
    main()
