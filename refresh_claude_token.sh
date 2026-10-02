#!/usr/bin/env bash
# Refresh CLAUDE_OAUTH_TOKEN in .env from the macOS keychain and restart the
# gateway, so claude-sub-auto keeps working.
#
# The keychain token is refreshed by the `claude` CLI whenever you use it, so
# run this if claude-sub-auto starts returning 401/429 and you've used `claude`
# since the last refresh.
#
# Alternative (set-and-forget): run `claude setup-token` once, and paste the
# long-lived token into .env as CLAUDE_OAUTH_TOKEN — no periodic refresh needed.
set -e
cd "$(dirname "$0")"

TOKEN=$(security find-generic-password -s "Claude Code-credentials" -w | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])')

python3 - "$TOKEN" <<'EOF'
import sys, os
lines = [l for l in open(".env").read().splitlines() if not l.startswith("CLAUDE_OAUTH_TOKEN=")]
lines.append(f"CLAUDE_OAUTH_TOKEN={sys.argv[1]}")
open(".env", "w").write("\n".join(lines) + "\n")
os.chmod(".env", 0o600)
EOF

docker compose up -d gateway   # recreate picks up the new env value
echo "CLAUDE_OAUTH_TOKEN refreshed; gateway recreated."
