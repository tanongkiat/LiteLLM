# HOWTO — Operating the LiteLLM Subscription Gateway

Task-by-task guide. All commands assume:

```bash
cd /Users/tanongkiatsookawat/Developers/LiteLLM
```

---

## 1. Start / stop everything

```bash
./gateway.sh start     # db first (health-gated), then gateway
./gateway.sh status    # are they up?
./gateway.sh logs      # follow gateway logs
./db.sh stop           # stop just the database
docker compose stop    # stop everything
```

Nothing auto-starts (`restart: no`). After a reboot: `./gateway.sh start`.
First boot after config changes: `docker compose up -d` (recreates containers).

Gateway: http://localhost:4242 — Admin UI: http://localhost:4242/ui
(login **admin** + `LITELLM_MASTER_KEY` from `.env`).

---

## 2. Onboard a developer (admin side)

```bash
./onboard_dev.sh alice              # $20/30d budget
./onboard_dev.sh bob 50 30d         # custom budget/duration
./onboard_dev.sh carol 20 30d --all # include claude-sub-auto (YOUR seat — avoid)
```

Prints the new virtual key and the exact command to hand to the dev. Default
model access: `claude-sub`, `elastic-chat`. Spend appears per key in the UI
(Models/Keys screens).

Revoke later: UI → Keys → delete, or
`curl -X POST localhost:4242/key/delete -H "Authorization: Bearer $MASTER" \
 -H "content-type: application/json" -d '{"keys":["sk-..."]}'`

## 3. Developer machine setup (each dev, once)

```bash
# with the key the admin gave them:
GATEWAY=https://gw.example.com KEY=sk-... ./setup_claude_code_client.sh --write-zshrc --test
source ~/.zshrc

claude    # log in with their OWN Team/Max seat, then use claude normally
```

What it does: writes `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL=claude-sub`,
`ANTHROPIC_CUSTOM_HEADERS` (the virtual key) as a marked block in `~/.zshrc`,
and validates the key against the gateway. Their seat token stays in their
keychain, is forwarded per-request, and refreshes whenever they use `claude` —
nothing is ever uploaded to the server.

## 4. Use Claude from an IDE (ZCode, Cursor, …) — `claude-sub-auto`

For IDEs that only accept base-URL + API key:

- Base URL: `http://localhost:4242/v1` (or your HTTPS gateway)
- API key: your virtual key (`LITELLM_VIRTUAL_KEY` in `.env`)
- Model: `claude-sub-auto`

The gateway supplies the OAuth token (`.env`) and injects the Claude Code
system prompt (Anthropic requires it — see Troubleshooting).

**Token expiry:** the seat token lasts ~8h. Refresh with:

```bash
./refresh_claude_token.sh    # pulls fresh token from keychain, recreates gateway
```

Run it whenever `claude-sub-auto` starts returning 401/429 and you've used
`claude` since the last refresh. Set-and-forget alternative:
`claude setup-token` once → paste the long-lived token into `.env` as
`CLAUDE_OAUTH_TOKEN`.

This model is **single-seat** — it's your seat. Dev keys are blocked from it.

## 5. Use the ChatGPT subscription (scripts / tools you control)

```bash
curl -N localhost:4242/codex/responses \
  -H "x-litellm-api-key: Bearer $LITELLM_VIRTUAL_KEY" \
  -H "Authorization: Bearer $(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.codex/auth.json")))["tokens"]["access_token"])')" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-5.5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"store":false,"stream":true}'
```

The Codex token (`~/.codex/auth.json`, refreshed by `codex login`) is passed
through verbatim. Note: **Codex CLI itself cannot be proxied** — in
subscription mode it ignores `OPENAI_BASE_URL` and talks to chatgpt.com
directly.

## 6. Add an API-key model

Append to `model_list` in `litellm_config.yaml` (keep it OUT of
`forward_client_headers_to_llm_api` — client OAuth headers must never reach
pooled providers):

```yaml
  - model_name: my-model
    litellm_params:
      model: openai/<upstream-model>
      api_base: https://...        # if not the official API
      api_key: os.environ/MY_KEY
```

Add `MY_KEY` to `.env`, then `docker compose up -d` and verify with
`python3 test_all_models.py`.

For `claude-api` (pooled Anthropic, per-token billing): the entry is already
in the config, commented out — uncomment it and add `ANTHROPIC_API_KEY` to
`.env`.

