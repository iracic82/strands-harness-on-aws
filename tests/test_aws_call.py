from __future__ import annotations

import importlib
import json

import boto3
import pytest
from botocore.stub import Stubber
from ops_investigator.tools import _clients

# ops_investigator/tools/__init__.py re-exports `aws_call` (the function) at the package level, which
# shadows the `aws_call` submodule attribute; importlib.import_module reaches the submodule directly
# (via sys.modules) so `mod.DENY`/`mod.OUTPUT_CAP`/etc. below see the module, not the re-exported tool.
mod = importlib.import_module("ops_investigator.tools.aws_call")


def _call(**kw):
    return mod.aws_call._tool_func(**kw)


@pytest.mark.parametrize(
    "service,op,ok",
    [
        ("ec2", "describe_instances", True),
        ("lambda", "list_functions", True),
        ("cloudtrail", "lookup_events", True),
        ("ec2", "run_instances", False),
        ("iam", "attach_role_policy", False),
        ("s3", "get_object", False),  # deny table
        ("ssm", "get_parameter", False),
        ("ssm", "get_parameters_by_path", False),
        ("secretsmanager", "get_secret_value", False),
        ("dynamodb", "scan", False),
        ("lambda", "get_function", False),
        ("ec2", "get_password_data", False),
        ("logs", "get_log_events", False),  # logs go through logs_insights only
        ("sts", "assume_role", False),
        ("kms", "decrypt", False),
        ("sqs", "receive_message", False),
        ("cognito-idp", "list_users", False),
    ],
)
def test_is_read_operation(service, op, ok):
    assert mod.is_read_operation(service, op) is ok


def _stubbed(service, region, **_):
    client = boto3.client(service, region_name=region or "us-west-2", aws_access_key_id="x", aws_secret_access_key="y")
    return client


def test_denied_operation_never_builds_a_client(monkeypatch):
    calls = []
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: calls.append(a))
    out = json.loads(_call(service="s3", operation="get_object", parameters={}, tool_context=_ctx()))
    assert "error" in out and "read-only" in out["hint"]
    assert calls == []


def _ctx(state=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        invocation_state=state if state is not None else {}, tool_use={"name": "aws_call"}, agent=None
    )


def test_success_path_and_jmespath(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    stub = Stubber(client)
    stub.add_response("describe_regions", {"Regions": [{"RegionName": "us-west-2"}, {"RegionName": "eu-west-1"}]}, {})
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    out = json.loads(
        _call(service="ec2", operation="describe_regions", query="Regions[].RegionName", tool_context=_ctx())
    )
    assert out["data"] == ["us-west-2", "eu-west-1"]
    assert out["truncated"] is False


def test_service_and_operation_normalized_before_the_call(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    stub = Stubber(client)
    stub.add_response("describe_regions", {"Regions": [{"RegionName": "us-west-2"}]}, {})
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    out = json.loads(_call(service=" EC2 ", operation=" Describe_Regions", tool_context=_ctx()))
    assert out["service"] == "ec2"
    assert out["operation"] == "describe_regions"
    assert out["data"]["Regions"][0]["RegionName"] == "us-west-2"


def test_unknown_operation_returns_json_error_not_exception(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    out = json.loads(_call(service="ec2", operation="describe_nothing", tool_context=_ctx()))
    assert out["error"].startswith("KeyError")


def test_returned_is_zero_when_jmespath_matches_nothing(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    stub = Stubber(client)
    stub.add_response("describe_regions", {"Regions": [{"RegionName": "us-west-2"}]}, {})
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    out = json.loads(_call(service="ec2", operation="describe_regions", query="NoSuchTopLevelKey", tool_context=_ctx()))
    assert out["data"] is None
    assert out["returned"] == 0


def test_output_cap(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    stub = Stubber(client)
    stub.add_response("describe_regions", {"Regions": [{"RegionName": "r" * 3000} for _ in range(30)]}, {})
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    raw = _call(service="ec2", operation="describe_regions", tool_context=_ctx())
    assert len(raw) <= mod.OUTPUT_CAP + 500
    assert json.loads(raw)["truncated"] is True


def test_client_error_is_structured(monkeypatch):
    client = _stubbed("ec2", "us-west-2")
    stub = Stubber(client)
    stub.add_client_error("describe_regions", service_error_code="AccessDeniedException", http_status_code=403)
    stub.activate()
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: client)
    out = json.loads(_call(service="ec2", operation="describe_regions", tool_context=_ctx()))
    assert out["error"].startswith("AccessDeniedException")
    assert "execution role" in out["hint"]


def test_budget_exhausted(monkeypatch):
    state = {}
    monkeypatch.setattr(_clients, "make_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client")))
    for _ in range(mod.CALL_BUDGET):
        state.setdefault("budget", {})["aws_call"] = state.get("budget", {}).get("aws_call", 0) + 1
    out = json.loads(_call(service="ec2", operation="describe_regions", tool_context=_ctx(state)))
    assert out["error"] == "budget_exhausted"


def test_user_agent_extra_carries_ids():
    cfg = _clients.client_config(session_id="s" * 33, actor_id="igor")
    assert "strands-harness-on-aws" in cfg.user_agent_extra and "actor=igor" in cfg.user_agent_extra
    assert cfg.read_timeout == 30
