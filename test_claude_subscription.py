#!/usr/bin/env python3
"""
Test Claude subscription (Claude Code OAuth) auth directly against api.anthropic.com.

This replicates what LiteLLM's `forward_client_headers_to_llm_api: true` does:
forwards the user's Claude Code OAuth token as `Authorization: Bearer <token>`
(instead of the usual `x-api-key`).

Key finding (verified 2026-10-02): Anthropic only serves subscription tokens to
Claude Code-shaped requests. Without the `claude-code-20250219` beta AND the
Claude Code system prompt, /v1/messages returns a cloaked rate_limit_error
(429 "Error") even though the token itself is valid. When the client IS Claude
Code (as via LiteLLM forwarding), it sends these automatically.

Token discovery order:
  1. --token argument
  2. CLAUDE_CODE_OAUTH_TOKEN env var (long-lived token from `claude setup-token`)
  3. ~/.claude/.credentials.json        (Linux / some setups)
  4. macOS Keychain, service "Claude Code-credentials"

Usage:
  python3 test_claude_subscription.py                     # models probe + tiny messages call
  python3 test_claude_subscription.py --stream            # streaming, mimics Claude Code
  python3 test_claude_subscription.py --no-cc-system      # shows the cloaked 429
  python3 test_claude_subscription.py --model claude-sonnet-4-5

Note: the first keychain read may pop a macOS permission dialog -> click Allow.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")

CC_BETA = "claude-code-20250219"
OAUTH_BETA = "oauth-2025-04-20"
BETAS = f"{CC_BETA},{OAUTH_BETA}"
CC_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def mask(token: str) -> str:
    if len(token) <= 20:
        return token[:4] + "…"
    return f"{token[:14]}…{token[-4:]} (len={len(token)})"


def _oauth_from_keychain_blob() -> dict | None:
    try:
        blob = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as e:
        print(f"    keychain read failed: {e}")
        return None
    if not blob:
        return None
    return json.loads(blob).get("claudeAiOauth", {})


def token_from_cred_file_meta():
    if not os.path.exists(CRED_FILE):
        return None, None
    with open(CRED_FILE) as f:
        oauth = json.load(f).get("claudeAiOauth", {})
    return oauth.get("accessToken"), {k: oauth.get(k) for k in ("expiresAt", "scopes")}


def token_from_keychain_meta():
    oauth = _oauth_from_keychain_blob()
    if not oauth:
        return None, None
    return oauth.get("accessToken"), {k: oauth.get(k) for k in ("expiresAt", "scopes")}


def find_token(explicit: str | None):
    """Return (token, source, meta) where meta may hold expiresAt/scopes."""
    sources = []
    if explicit:
        sources.append(("--token argument", lambda: (explicit, {})))
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        sources.append(("env CLAUDE_CODE_OAUTH_TOKEN", lambda: (env, {})))
    sources.append((f"file {CRED_FILE}", token_from_cred_file_meta))
    sources.append((f"macOS Keychain ({KEYCHAIN_SERVICE})", token_from_keychain_meta))

    for name, fn in sources:
        try:
            token, meta = fn()
        except Exception as e:
            print(f"  - {name}: error {e}")
            continue
        if token:
            return token, name, meta or {}
        print(f"  - {name}: not found")
    return None, None, {}


def check_expiry(meta: dict):
    expires_at = meta.get("expiresAt")
    if not expires_at:
        return
    remaining_s = (expires_at - time.time() * 1000) / 1000
    if remaining_s <= 0:
        print(f"  !! TOKEN EXPIRED {abs(remaining_s)/3600:.1f}h ago — run `claude` once "
              f"(or `claude /login`) to refresh, or use a long-lived `claude setup-token`.")
    elif remaining_s < 3600:
        print(f"  !! token expires in {remaining_s/60:.0f} min — refresh soon.")
    else:
        print(f"  token expires in {remaining_s/3600:.1f}h")


def oauth_headers(token: str, ua: str) -> dict:
    # The exact header set LiteLLM forwards for subscription auth:
    # Bearer token in Authorization (NOT x-api-key) + OAuth/CC beta headers.
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": BETAS,
        "content-type": "application/json",
        "user-agent": ua,
    }


def request(method: str, path: str, token: str, ua: str, payload: dict | None = None, timeout: int = 120):
    """Returns (status, body_bytes, elapsed_s). The body must be read inside the with-block."""
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(payload).encode() if payload else None,
        headers=oauth_headers(token, ua),
        method=method,
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t0


def parse_body(body: bytes):
    """Try JSON; return (data, raw_text)."""
    text = body.decode(errors="replace")
    try:
        return json.loads(body), text
    except json.JSONDecodeError:
        return None, text[:300]


def print_http_error(status: int, body: bytes):
    data, raw = parse_body(body)
    msg = data.get("error", {}).get("message", raw[:300]) if isinstance(data, dict) else raw[:300]
    print(f"  HTTP {status}: {msg}")


def test_models(token: str, ua: str):
    print("\n[2/3] GET /v1/models (proves auth without consuming tokens)")
    status, body, dt = request("GET", "/v1/models?limit=20", token, ua)
    if status == 200:
        data, raw = parse_body(body)
        if data is None:
            # Observed: endpoint answers 200 with an empty body for OAuth tokens.
            print(f"  200 OK in {dt:.2f}s — auth accepted (empty body: {raw!r})")
            return []
        models = [m["id"] for m in data.get("data", [])]
        print(f"  200 OK in {dt:.2f}s — {len(models)} models available to this token:")
        for m in models:
            print(f"    - {m}")
        return models
    if status == 401:
        print("  401 — /v1/models may not accept subscription tokens; not fatal, trying /v1/messages.")
        return []
    print_http_error(status, body)
    return []


def test_messages(token: str, ua: str, model: str, prompt: str, max_tokens: int, stream: bool, cc_system: bool):
    print(f"\n[3/3] POST /v1/messages (model={model}, max_tokens={max_tokens}"
          f"{', stream' if stream else ''}{', CC system prompt' if cc_system else ', NO CC system prompt'})")
    payload = {"model": model, "max_tokens": max_tokens, "stream": stream,
               "messages": [{"role": "user", "content": prompt}]}
    if cc_system:
        payload["system"] = [{"type": "text", "text": CC_SYSTEM}]
    status, body, dt = request("POST", "/v1/messages", token, ua, payload)
    if status == 200:
        if stream:
            print(f"  200 OK (streaming, {dt:.2f}s):")
            for line in body.decode(errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                evt = json.loads(data)
                if evt.get("type") == "content_block_delta":
                    print("    " + evt["delta"].get("text", ""), end="", flush=True)
                elif evt.get("type") == "message_delta" and evt["delta"].get("stop_reason"):
                    print(f"\n  stop_reason={evt['delta']['stop_reason']}")
            print()
        else:
            data, raw = parse_body(body)
            if data is None:
                print(f"  200 OK in {dt:.2f}s — but body is not JSON: {raw!r}")
                print("\nRESULT: FAIL")
                return False
            text = "".join(b.get("text", "") for b in data.get("content", []))
            print(f"  200 OK in {dt:.2f}s — usage={data.get('usage')}")
            print(f"  reply: {text!r}")
        print("\nRESULT: PASS — subscription OAuth auth works end-to-end.")
        return True
    print_http_error(status, body)
    hints = {
        401: "token rejected: expired/revoked → open `claude` and /login again, or `claude setup-token`.",
        403: "token lacks inference scope (needs user:inference).",
        404: f"unknown model '{model}' — pick one from the [2/3] list above.",
        429: "either plan rate limit, OR the request isn't Claude Code-shaped — the CC system prompt is required (remove --no-cc-system).",
    }
    if status in hints:
        print(f"  hint: {hints[status]}")
    print("\nRESULT: FAIL")
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token", help="explicit OAuth token (default: env/file/keychain)")
    p.add_argument("--model", default="claude-sonnet-4-5", help="model for the messages test")
    p.add_argument("--prompt", default="Reply with exactly: SUBSCRIPTION OK")
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--stream", action="store_true", help="stream the response (mimics Claude Code)")
    p.add_argument("--skip-models", action="store_true")
    p.add_argument("--no-cc-system", action="store_true",
                   help="omit the Claude Code system prompt — Anthropic then rejects "
                        "subscription tokens with a cloaked 429 (useful to see the difference)")
    p.add_argument("--user-agent", default="claude-cli/2.1.273 (external, cli)",
                   help="UA sent upstream; Anthropic may expect a claude-cli UA for subscription auth")
    args = p.parse_args()

    print("[1/3] Locating Claude Code subscription OAuth token")
    token, source, meta = find_token(args.token)
    if not token:
        sys.exit("No OAuth token found. Log in via `claude` first, or pass --token.")
    print(f"  source : {source}")
    print(f"  token  : {mask(token)}")
    if meta.get("scopes"):
        print(f"  scopes : {meta['scopes']}")
    check_expiry(meta)

    if not args.skip_models:
        test_models(token, args.user_agent)
    ok = test_messages(token, args.user_agent, args.model, args.prompt, args.max_tokens,
                       args.stream, cc_system=not args.no_cc_system)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
