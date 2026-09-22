"""Offline tests for the operator scripts (spec 4.4, 7). No AWS calls: every boto3 client is faked."""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.awsrequest import AWSRequest
from ops_investigator.runtime_common import MIN_SESSION_LEN

from scripts import auth_bootstrap, invoke, memory_wipe
from scripts.a2a_client import card_protocol_version, summarize_event

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agentcore" / "agentcore.json"
MAKEFILE = ROOT / "Makefile"


@pytest.fixture
def config_copy(tmp_path) -> Path:
    """A throwaway copy of the real agentcore.json; the committed one must never be rewritten by a test."""
    path = tmp_path / "agentcore.json"
    shutil.copy(CONFIG, path)
    return path


# --- auth_bootstrap ---------------------------------------------------------------------------------


def test_placeholder_constants_match_the_committed_config():
    """If the placeholders in agentcore.json are ever respelled, --reset-placeholders would write the
    wrong thing back, and the repo-hygiene gate is the only thing standing between that and a commit."""
    config = json.loads(CONFIG.read_text())
    for runtime in config["runtimes"]:
        jwt = runtime["authorizerConfiguration"]["customJwtAuthorizer"]
        assert jwt["discoveryUrl"] == auth_bootstrap.placeholder_discovery_url()
        assert jwt["allowedClients"] == [auth_bootstrap.PLACEHOLDER]
    env_values = {
        var["value"]
        for runtime in config["runtimes"]
        for var in runtime["envVars"]
        if var["name"] == "COGNITO_DISCOVERY_URL"
    }
    assert env_values == {auth_bootstrap.PLACEHOLDER}


def test_apply_auth_rewrites_every_jwt_runtime_and_env_var(config_copy):
    config = auth_bootstrap.load_config(config_copy)
    discovery = auth_bootstrap.discovery_url("eu-west-1", "eu-west-1_Abc123")
    changed = auth_bootstrap.apply_auth(config, discovery=discovery, client_id="client-xyz")
    auth_bootstrap.write_config(config_copy, config)

    assert changed == ["OpsInvestigator", "OpsInvestigatorA2A"]
    written = config_copy.read_text()
    assert written.endswith("}\n")  # json.dump writes no trailing newline of its own
    reloaded = json.loads(written)
    for runtime in reloaded["runtimes"]:
        jwt = runtime["authorizerConfiguration"]["customJwtAuthorizer"]
        assert jwt["discoveryUrl"] == discovery
        assert jwt["allowedClients"] == ["client-xyz"]
    env = {var["name"]: var["value"] for var in reloaded["runtimes"][1]["envVars"]}
    assert env["COGNITO_DISCOVERY_URL"] == discovery
    assert env["A2A_PUBLIC_URL"] == "REPLACED_AFTER_FIRST_DEPLOY"  # a different script's placeholder, untouched


def test_reset_placeholders_restores_the_committed_values(config_copy):
    pristine = json.loads(config_copy.read_text())
    config = auth_bootstrap.load_config(config_copy)
    auth_bootstrap.apply_auth(
        config, discovery=auth_bootstrap.discovery_url("us-west-2", "us-west-2_Real"), client_id="real"
    )
    auth_bootstrap.write_config(config_copy, config)
    assert json.loads(config_copy.read_text()) != pristine

    assert auth_bootstrap.main(["--reset-placeholders", "--config", str(config_copy)]) == 0
    assert json.loads(config_copy.read_text()) == pristine


def test_apply_auth_leaves_a_runtime_without_a_jwt_authorizer_alone():
    config = {"runtimes": [{"name": "Plain", "envVars": [{"name": "AWS_REGION", "value": "us-west-2"}]}]}
    assert (
        auth_bootstrap.apply_auth(config, discovery="https://x/.well-known/openid-configuration", client_id="c") == []
    )
    assert config == {"runtimes": [{"name": "Plain", "envVars": [{"name": "AWS_REGION", "value": "us-west-2"}]}]}


