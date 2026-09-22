#!/usr/bin/env bash
# Owner-run end-to-end check in a real account (spec 7, 10). Costs a few dollars and deploys infrastructure.
#
#   AWS_PROFILE=<your-profile> DEMO_PASSWORD='...' bash scripts/smoke.sh
#
# Order matters: the A2A card has to advertise a public URL that only exists after the first deploy, so the
# runtime is deployed, its URL is written back into agentcore.json, and it is deployed a second time. Every
# agentcore CLI call runs from the repo root through npx -- a deprecated Python `agentcore` binary shadows
# the npm one on some machines.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${REGION:=us-west-2}"
: "${DEMO_PASSWORD:?set DEMO_PASSWORD to the password of the demo user}"
# Both, for every child process: botocore reads only AWS_DEFAULT_REGION, so a client built without an
# explicit region_name would use the region from ~/.aws/config rather than the one being smoked.
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
AGENTCORE="npx -y @aws/agentcore@0.30.0"

# doctor exits 1 on any WARN and 2 on any FAIL; only a FAIL is worth stopping a paid smoke for. Run it
# directly, not through `make doctor`: make collapses every recipe failure to its own exit 2, so a WARN
# would look exactly like a FAIL here.
uv run python doctor.py --region "$REGION" && DOCTOR=0 || DOCTOR=$?
[ "$DOCTOR" -ge 2 ] && { echo "doctor reported a FAIL; fix it before spending money here"; exit "$DOCTOR"; }

make auth-bootstrap
make deploy

STATUS=$($AGENTCORE status --json)
eval "$(uv run python - "$STATUS" <<'PY'
"""Print shell assignments for the deployed ids.

`agentcore status --json` returns {"resources": [{"resourceType", "name", "deploymentState", ...}]}, and
before the first deploy every entry is "local-only" with no ARN at all -- which field carries the ARN of
a *deployed* runtime is confirmed in Task 15. So: look for an obvious key on the named resource first,
then fall back to scanning the whole document for an ARN that belongs to that runtime.
"""
import json
import re
import shlex
import sys

ARN = re.compile(r"arn:aws[\w-]*:bedrock-agentcore:[^\s\"']+")


def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node


def resource(status, name):
    for item in status.get("resources") or status.get("runtimes") or []:
        if item.get("name") == name:
            return item
    return {}


def field(status, name, *keys):
    item = resource(status, name)
    return next((str(item[key]) for key in keys if item.get(key)), "")


def runtime_arn(status, name):
    found = field(status, name, "arn", "agentRuntimeArn", "runtimeArn")
    if found:
        return found
    owned = [s for s in strings(status) if ARN.fullmatch(s) and (f"/{name}-" in s or s.endswith(f"/{name}"))]
    return owned[0] if owned else ""


def memory_id(status, name):
    found = field(status, name, "id", "memoryId", "arn")
    if found:
        return found
    return next((s for s in strings(status) if s.startswith(f"{name}-")), "")


status = json.loads(sys.argv[1])
print(f"export ARN={shlex.quote(runtime_arn(status, 'OpsInvestigator'))}")
print(f"export A2A_ARN={shlex.quote(runtime_arn(status, 'OpsInvestigatorA2A'))}")
print(f"export MEMORY_ID={shlex.quote(memory_id(status, 'OpsInvestigatorMemory'))}")
print(f"# status keys: {sorted(status)}", file=sys.stderr)
PY
)"
: "${ARN:?OpsInvestigator has no ARN in agentcore status --json}"
: "${A2A_ARN:?OpsInvestigatorA2A has no ARN in agentcore status --json}"
: "${MEMORY_ID:?no memory id in agentcore status --json}"

CLIENT_ID=$(uv run python -c "
import json
print(json.load(open('agentcore/agentcore.json'))['runtimes'][0]['authorizerConfiguration']['customJwtAuthorizer']['allowedClients'][0])
")
export CLIENT_ID MEMORY_ID REGION
A2A_URL=$(uv run python -c "
import sys, urllib.parse
print('https://bedrock-agentcore.%s.amazonaws.com/runtimes/%s/invocations?qualifier=DEFAULT' % (sys.argv[1], urllib.parse.quote(sys.argv[2], safe='')))
" "$REGION" "$A2A_ARN")
export A2A_URL
echo "== runtime $ARN"
echo "== a2a     $A2A_URL"
echo "== memory  $MEMORY_ID"

echo "== second deploy so the A2A card knows its own public URL"
uv run python - "$A2A_URL" <<'PY'
import json
import sys
from pathlib import Path

path = Path("agentcore/agentcore.json")
config = json.loads(path.read_text())
for runtime in config.get("runtimes", []):
    for var in runtime.get("envVars", []):
        if var.get("name") == "A2A_PUBLIC_URL":
            var["value"] = sys.argv[1]
path.write_text(json.dumps(config, indent=2) + "\n")
PY
make deploy

bash scripts/demo.sh

echo "== span check: one invoke_agent span per invoke (GenAI observability, spec 8)"
uv run python - "$REGION" <<'PY'
import sys
import time

import boto3

logs = boto3.client("logs", region_name=sys.argv[1])
now = int(time.time())
query = logs.start_query(
    logGroupName="aws/spans",
    startTime=now - 1800,
    endTime=now,
    queryString="fields name | filter name like /invoke_agent ops-investigator/ | stats count() as n",
)["queryId"]
for _ in range(20):
    result = logs.get_query_results(queryId=query)
    if result["status"] in ("Complete", "Failed", "Cancelled"):
        print(result["status"], result.get("results"))
        break
    time.sleep(3)
else:
    print("span query still running; check CloudWatch Logs Insights on aws/spans")
PY

echo "== done. Teardown with: make destroy   (left up for the demo by default)"
# This run wrote a real discovery URL, a real client id and a real A2A_PUBLIC_URL (which carries the
# account id) into agentcore/agentcore.json. --reset-placeholders puts all three back, but git is the
# one-step undo that also drops the reformatting.
echo "== before committing: git checkout agentcore/agentcore.json"
echo "   (or, to keep other edits: uv run python scripts/auth_bootstrap.py --reset-placeholders)"
