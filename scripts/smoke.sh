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
# The `${ARN:?}` guards below only fail loudly if the parser is the only thing that can set these. An
# ARN left over in the caller's environment from an earlier run would sail past them and be deployed
# against silently, which is the one failure mode worse than stopping.
unset ARN A2A_ARN MEMORY_ID
eval "$(uv run python - "$STATUS" <<'PY'
"""Print shell assignments for the deployed ids.

`agentcore status --json` (CLI 0.30.0, confirmed against a real deploy) returns:

    {"success": true, "projectName": "...", "targetName": "default", "targetRegion": "...",
     "resources": [{"resourceType": "agent" | "memory", "name": "OpsInvestigator",
                    "deploymentState": "deployed", "identifier": "<arn>", "detail": "READY",
                    "invocationUrl": "https://bedrock-agentcore.../invocations"}],
     "deployedState": {"targets": {"default": {"resources": {
         "runtimes": {"OpsInvestigator": {"runtimeId": "...", "runtimeArn": "arn:...", "roleArn": "arn:..."}},
         "memories": {"OpsInvestigatorMemory": {"memoryId": "...", "memoryArn": "arn:..."}},
         "stackName": "...", "deployHash": "..."}}}}}

`deployedState` is the map to read: it is keyed by the names in agentcore.json and carries the id and
the ARN as separate fields. `resources[]` is the display list, and is only a fallback here -- its
`identifier` is an ARN even for the memory (whose id is the ARN's last path segment), and before the
first deploy every entry is "local-only" with no identifier at all.
"""
import json
import shlex
import sys

status = json.loads(sys.argv[1])
targets = (status.get("deployedState") or {}).get("targets") or {}
target = targets.get(str(status.get("targetName") or "")) or next(iter(targets.values()), {})
deployed = target.get("resources") or {}


def listed(name):
    """The `resources[]` entry for `name`, matched exactly: "OpsInvestigator" is a prefix of
    "OpsInvestigatorA2A", so a substring match would hand back the wrong runtime."""
    return next((r for r in status.get("resources") or [] if r.get("name") == name), {})


def runtime_arn(name):
    entry = (deployed.get("runtimes") or {}).get(name) or {}
    return str(entry.get("runtimeArn") or listed(name).get("identifier") or "")


def memory_id(name):
    entry = (deployed.get("memories") or {}).get(name) or {}
    return str(entry.get("memoryId") or (listed(name).get("identifier") or "").rsplit("/", 1)[-1])


print(f"export ARN={shlex.quote(runtime_arn('OpsInvestigator'))}")
print(f"export A2A_ARN={shlex.quote(runtime_arn('OpsInvestigatorA2A'))}")
print(f"export MEMORY_ID={shlex.quote(memory_id('OpsInvestigatorMemory'))}")
for item in status.get("resources") or []:
    print(f"# {item.get('name')}: {item.get('deploymentState')} {item.get('detail', '')}", file=sys.stderr)
PY
)"
NOT_DEPLOYED="is empty in agentcore status --json; is the deploy above actually finished (deploymentState=deployed)?"
: "${ARN:?OpsInvestigator $NOT_DEPLOYED}"
: "${A2A_ARN:?OpsInvestigatorA2A $NOT_DEPLOYED}"
: "${MEMORY_ID:?OpsInvestigatorMemory $NOT_DEPLOYED}"

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
# Two sources on purpose. `aws/spans` is where CloudWatch Transaction Search indexes spans, and it is what
# the GenAI Observability console reads -- but enabling Transaction Search is not the same as it working:
# in this repo's first cloud run the group was still empty of this agent's spans an hour later, while
# X-Ray already had them. X-Ray answers straight away, so an empty span count is only news when X-Ray is
# empty too; that distinction is the difference between "not indexed yet" and "not instrumented" (P59).
uv run python - "$REGION" <<'PY'
import datetime
import sys
import time

import boto3

region = sys.argv[1]
logs = boto3.client("logs", region_name=region)
now = int(time.time())
spans = None
try:
    query = logs.start_query(
        logGroupName="aws/spans",
        startTime=now - 1800,
        endTime=now,
        queryString="fields name | filter name like /invoke_agent ops-investigator/ | stats count() as n",
    )["queryId"]
except logs.exceptions.ResourceNotFoundException:
    # The group is created by Transaction Search on first delivery, so on a fresh account it does not
    # exist yet. That is the same state P59 describes, not a reason to fail a smoke that has already paid
    # for a deploy and a demo.
    print("aws/spans does not exist yet; Transaction Search has not delivered a span in this region (P59)")
    query = None

if query is not None:
    for _ in range(20):
        result = logs.get_query_results(queryId=query)
        if result["status"] == "Complete":
            rows = result.get("results") or []
            spans = int(rows[0][0]["value"]) if rows else 0
            break
        if result["status"] in ("Failed", "Cancelled"):
            print(f"aws/spans query {result['status']}")
            break
        time.sleep(3)
    else:
        print("aws/spans query still running; check CloudWatch Logs Insights on aws/spans")

if spans is not None:
    print(f"invoke_agent spans in aws/spans (last 30 min): {spans}")

# Paginated: a busy account can push this agent's traces past the first page, and an undercount here
# would read as "tracing is broken" exactly when it is not.
end = datetime.datetime.now(datetime.timezone.utc)
ours = 0
pages = boto3.client("xray", region_name=region).get_paginator("get_trace_summaries")
for page in pages.paginate(
    StartTime=end - datetime.timedelta(minutes=30), EndTime=end, TimeRangeType="Event"
):
    for trace in page.get("TraceSummaries", []):
        if any("OpsInvestigator" in (s.get("Name") or "") for s in trace.get("ServiceIds") or []):
            ours += 1
print(f"X-Ray traces naming an OpsInvestigator runtime (last 30 min): {ours}")
# `spans` is 0 when the query ran and found nothing, and None when it could not run at all. Only the
# first of those means "indexed nothing yet"; `not spans` conflated them and printed a conclusion about a
# query that had failed.
if (spans == 0 or query is None) and ours:
    print(
        "Tracing works; Transaction Search has not indexed into aws/spans. Enabling it is per region and "
        "delivery can lag by tens of minutes -- and in a region where it was only just enabled it may not "
        "arrive during this run at all (P59). Read X-Ray, and use the X-Ray trace view for screenshots; "
        "GenAI Observability is the richer view once Transaction Search delivers."
    )
PY

# The runtime log groups carry the whole conversation -- system prompt, user prompt, reasoning, tool
# arguments and answers, as `gen_ai.*.message` span events -- and AgentCore creates them with retention
# "never expire" (P61). Print the command, do not run it: how long an account keeps its own logs is the
# operator's decision, not this script's.
echo "== the runtime log groups keep the full conversation, with no expiry (PITFALLS P61)."
echo "   To cap that, run (14 days is a suggestion, not a recommendation):"
for RUNTIME_ARN in "$ARN" "$A2A_ARN"; do
  echo "   aws logs put-retention-policy --region $REGION \\"
  echo "     --log-group-name /aws/bedrock-agentcore/runtimes/${RUNTIME_ARN##*/}-DEFAULT --retention-in-days 14"
done

echo "== done. Teardown with: make destroy   (left up for the demo by default)"
# This run wrote a real discovery URL, a real client id and a real A2A_PUBLIC_URL (which carries the
# account id) into agentcore/agentcore.json. --reset-placeholders puts all three back, but git is the
# one-step undo that also drops the reformatting.
echo "== before committing: git checkout agentcore/agentcore.json"
echo "   (or, to keep other edits: uv run python scripts/auth_bootstrap.py --reset-placeholders)"
