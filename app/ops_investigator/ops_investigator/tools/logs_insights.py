"""CloudWatch Logs Insights, bounded. The agent's own log groups are never readable (spec 4.6)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError
from strands import tool
from strands.types.tools import ToolContext

from ops_investigator.tools import _budget, _clients

DEFAULT_PREFIXES = ("/aws/lambda/", "/aws/ecs/", "/aws/apigateway/", "/aws/eks/")
FORBIDDEN_PREFIXES = ("/aws/bedrock-agentcore/", "aws/spans")
CALL_BUDGET = 10
MAX_MINUTES = 24 * 60
POLL_SECONDS = 1.0
POLL_LIMIT = 25


def configured_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("OPS_LOG_GROUP_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_PREFIXES


def allowed_log_group(name: str, prefixes: Sequence[str]) -> bool:
    if any(name.startswith(f) for f in FORBIDDEN_PREFIXES):
        return False
    return any(name.startswith(p) for p in prefixes)


@tool(context=True)
def logs_insights(
    log_group: str,
    query_string: str,
    minutes: int = 60,
    limit: int = 100,
    region: str | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Run a CloudWatch Logs Insights query over the last `minutes` on one allowed log group and return rows.

    Allowed groups are configured by OPS_LOG_GROUP_PREFIXES. Keep queries narrow (filter + limit).
    """
    state = tool_context.invocation_state if tool_context is not None else None
    log_group = log_group.strip()
    query_string = query_string.strip()
    if not allowed_log_group(log_group, configured_prefixes()):
        return json.dumps(
            {
                "error": f"log group {log_group} is not in the allowed prefixes",
                "hint": f"allowed prefixes: {', '.join(configured_prefixes())}",
            }
        )
    if not _budget.take(state, "logs_insights", CALL_BUDGET):
        return json.dumps({"error": "budget_exhausted", "hint": f"{CALL_BUDGET} Logs Insights queries per turn"})
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    limit = max(1, min(int(limit), 500))
    ids = (state or {}).get("ids", {})
    try:
        client = _clients.make_client(
            "logs", region, session_id=ids.get("session_id", ""), actor_id=ids.get("actor_id", "")
        )
        end = int(time.time())
        qid = client.start_query(
            logGroupName=log_group,
            startTime=end - minutes * 60,
            endTime=end,
            queryString=query_string,
            limit=limit,
        )["queryId"]
        for _ in range(POLL_LIMIT):
            res = client.get_query_results(queryId=qid)
            if res.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
                rows = [{c["field"]: c["value"] for c in row} for row in res.get("results", [])]
                return json.dumps(
                    {
                        "log_group": log_group,
                        "status": res["status"],
                        "rows": rows,
                        "stats": res.get("statistics", {}),
                    },
                    default=str,
                )
            time.sleep(POLL_SECONDS)
        return json.dumps({"error": "query still running", "hint": "narrow the time window or query", "query_id": qid})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        return json.dumps(
            {
                "error": f"{code}: {e.response.get('Error', {}).get('Message', '')}"[:500],
                "hint": "check the log group name and the execution role's logs permissions",
            }
        )
    except (BotoCoreError, KeyError) as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"[:500], "hint": "transient AWS error; retry once"})
