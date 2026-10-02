# LiteLLM Subscription Gateway

One gateway, three kinds of model access, per-developer identity throughout:

- **Claude subscription seats** — each developer's own Team/Max seat token is
  forwarded per-request to Anthropic (server stores nothing).
- **ChatGPT subscription** — per-user Codex token via a pass-through route.
- **API-key models** — pooled credentials (e.g. Elastic), normal per-token use.

Plus everything LiteLLM gives you on top: virtual keys, budgets, teams, spend
logs, and the Admin UI.

Verified end-to-end (LiteLLM `main-stable`, 2026-10-02):

```
CLAUDE  /v1/messages         → 200   per-user forwarding
CLAUDE  /v1/chat/completions → 200   server-side-token variant (claude-sub-auto)
CHATGPT /codex/responses     → 200   per-user passthrough
ELASTIC /v1/chat/completions → 200   custom provider
```

## Architecture

Every request carries the gateway identity; subscription models additionally
carry the user's own seat token:

| Header | Meaning |
|---|---|
| `x-litellm-api-key` (or `Authorization`) | virtual key → gateway auth, budgets, logging |
| `Authorization` (subscription models) | the dev's own OAuth token → forwarded upstream |

| Model | Upstream | Credential | Intended for |
|---|---|---|---|
| `claude-sub` | api.anthropic.com | each dev's own token, forwarded per-request | Claude Code users |
| `claude-sub-auto` | api.anthropic.com | one OAuth token in `.env` + injected CC prompt | IDEs that only take base-URL+key (single user!) |
| `/codex/responses` (passthrough) | chatgpt.com/backend-api/codex | each user's Codex token, forwarded | scripts / tools you control |
| `elastic-chat` | Elastic inference endpoint | `EC_APIKEY` (pooled) | anyone |
| `claude-api` (commented) | api.anthropic.com | `ANTHROPIC_API_KEY` (pooled) | IDE users, per-token billing |

Design rules learned the hard way (details in HOWTO → Troubleshooting):

- Anthropic serves subscription tokens **only** to Claude Code-shaped requests
  (CC beta header + CC system prompt first) — otherwise a cloaked 429.
- LiteLLM's header forwarding whitelist covers `Authorization` only for the
  Anthropic OAuth special case — hence the passthrough route for ChatGPT.
- Codex CLI ignores `OPENAI_BASE_URL` in subscription mode — it cannot be
  proxied per-user.

## Quickstart (docker compose)

Two containers — Postgres (`litellm-db`) + gateway (`litellm-gateway`) —
**both manual-start** (`restart: no`): nothing comes back after a reboot
until you start it.

```bash
cd /Users/tanongkiatsookawat/Developers/LiteLLM
./gateway.sh start        # db (health-gated) + gateway
./gateway.sh status | logs
docker compose stop       # everything off
```

- Gateway: http://localhost:4242
- Admin UI: http://localhost:4242/ui — username **admin**, password =
  `LITELLM_MASTER_KEY` from `.env`.

## Documentation

**[HOWTO.md](HOWTO.md)** — step-by-step guides: onboard a developer, wire up
Claude Code / ZCode, add models, refresh tokens, run tests, deploy to a
server, and troubleshoot everything that bit us once.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | The two-container stack (manual start) |
| `litellm_config.yaml` | Models, forward rules, passthrough, callbacks |
| `.env` | All secrets (chmod 600, gitignored) — see below |
| `db.sh` / `gateway.sh` | Manual start/stop/status/logs per container |
| `onboard_dev.sh` | ADMIN: mint per-dev virtual keys |
| `setup_claude_code_client.sh` | DEV: wire Claude Code to the gateway |
| `refresh_claude_token.sh` | Sync fresh Claude OAuth token into `.env` |
| `elastic_handler.py` | Custom provider for Elastic inference |
| `cc_prompt_inject.py` | Callback: CC system prompt for `claude-sub-auto` |
| `test_all_models.py` | Health-check every model on the gateway |
| `test_claude_sub_litellm.py` | claude-sub via gateway + virtual key |
| `test_claude_subscription.py` / `test_chatgpt_subscription.py` | Direct-to-provider auth verifiers |
| `.venv-litellm/` | Optional host-mode fallback (no docker) |

### `.env` contents

| Var | What it is |
|---|---|
| `LITELLM_MASTER_KEY` | Admin key (UI login, key minting) |
| `LITELLM_VIRTUAL_KEY` | Your personal virtual key |
| `EC_APIKEY` | Elastic inference API key (encoded form) |
| `CLAUDE_OAUTH_TOKEN` | Seat token for `claude-sub-auto` (~8h expiry — refresh!) |
| `POSTGRES_PASSWORD` | DB password (container-to-container) |
| `DATABASE_URL` | Host-mode fallback connection string |

## Security notes

- `.env` holds live credentials — keep it chmod 600 and out of git (already
  in `.gitignore`).
- Subscription tokens must only cross the network over **HTTPS**; plain HTTP
  is acceptable on localhost only.
- `claude-sub-auto` rides one seat — keep it personal, dev keys are blocked
  from it by default.
- Per-user forwarding keeps every request attributed to its own paid seat;
  never pool one seat across the team.
