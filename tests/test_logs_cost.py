from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber
from ops_investigator.tools import _clients

# ops_investigator/tools/__init__.py re-exports `logs_insights`/`cost_explorer` (the functions) at the
# package level, which shadows the submodule attributes; importlib.import_module reaches the submodule
# directly (via sys.modules) so `li.time`/`ce.CALL_BUDGET`/etc. below see the module, not the re-exported tool.
li = importlib.import_module("ops_investigator.tools.logs_insights")
ce = importlib.import_module("ops_investigator.tools.cost_explorer")


def _call(tool, **kw):
    return tool._tool_func(**kw)


def _ctx(state=None):
    return SimpleNamespace(invocation_state=state if state is not None else {}, tool_use={}, agent=None)


@pytest.mark.parametrize(
    "name,ok",
    [
        ("/aws/lambda/orders", True),
        ("/aws/bedrock-agentcore/runtimes/x", False),
        ("aws/spans", False),
        ("/custom/app", False),
    ],
)
def test_allowed_log_group(name, ok):
    assert li.allowed_log_group(name, ["/aws/lambda/", "/aws/ecs/"]) is ok


def test_logs_insights_denied_group_makes_no_call(monkeypatch):
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    out = json.loads(
        _call(
            li.logs_insights,
            log_group="/aws/bedrock-agentcore/runtimes/x",
            query_string="fields @message",
            tool_context=_ctx(),
        )
    )
    assert "error" in out


def test_logs_insights_polls_until_complete(monkeypatch):
    client = boto3.client("logs", region_name="us-west-2", aws_access_key_id="x", aws_secret_access_key="y")
    stub = Stubber(client)
    stub.add_response("start_query", {"queryId": "q1"})
    stub.add_response("get_query_results", {"status": "Running", "results": []})
    stub.add_response(
        "get_query_results", {"status": "Complete", "results": [[{"field": "@message", "value": "boom"}]]}
    )
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(li.time, "sleep", lambda s: None)
    out = json.loads(
        _call(li.logs_insights, log_group="/aws/lambda/orders", query_string="fields @message", tool_context=_ctx())
    )
    assert out["rows"] == [{"@message": "boom"}]


def test_cost_explorer_groups_by_service(monkeypatch):
    client = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    stub = Stubber(client)
    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-09-15", "End": "2026-09-22"},
                    "Groups": [
                        {"Keys": ["Amazon Bedrock"], "Metrics": {"UnblendedCost": {"Amount": "12.5", "Unit": "USD"}}}
                    ],
                    "Total": {},
                    "Estimated": True,
                }
            ]
        },
    )
    stub.activate()
    seen = {}

    def fake(service, region, **k):
        seen["region"] = region
        return client

    monkeypatch.setattr(_clients, "make_client", fake)
    out = json.loads(_call(ce.cost_explorer, days=7, tool_context=_ctx()))
    assert seen["region"] == "us-east-1"
    assert out["groups"][0] == {"key": "Amazon Bedrock", "usd": 12.5}


def test_cost_explorer_budget():
    state = {"budget": {"cost_explorer": ce.CALL_BUDGET}}
    assert json.loads(_call(ce.cost_explorer, tool_context=_ctx(state)))["error"] == "budget_exhausted"
