#!/usr/bin/env bash
# The two-session demo: long-term memory outlives the session that created it (spec 7, P27).
#
#   REGION=us-west-2 ARN=... CLIENT_ID=... MEMORY_ID=... DEMO_PASSWORD=... [A2A_URL=...] bash scripts/demo.sh
#
# Session A states a preference and is thrown away. AgentCore Memory extracts it asynchronously (minutes,
# not milliseconds), so the script polls the record namespace before session B -- a brand new session id
# for the same actor -- is asked to recall it. Set TRANSPORT=boto3 for an IAM-authorized runtime.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${REGION:=us-west-2}"
: "${TRANSPORT:=https}"
: "${ARN:?set ARN to the OpsInvestigator runtime ARN (agentcore status --json)}"
: "${CLIENT_ID:?set CLIENT_ID to the Cognito app client id (auth_bootstrap.py prints it)}"
: "${MEMORY_ID:?set MEMORY_ID to the OpsInvestigatorMemory id (agentcore status --json)}"
: "${DEMO_PASSWORD:?set DEMO_PASSWORD to the password of the demo user}"

STDERR=$(mktemp)
trap 'rm -f "$STDERR"' EXIT

TOKEN=$(uv run python scripts/get_token.py --region "$REGION" --client-id "$CLIENT_ID")
ACTOR=$(uv run python - "$TOKEN" <<'PY'
import base64
import json
import sys

payload = sys.argv[1].split(".")[1]
print(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"])
PY
)
echo "== actor $ACTOR (the JWT sub; memory is namespaced by it)"

echo "== Session A"
uv run python scripts/invoke.py --transport "$TRANSPORT" --arn "$ARN" --region "$REGION" --token "$TOKEN" \
  --prompt "Remember this preference: I own the prod account in $REGION and NAT Gateway cost is my top concern. Then tell me which AgentCore runtimes exist here." \
  2>"$STDERR"
SESSION_A=$(sed -n 's/^session_id=//p' "$STDERR")
echo "== session A was $SESSION_A"

echo "== waiting for AgentCore Memory extraction (asynchronous; up to 180s)"
for _ in $(seq 1 36); do
  FOUND=$(uv run python - "$REGION" "$MEMORY_ID" "/preferences/$ACTOR/" <<'PY'
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

region, memory_id, namespace = sys.argv[1:4]
client = boto3.client("bedrock-agentcore", region_name=region)
try:
    records = client.retrieve_memory_records(
        memoryId=memory_id, namespace=namespace, searchCriteria={"searchQuery": "top cost concern", "topK": 3}
    ).get("memoryRecordSummaries", [])
except (BotoCoreError, ClientError) as e:  # the namespace does not exist until the first record lands
    print(0)
    print(f"{type(e).__name__}: {e}", file=sys.stderr)
    raise SystemExit(0)
for record in records:
    print(str(record.get("content", ""))[:200], file=sys.stderr)
print(len(records))
PY
)
  [ "$FOUND" != "0" ] && { echo "== memory records in /preferences/$ACTOR/: $FOUND"; break; }
  sleep 5
done

echo "== Session B (new session id, same actor)"
uv run python scripts/invoke.py --transport "$TRANSPORT" --arn "$ARN" --region "$REGION" --token "$TOKEN" \
  --prompt "What did I say my top cost concern was? Then show NAT Gateway cost for the last 7 days."

if [ -n "${A2A_URL:-}" ]; then
  echo "== A2A card"
  uv run python scripts/a2a_client.py --url "$A2A_URL" --token "$TOKEN"
fi
