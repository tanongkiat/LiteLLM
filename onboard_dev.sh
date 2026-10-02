#!/usr/bin/env bash
# ADMIN-side: mint a virtual key for one developer and print the client setup
# line to hand them. Run on the gateway host.
#
#   ./onboard_dev.sh alice                    # $20 / 30d budget, default models
#   ./onboard_dev.sh bob 50 30d               # custom budget
#   ./onboard_dev.sh carol 20 30d --all       # access to every model incl. claude-sub-auto
#
# Default model access: claude-sub (their own seat, per-user forwarding) and
# elastic-chat (pooled). claude-sub-auto is EXCLUDED by default — it rides the
# admin's single seat and should not be shared with the team.
set -e
cd "$(dirname "$0")"

NAME="${1:?usage: onboard_dev.sh <name> [max_budget] [budget_duration] [--all]}"
BUDGET="${2:-20}"
DURATION="${3:-30d}"
MODELS='["claude-sub", "elastic-chat"]'
if [[ "${4:-}" == "--all" ]]; then MODELS=""; fi

MASTER=$(grep ^LITELLM_MASTER_KEY .env | cut -d= -f2)
PAYLOAD=$(python3 - "$NAME" "$BUDGET" "$DURATION" "$MODELS" <<'EOF'
import json, sys
name, budget, duration, models = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
p = {"key_alias": name, "max_budget": float(budget), "budget_duration": duration}
if models:
    p["models"] = json.loads(models)
print(json.dumps(p))
EOF
)

RESP=$(curl -s -X POST http://localhost:4242/key/generate \
  -H "Authorization: Bearer $MASTER" -H "content-type: application/json" -d "$PAYLOAD")

KEY=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key",""))')
if [[ -z "$KEY" ]]; then echo "FAILED: $RESP"; exit 1; fi

echo "virtual key for '$NAME' (budget \$$BUDGET / $DURATION):"
echo "  $KEY"
echo
echo "hand this to $NAME — they run:"
echo "  GATEWAY=http://<gateway-host>:4242 KEY=$KEY ./setup_claude_code_client.sh"