def test_bootstrap_needs_a_password(capsys):
    with pytest.raises(SystemExit):
        auth_bootstrap.main(["--region", "us-west-2", "--password", ""])
    assert "DEMO_PASSWORD" in capsys.readouterr().err


# --- invoke -----------------------------------------------------------------------------------------


def test_new_session_id_clears_agentcores_floor():
    session = invoke.new_session_id()
    assert len(session) == 40 >= MIN_SESSION_LEN
    assert session != invoke.new_session_id()


def test_invocations_url_encodes_the_arn():
    url = invoke.invocations_url("arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/Ops-abc", "us-west-2")
    assert url == (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime%2FOps-abc/invocations?qualifier=DEFAULT"
    )


def test_iter_frames_parses_sse_lines_and_passes_junk_through():
    lines = [
        b'data: {"v": 1, "type": "text", "delta": "hi"}',
        b"",
        b"<html>502 Bad Gateway</html>",
        b"data: not json",
        b'data: ["not", "an", "object"]',
    ]
    assert list(invoke.iter_frames(lines)) == [
        {"v": 1, "type": "text", "delta": "hi"},
        "<html>502 Bad Gateway</html>",
        "data: not json",
        'data: ["not", "an", "object"]',
    ]


def test_frame_printer_renders_every_v1_frame_type():
    frames = [
        {"v": 1, "type": "thinking", "delta": "let me look"},
        {"v": 1, "type": "text", "delta": "Checking "},
        {"v": 1, "type": "text", "delta": "the account."},
        {"v": 1, "type": "tool_use", "name": "aws_call", "input_keys": ["operation", "service"]},
        {"v": 1, "type": "tool_result", "name": "aws_call", "status": "success", "bytes": 42},
        {"v": 1, "type": "denied", "name": "shell", "reason": "read-only"},
        {"v": 1, "type": "metrics", "input_tokens": 10, "output_tokens": 2, "est_usd": 0.001},
        {"v": 1, "type": "error", "message": "boom", "hint": "see the logs"},
        {"v": 1, "type": "done"},
    ]
    printer = invoke.FramePrinter()
    out = "".join(printer.render(frame) for frame in frames) + printer.finish()
    assert out.splitlines() == [
        "[thinking] let me look",
        "Checking the account.",
        "[tool_use] aws_call(operation, service)",
        "[tool_result] aws_call status=success bytes=42",
        "[denied] shell: read-only",
        '[metrics] {"est_usd": 0.001, "input_tokens": 10, "output_tokens": 2}',
        "[error] boom (hint: see the logs)",
    ]
    assert printer.done and printer.errors == 1


def test_frame_printer_raw_mode_and_unknown_types():
    printer = invoke.FramePrinter(raw=True)
    assert printer.render({"v": 1, "type": "text", "delta": "hi"}) == '{"v": 1, "type": "text", "delta": "hi"}\n'
    assert invoke.FramePrinter().render({"v": 1, "type": "future", "x": 1}) == '[future] {"x": 1}\n'
    assert invoke.FramePrinter().render("502 Bad Gateway") == "502 Bad Gateway\n"


def test_print_frames_stops_at_done():
    lines = [
        'data: {"v": 1, "type": "text", "delta": "answer"}',
        'data: {"v": 1, "type": "done"}',
        'data: {"v": 1, "type": "text", "delta": "never printed"}',
    ]
    out = io.StringIO()
    printer = invoke.print_frames(lines, out=out)
    assert out.getvalue() == "answer\n"
    assert printer.done and printer.errors == 0


def test_boto3_is_unsigned_only_when_a_bearer_token_is_in_play():
    """SigV4 deletes any Authorization header before it signs, so the two cannot be combined."""
    assert invoke.boto3_config(None) is None
    assert invoke.boto3_config("jwt").signature_version is not None