## 7. Run the tests

```bash
python3 test_all_models.py                     # every model, right auth path (smoke test)
python3 test_claude_sub_litellm.py             # claude-sub via gateway + virtual key
python3 test_claude_subscription.py            # direct: token → api.anthropic.com
python3 test_chatgpt_subscription.py           # direct: token → chatgpt.com backend
```

`test_all_models.py` exits non-zero on any failure — safe for CI. Each run
spends a few tokens per model. Re-run after every LiteLLM upgrade.

## 8. Deploy to a server (team launch)

1. Copy this folder to the server (`.env` included — set a **fresh**
   `LITELLM_MASTER_KEY` there; chmod 600).
2. Put HTTPS in front — tokens ride in headers. Caddy example:
   ```
   gw.example.com {
       reverse_proxy localhost:4242
   }
   ```
   (certificates automatic; open only 443, keep 4242 on localhost.)
3. `./gateway.sh start`.
4. Onboard devs (HOWTO 2) — they run the client setup with
   `GATEWAY=https://gw.example.com`.
5. IDE users → pooled models (`elastic-chat`, `claude-api`). Subscription
   forwarding stays for Claude Code users.

DB data lives in the `litellm_pgdata` docker volume — back that up and you've
backed up keys, budgets, and logs.

---

## Troubleshooting

**Claude subscription call returns 429 with message "Error"** (through
gateway or direct): Anthropic's client-shape check. The request must carry
`anthropic-beta: claude-code-20250219,oauth-2025-04-20` AND the Claude Code
system prompt as the **first** system block ("You are Claude Code, Anthropic's
official CLI for Claude."). Real Claude Code sends both automatically;
`claude-sub-auto` gets the prompt injected. Reproduce with
`test_claude_subscription.py --no-cc-system`.

**`claude-sub-auto` returns 401/429:** seat token expired — run
`./refresh_claude_token.sh` (HOWTO 4).

**Models list is empty after starting the container:** the `main-stable`
image's default CMD has no `--config`; the compose file overrides it
(`--config /app/config.yaml`). If you bypass compose, pass the flag yourself.

**Custom provider fails to boot with `KeyError: 'custom_handler'`:** in
`custom_provider_map` the key is `custom_handler`, not `class`
(LiteLLM ≥ 1.103).

**Callbacks fail with "neither a CustomLogger instance nor a callable":**
callback classes must subclass `litellm.integrations.custom_logger.CustomLogger`
(see `cc_prompt_inject.py`).

**`/codex/responses` answers `no_db_connection`:** passthrough routes resolve
virtual keys in the DB — use the master key, or a virtual key with the db up.

**Anthropic 400 "use the top-level 'system' parameter":** a `role:"system"`
message was sent inside a native Anthropic request. `cc_prompt_inject.py`
injects into the top-level `system` field for `anthropic_messages` calls and
as a system message for OpenAI-compat calls — if you see this, the callback
is outdated: restart the gateway (`docker compose restart gateway`) so the
bind-mounted file is re-imported.

**IDE says "invalid API key" but the key is correct:** the client sends
`Authorization: <key>` **without** the `Bearer ` prefix — LiteLLM 401s that.
Accepted shapes: `Authorization: Bearer <key>`, `x-api-key: <key>`,
`api-key: <key>`. Fix the IDE's provider settings (use its OpenAI-compatible
or Anthropic provider template, which send proper headers), and make sure the
key has no quotes/spaces/"Bearer" typed into the field. Verify a key from a
terminal: `curl -H "Authorization: Bearer <key>" localhost:4242/v1/models`.

**Changed a bind-mounted file (config, handlers, callbacks) but nothing
happened:** the running process keeps the old module/config in memory —
`docker compose restart gateway` (or `up -d` to recreate).

**IDE works with OpenAI-compat but not Anthropic mode (or vice versa):** both
are supported for `claude-sub-auto` — OpenAI-compat
(`/v1/chat/completions`, `Authorization: Bearer`) and Anthropic-native
(`/v1/messages`, `x-api-key`); the CC prompt is injected correctly per path.

**ChatGPT passthrough 401 "Could not parse your authentication token":** the
request reached chatgpt.com without the real token (e.g. a plain model entry
instead of the passthrough route, or an expired `~/.codex/auth.json` →
`codex login`).

**Gateway key rejected (400):** wrong key, or you hit a passthrough route
without the db running (see above).
