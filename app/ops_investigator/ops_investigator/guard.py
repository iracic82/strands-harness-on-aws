"""Deterministic read-only intervention (spec P10, 6.1).

A policy *string* passed to create_harness(interventions=...) becomes an LLM classifier that pauses with an
interrupt; in a headless runtime nobody resumes it and the session wedges. This handler returns Deny, never Confirm.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from strands.hooks import BeforeToolCallEvent
from strands.interventions import InterventionHandler
from strands.interventions.actions import Deny, Proceed

from ops_investigator.tools.aws_call import is_read_operation

log = logging.getLogger("ops_investigator.guard")

DANGEROUS_TOOLS: frozenset[str] = frozenset({"shell", "write", "edit", "web_fetch", "web_search"})
# UX-level check for the local profile only; the enforced control for AWS mutation is IAM + aws_call's allowlist.
MUTATING_AWS_CLI = re.compile(
    r"\baws\b.*\b(create|put|delete|update|modify|terminate|run-instances|start|stop|reboot|attach|detach|"
    r"authorize|revoke|add|remove|associate|disassociate|enable|disable|invoke|send|publish|rm|cp|sync|mv|"
    r"assume-role|get-secret-value|get-parameter)\b",
    re.IGNORECASE,
)


class ReadOnlyGuard(InterventionHandler):
    name = "read_only_guard"

    def __init__(self, denied_tools: frozenset[str] = DANGEROUS_TOOLS) -> None:
        self.denied_tools = denied_tools

    def _deny(self, name: str, reason: str) -> Deny:
        log.warning("guard.denied tool=%s reason=%s", name, reason)
        return Deny(reason=reason)

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Deny:
        tool_use = event.tool_use
        name = str(tool_use.get("name", ""))
        inp = tool_use.get("input") or {}
        if name in self.denied_tools:
            return self._deny(name, f"{name} is disabled for the read-only investigator in this profile")
        if name == "aws_call":
            service, op = str(inp.get("service", "")), str(inp.get("operation", ""))
            if not is_read_operation(service, op):
                return self._deny(name, f"read-only: {service}:{op} is not a permitted read operation")
        if name == "shell":
            cmd = str(inp.get("command", ""))
            if MUTATING_AWS_CLI.search(cmd):
                return self._deny(name, "read-only: mutating or secret-reading aws CLI call refused")
        return Proceed()
