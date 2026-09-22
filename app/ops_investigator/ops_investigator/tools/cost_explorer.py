"""Cost Explorer summary. The API lives in us-east-1 and bills ~$0.01 per request, hence the small budget."""

from __future__ import annotations

import datetime as dt
import json

from botocore.exceptions import BotoCoreError, ClientError
from strands import tool
from strands.types.tools import ToolContext

from ops_investigator.tools import _budget, _clients

CALL_BUDGET = 10
CE_REGION = "us-east-1"
GROUPS = ("SERVICE", "REGION", "LINKED_ACCOUNT", "USAGE_TYPE")


@tool(context=True)
def cost_explorer(
    days: int = 7,
    group_by: str = "SERVICE",
    region: str | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Unblended cost for the last `days` days grouped by SERVICE, REGION, LINKED_ACCOUNT, or USAGE_TYPE (top 25)."""
    state = tool_context.invocation_state if tool_context is not None else None
    if group_by not in GROUPS:
        return json.dumps({"error": f"group_by must be one of {GROUPS}", "hint": "e.g. group_by='SERVICE'"})
    if not _budget.take(state, "cost_explorer", CALL_BUDGET):
        return json.dumps({"error": "budget_exhausted", "hint": f"{CALL_BUDGET} Cost Explorer calls per turn"})
    days = max(1, min(int(days), 90))
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    ids = (state or {}).get("ids", {})
    try:
        client = _clients.make_client(
            "ce", CE_REGION, session_id=ids.get("session_id", ""), actor_id=ids.get("actor_id", "")
        )
        res = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY" if days > 31 else "DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
        )
        totals: dict[str, float] = {}
        for period in res.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                key = g["Keys"][0]
                totals[key] = totals.get(key, 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        groups = sorted(({"key": k, "usd": round(v, 2)} for k, v in totals.items()), key=lambda x: -x["usd"])[:25]
        return json.dumps(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "group_by": group_by,
                "total_usd": round(sum(totals.values()), 2),
                "groups": groups,
            }
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        return json.dumps(
            {
                "error": f"{code}: {e.response.get('Error', {}).get('Message', '')}"[:500],
                "hint": "Cost Explorer must be enabled in the account (Billing console) and ce:GetCostAndUsage allowed",
            }
        )
    except (BotoCoreError, KeyError) as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"[:500], "hint": "transient AWS error; retry once"})