def test_the_bearer_token_survives_on_the_boto3_transport():
    """The whole point of the UNSIGNED client: `before-sign` still fires, and nothing overwrites the
    header afterwards. Emitting the event by hand is the only way to see that without a real call."""
    client = invoke.bearer_client("us-west-2", "jwt-value")
    request = AWSRequest(method="POST", url="https://example.invalid/", headers={})
    client.meta.events.emit("before-sign.bedrock-agentcore.InvokeAgentRuntime", request=request)
    assert request.headers["Authorization"] == "Bearer jwt-value"

    unsigned = invoke.bearer_client("us-west-2", None)
    plain = AWSRequest(method="POST", url="https://example.invalid/", headers={})
    unsigned.meta.events.emit("before-sign.bedrock-agentcore.InvokeAgentRuntime", request=plain)
    assert "Authorization" not in plain.headers


@pytest.mark.parametrize(
    "argv",
    [
        ["--prompt", "hi"],  # no --arn and no --dev
        ["--prompt", "hi", "--arn", "arn:x"],  # no --region
        ["--prompt", "hi", "--arn", "arn:x", "--region", "us-west-2", "--transport", "https"],  # no --token
    ],
)
def test_invoke_refuses_an_unusable_combination(argv):
    with pytest.raises(SystemExit) as excinfo:
        invoke.main(argv)
    assert excinfo.value.code == 2


# --- a2a_client -------------------------------------------------------------------------------------


def test_card_protocol_version_reads_the_1_0_shape_and_falls_back_to_0_3():
    assert card_protocol_version({"supportedInterfaces": [{"protocolVersion": "1.0"}]}) == "1.0"
    assert card_protocol_version({"protocolVersion": "0.3.0"}) == "0.3.0"
    assert card_protocol_version({}) is None


