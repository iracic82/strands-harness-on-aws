"""Deterministic read-only intervention (spec P10, 6.1).

A policy *string* passed to create_harness(interventions=...) becomes an LLM classifier that pauses with an
interrupt; in a headless runtime nobody resumes it and the session wedges. This handler returns Deny, never Confirm.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

from strands.hooks import BeforeToolCallEvent
from strands.interventions import InterventionHandler
from strands.interventions.actions import Deny, Proceed

from ops_investigator.tools.aws_call import DENY, is_read_operation

log = logging.getLogger("ops_investigator.guard")

DANGEROUS_TOOLS: frozenset[str] = frozenset({"shell", "write", "edit", "web_fetch", "web_search"})

READ_VERB_PREFIXES = ("describe-", "list-", "get-", "lookup-", "search-", "query-")
READ_VERBS = frozenset({"ls", "help"})
# Value-less aws CLI global flags: everything under --no-* (--no-paginate, --no-cli-pager, --no-sign-request,
# --no-verify-ssl, ...) plus --debug. Any other "-"-prefixed token is assumed to take one value (--profile x).
VALUELESS_FLAGS = frozenset({"--debug"})
# Shell metacharacters shlex.split does not tokenize on; their presence means the command is not the single,
# literal `aws ...` invocation this parser analyses, so refuse outright rather than parse past them.
SHELL_METACHARS = re.compile(r"[;&|`]|\$\(")
# Reuse aws_call's deny table (secret-bearing "reads") in AWS CLI kebab-case spelling; aws_call's DENY is snake_case.
SHELL_DENY = frozenset((svc, op.replace("_", "-")) for svc, op in DENY)


def shell_is_read_only(command: str) -> bool:
    """UX-level check for the local profile: every `aws <service> <verb>` in the command must be a read verb.

    IAM and aws_call's allowlist are the enforced controls; this only improves the local dev UX by refusing
    obviously-mutating aws CLI invocations before they run outside the AWS-API-level guard.
    """
    if SHELL_METACHARS.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    i = 0
    while i < len(tokens):
        if tokens[i].lower() != "aws":
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("-"):  # skip global options (--profile x, --no-cli-pager)
            if tokens[j].startswith("--no-") or tokens[j] in VALUELESS_FLAGS:
                j += 1
            else:
                j += 2 if "=" not in tokens[j] else 1
        if j + 1 >= len(tokens):
            return False
        service, verb = tokens[j].lower(), tokens[j + 1].lower()
        if (service, verb) in SHELL_DENY:
            return False
        if not (verb.startswith(READ_VERB_PREFIXES) or verb in READ_VERBS):
            return False
        i = j + 2
    return True


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
        inp = tool_use.get("input")
        if not isinstance(inp, dict):
            return self._deny(name, "malformed tool input")
        if name in self.denied_tools:
            return self._deny(name, f"{name} is disabled for the read-only investigator in this profile")
        if name == "aws_call":
            service, op = str(inp.get("service", "")), str(inp.get("operation", ""))
            if not is_read_operation(service, op):
                return self._deny(name, f"read-only: {service}:{op} is not a permitted read operation")
        if name == "shell":
            cmd = str(inp.get("command", ""))
            if not shell_is_read_only(cmd):
                return self._deny(
                    name,
                    "read-only: only aws describe/list/get/lookup/search/query (and s3 ls) are allowed from the shell",
                )
        return Proceed()
