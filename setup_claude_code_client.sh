#!/usr/bin/env bash
# DEV-side: wire Claude Code to the team gateway. Each dev runs this on THEIR
# machine, with the virtual key the admin minted for them.
#
#   GATEWAY=https://gw.example.com KEY=sk-... ./setup_claude_code_client.sh
#   ... --write-zshrc    # also append the exports to ~/.zshrc (marked block)
#   ... --test           # verify the key works against the gateway
#
# The dev then logs in with their OWN Claude seat (`claude` → subscription
# login). Their OAuth token is forwarded per-request by the gateway — it is
# never stored server-side. HTTP gateways only for localhost; a shared server
# must be HTTPS.
set -e
cd "$(dirname "$0")"

GATEWAY="${GATEWAY:?set GATEWAY=http(s)://host:port}"
KEY="${KEY:?set KEY=<your virtual key>}"
ACTION="${1:-}"

EXPORTS="export ANTHROPIC_BASE_URL=$GATEWAY
export ANTHROPIC_MODEL=claude-sub
export ANTHROPIC_CUSTOM_HEADERS=\"x-litellm-api-key: Bearer $KEY\""

echo "# add to your shell profile (or run in each terminal):"
echo "$EXPORTS"
echo

if [[ "$ACTION" == "--write-zshrc" ]]; then
  python3 - "$HOME/.zshrc" <<'EOF'
import os, sys
path = sys.argv[1]
marker = "# litellm-gateway"
lines = open(path).read().splitlines() if os.path.exists(path) else []
out, skipping = [], False
for l in lines:
    s = l.strip()
    if s == marker + " start":
        skipping = True
    elif s == marker + " end":
        skipping = False
    elif not skipping:
        out.append(l)
open(path, "w").write("\n".join(out) + "\n")
EOF
  printf '# litellm-gateway start\n%s\n# litellm-gateway end\n' "$EXPORTS" >> ~/.zshrc
  echo "written to ~/.zshrc (marked block) — run: source ~/.zshrc"
fi

if [[ "$ACTION" == "--test" || "$ACTION" == "--write-zshrc" ]]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" "$GATEWAY/v1/models")
  echo "key check: GET $GATEWAY/v1/models -> HTTP $CODE (200 = key valid)"
  [[ "$CODE" == "200" ]] || { echo "key rejected — check with the gateway admin"; exit 1; }
  echo "next: run 'claude' and log in with your subscription account, then just use claude normally."
fi