def test_summarize_event_handles_each_stream_response_shape():
    assert summarize_event({"task": {"id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_SUBMITTED"}}}) == (
        "[task] id=t1 context=c1 state=TASK_STATE_SUBMITTED"
    )
    status = {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING", "message": {"parts": [{"text": "hi"}]}}}}
    assert summarize_event(status) == "[status] TASK_STATE_WORKING: hi"
    artifact = {"artifactUpdate": {"artifact": {"name": "answer", "parts": [{"text": "all "}, {"text": "good"}]}}}
    assert summarize_event(artifact) == "[artifact] answer: all good"
    assert summarize_event({"message": {"parts": [{"text": "note"}]}}) == "[message] note"
    assert summarize_event({"unknown": 1}) == '[event] {"unknown": 1}'


# --- memory_wipe ------------------------------------------------------------------------------------


class FakeMemoryClient:
    """The slice of the bedrock-agentcore data plane memory_wipe uses, recording what it deleted."""

    def __init__(self, records: dict[str, list[dict]], events: dict[str, list[dict]], *, paginators: bool = True):
        self.records = records
        self.events = events
        self.paginators = paginators
        self.deleted: list[tuple[str, str]] = []

    def can_paginate(self, name: str) -> bool:
        return self.paginators

    def get_paginator(self, name: str) -> Any:
        return SimpleNamespace(paginate=lambda **kwargs: iter(self._pages(name, kwargs)))

    def _pages(self, name: str, kwargs: dict) -> list[dict]:
        if name == "list_memory_records":
            return [{"memoryRecordSummaries": self.records.get(kwargs["namespace"], [])}]
        if name == "list_sessions":
            return [{"sessionSummaries": [{"sessionId": sid} for sid in self.events]}]
        return [{"events": self.events.get(kwargs["sessionId"], [])}]

    def list_memory_records(self, **kwargs: Any) -> dict:
        return self._pages("list_memory_records", kwargs)[0]

    def list_sessions(self, **kwargs: Any) -> dict:
        return self._pages("list_sessions", kwargs)[0]

    def list_events(self, **kwargs: Any) -> dict:
        return self._pages("list_events", kwargs)[0]

    def delete_memory_record(self, *, memoryId: str, memoryRecordId: str) -> dict:  # noqa: N803 (boto3 spelling)
        self.deleted.append(("record", memoryRecordId))
        return {"memoryRecordId": memoryRecordId}

    def delete_event(self, *, memoryId: str, actorId: str, sessionId: str, eventId: str) -> dict:  # noqa: N803
        self.deleted.append(("event", eventId))
        return {"eventId": eventId}


def _fake_client(**kwargs: Any) -> FakeMemoryClient:
    return FakeMemoryClient(
        records={"/preferences/bob/": [{"memoryRecordId": "r1"}], "/facts/bob/": [{"memoryRecordId": "r2"}]},
        events={"s1": [{"eventId": "e1"}, {"eventId": "e2"}]},
        **kwargs,
    )


def test_namespaces_match_the_strategies_in_agentcore_json():
    templates = [
        template
        for strategy in json.loads(CONFIG.read_text())["memories"][0]["strategies"]
        for template in strategy["namespaceTemplates"]
    ]
    assert memory_wipe.namespaces("bob") == [template.replace("{actorId}", "bob") for template in templates]


def test_wipe_deletes_every_record_and_event_for_the_actor(capsys):
    client = _fake_client()
    assert memory_wipe.wipe_records(client, "mem-1", "bob") == 2
    assert memory_wipe.wipe_events(client, "mem-1", "bob") == 2
    assert client.deleted == [("record", "r1"), ("record", "r2"), ("event", "e1"), ("event", "e2")]
    assert "record /preferences/bob/r1" in capsys.readouterr().out


def test_dry_run_lists_without_deleting():
    client = _fake_client()
    assert memory_wipe.wipe_records(client, "mem-1", "bob", dry_run=True) == 2
    assert memory_wipe.wipe_events(client, "mem-1", "bob", dry_run=True) == 2
    assert client.deleted == []


def test_wipe_falls_back_to_a_manual_next_token_loop():
    client = _fake_client(paginators=False)
    assert memory_wipe.wipe_records(client, "mem-1", "bob") == 2
    assert client.deleted == [("record", "r1"), ("record", "r2")]


# --- the scripts directory itself -------------------------------------------------------------------


def test_no_script_shadows_a_stdlib_module():
    """`python scripts/x.py` puts scripts/ first on sys.path. A `token.py` there shadows the stdlib
    `token` module, which `tokenize` imports, which `linecache` imports, which every traceback needs --
    so one badly named script breaks every other script in this directory (that is why the Cognito one
    is `get_token.py`)."""
    shadows = sorted(path.name for path in (ROOT / "scripts").glob("*.py") if path.stem in sys.stdlib_module_names)
    assert shadows == []


def test_makefile_only_reaches_the_agentcore_cli_through_npx_from_the_repo_root():
    """A deprecated Python `agentcore` shadows the npm CLI on some machines, and the CLI wants the repo
    root, not agentcore/, as its working directory."""
    text = MAKEFILE.read_text()
    assert "AGENTCORE = npx -y @aws/agentcore@0.30.0" in text
    for line in text.splitlines():
        body = line.strip()
        assert not body.startswith("agentcore "), line
        assert "cd agentcore" not in body, line


def test_every_script_the_makefile_runs_exists_and_is_executable():
    referenced = {
        word.strip('"')
        for line in MAKEFILE.read_text().splitlines()
        for word in line.split()
        if word.strip('"').startswith("scripts/")
    }
    assert referenced >= {"scripts/invoke.py", "scripts/a2a_client.py", "scripts/smoke.sh"}
    for name in referenced:
        path = ROOT / name
        assert path.is_file(), name
        assert path.stat().st_mode & 0o111, f"{name} is not executable"


def test_makefile_declares_every_target_phony():
    lines = [line for line in MAKEFILE.read_text().splitlines() if line and not line[0].isspace()]
    phony = set(next(line for line in lines if line.startswith(".PHONY:")).removeprefix(".PHONY:").split())
    targets = {line.split(":")[0] for line in lines if ":" in line and not line.startswith(("#", ".PHONY"))}
    assert targets == phony
