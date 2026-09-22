from __future__ import annotations

from types import SimpleNamespace

import pytest
from ops_investigator.guard import DANGEROUS_TOOLS, ReadOnlyGuard
from strands.interventions.actions import Confirm, Deny, Proceed


def _event(name, inp):
    return SimpleNamespace(
        tool_use={"toolUseId": "t1", "name": name, "input": inp},
        agent=None,
        selected_tool=None,
        invocation_state={},
        cancel_tool=None,
    )


def test_allows_read_aws_call():
    r = ReadOnlyGuard().before_tool_call(_event("aws_call", {"service": "ec2", "operation": "describe_instances"}))
    assert isinstance(r, Proceed)


@pytest.mark.parametrize(
    "inp",
    [
        {"service": "ec2", "operation": "terminate_instances"},
        {"service": "s3", "operation": "get_object"},
        {"service": "iam", "operation": "attach_role_policy"},
    ],
)
def test_denies_mutating_or_secret_aws_call(inp):
    r = ReadOnlyGuard().before_tool_call(_event("aws_call", inp))
    assert isinstance(r, Deny) and "read-only" in r.reason


@pytest.mark.parametrize("name", sorted(DANGEROUS_TOOLS))
def test_denies_dangerous_tools_in_cloud_profile(name):
    assert isinstance(ReadOnlyGuard().before_tool_call(_event(name, {"command": "ls"})), Deny)


def test_local_profile_allows_shell_but_blocks_mutating_aws_cli():
    g = ReadOnlyGuard(denied_tools=frozenset())
    assert isinstance(g.before_tool_call(_event("shell", {"command": "aws ec2 describe-instances"})), Proceed)
    assert isinstance(
        g.before_tool_call(_event("shell", {"command": "aws ec2 terminate-instances --instance-ids i-1"})), Deny
    )
    assert isinstance(g.before_tool_call(_event("shell", {"command": "aws s3 rm s3://b/k"})), Deny)


def test_never_confirms():
    g = ReadOnlyGuard()
    for name, inp in [
        ("aws_call", {"service": "x", "operation": "delete_thing"}),
        ("shell", {"command": "rm -rf /"}),
        ("read", {"path": "/etc/hosts"}),
        ("unknown_tool", {}),
    ]:
        assert not isinstance(g.before_tool_call(_event(name, inp)), Confirm)


def test_unknown_tools_proceed():
    assert isinstance(ReadOnlyGuard().before_tool_call(_event("search_memory", {"query": "x"})), Proceed)
