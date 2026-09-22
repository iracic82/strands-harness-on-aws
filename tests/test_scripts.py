"""Offline tests for the operator scripts (spec 4.4, 7). No AWS calls: every boto3 client is faked."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import botocore
import pytest
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError
from ops_investigator import sse
from ops_investigator.runtime_common import MIN_SESSION_LEN

from scripts import auth_bootstrap, invoke, memory_wipe, setup
from scripts.a2a_client import card_protocol_version, card_url_for, summarize_event

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


def test_reset_placeholders_restores_every_deployment_time_value(config_copy):
    """Including A2A_PUBLIC_URL, which `apply_auth` never touches but `smoke.sh` fills in with a URL
    containing the account id. The reset is the one "put the file back" command, so it has to cover it."""
    pristine = json.loads(config_copy.read_text())
    config = auth_bootstrap.load_config(config_copy)
    auth_bootstrap.apply_auth(
        config, discovery=auth_bootstrap.discovery_url("us-west-2", "us-west-2_Real"), client_id="real"
    )
    deployed_url = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3A.../invocations"
    assert auth_bootstrap.set_env_var(config, auth_bootstrap.PUBLIC_URL_ENV, deployed_url) == ["OpsInvestigatorA2A"]
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


# --- setup ------------------------------------------------------------------------------------------

# The placeholder account, and the only 12-digit number the repo-hygiene gate allows in a tracked file.
EXAMPLE_ACCOUNT = "123456789012"


class FakeSts:
    """The one call setup.py makes: who am I, and in which account."""

    def get_caller_identity(self) -> dict:
        return {"Account": EXAMPLE_ACCOUNT, "Arn": f"arn:aws:sts::{EXAMPLE_ACCOUNT}:assumed-role/Admin/igor"}


@pytest.fixture
def generated(tmp_path, monkeypatch) -> SimpleNamespace:
    """setup.py pointed at a throwaway directory, with STS faked and the profile's region empty."""
    targets, env = tmp_path / "aws-targets.json", tmp_path / ".env"
    monkeypatch.setattr(setup, "TARGETS", targets)
    monkeypatch.setattr(setup, "ENV", env)
    monkeypatch.setattr(
        setup,
        "boto3",
        SimpleNamespace(
            client=lambda *args, **kwargs: FakeSts(),
            session=SimpleNamespace(Session=lambda: SimpleNamespace(region_name=None)),
        ),
    )
    monkeypatch.delenv("AWS_REGION", raising=False)
    return SimpleNamespace(targets=targets, env=env)


def test_setup_writes_both_generated_files_for_the_detected_account(generated, capsys):
    assert setup.main(["--region", "eu-west-1"]) == 0

    targets = json.loads(generated.targets.read_text())
    assert [(t["account"], t["region"]) for t in targets] == [(EXAMPLE_ACCOUNT, "eu-west-1")]
    # The example's description tells you to copy the file; the generated one says where it came from.
    assert all("setup.py" in t["description"] for t in targets)

    env = generated.env.read_text()
    assert "AWS_REGION=eu-west-1" in env and "AWS_DEFAULT_REGION=eu-west-1" in env
    assert f"AWS_ACCOUNT_ID={EXAMPLE_ACCOUNT}" in env
    assert "<region>" not in env and "<account-id>" not in env

    out = capsys.readouterr().out
    assert "written" in out and f"account={EXAMPLE_ACCOUNT} region=eu-west-1 caller=igor" in out
    assert out.rstrip().endswith("next: make doctor")


def test_setup_is_idempotent_and_never_clobbers_an_edit(generated, capsys):
    setup.main(["--region", "us-west-2"])
    capsys.readouterr()

    assert setup.main(["--region", "us-west-2"]) == 0
    assert "unchanged" in capsys.readouterr().out

    generated.env.write_text("AWS_REGION=my-own-thing\n")
    assert setup.main(["--region", "us-west-2"]) == 0
    assert "kept" in capsys.readouterr().out
    assert generated.env.read_text() == "AWS_REGION=my-own-thing\n"

    assert setup.main(["--region", "us-west-2", "--force"]) == 0
    assert "written" in capsys.readouterr().out
    assert "AWS_REGION=us-west-2" in generated.env.read_text()


def test_setup_writes_nothing_when_sts_refuses(generated, monkeypatch, capsys):
    """Expired SSO is the normal way this fails. A half-written aws-targets.json would deploy into
    whatever account the file ended up naming, and the error you would get is about CDK."""

    def raise_expired(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "GetCallerIdentity")

    monkeypatch.setattr(setup.boto3, "client", raise_expired)

    assert setup.main(["--region", "us-west-2"]) == 2
    assert not generated.targets.exists() and not generated.env.exists()
    err = capsys.readouterr().err
    assert "ExpiredToken" in err and "aws sso login" in err


def test_setup_refuses_an_identity_without_an_account(generated, monkeypatch, capsys):
    monkeypatch.setattr(setup.boto3, "client", lambda *args, **kwargs: SimpleNamespace(get_caller_identity=dict))

    assert setup.main(["--region", "us-west-2"]) == 2
    assert not generated.targets.exists() and not generated.env.exists()
    assert "without an account id" in capsys.readouterr().err


def test_detect_region_prefers_the_flag_then_the_environment_then_the_profile(monkeypatch):
    profile = SimpleNamespace(session=SimpleNamespace(Session=lambda: SimpleNamespace(region_name="sa-east-1")))
    monkeypatch.setattr(setup, "boto3", profile)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    assert setup.detect_region("ap-south-1") == "ap-south-1"
    assert setup.detect_region(None) == "eu-west-2"
    monkeypatch.delenv("AWS_REGION")
    assert setup.detect_region(None) == "sa-east-1"


def test_reset_placeholders_delegates_to_auth_bootstrap_and_calls_no_aws(monkeypatch, capsys):
    """One copy of the placeholder spellings. `setup.py` is the command people remember, not a second
    set of regexes -- and putting a file back must not need credentials."""
    seen: list[Path] = []

    def fake_reset(path: Path) -> list[str]:
        seen.append(path)
        return ["OpsInvestigator", "OpsInvestigatorA2A"]

    monkeypatch.setattr(setup.auth_bootstrap, "reset_placeholders", fake_reset)
    monkeypatch.setattr(setup, "boto3", None)  # any AWS call would raise AttributeError

    assert setup.main(["--reset-placeholders"]) == 0
    assert seen == [auth_bootstrap.CONFIG]
    assert "OpsInvestigator, OpsInvestigatorA2A" in capsys.readouterr().out


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
        {"v": 1, "type": "denied", "tool": "shell", "reason": "read-only"},
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


def test_frame_printer_renders_a_real_denied_frame_from_the_sse_mapper():
    """Regression: `_line` once read `frame['name']` for a `denied` frame, but `sse.EventMapper` emits
    `tool`, so every pretty-printed denial showed a blank tool name. Route an actual mapped
    `tool_cancel_event` (the real SDK shape) through `FramePrinter.render`, not a hand-built fixture."""
    ev = {
        "tool_cancel_event": {
            "tool_use": {"toolUseId": "t1", "name": "aws_call"},
            "message": "DENIED: read-only: ec2:terminate_instances is not a permitted read operation",
        }
    }
    frame = sse.map_event(ev)
    printer = invoke.FramePrinter()
    line = printer.render(frame) + printer.finish()
    assert "aws_call" in line
    assert "DENIED: read-only: ec2:terminate_instances is not a permitted read operation" in line


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
    assert invoke.boto3_config("jwt").signature_version is botocore.UNSIGNED


def _recorded_stream(monkeypatch) -> list[tuple[str, dict, bytes]]:
    """Replace the HTTP transport so open_stream can be inspected without a socket."""
    calls: list[tuple[str, dict, bytes]] = []

    @contextlib.contextmanager
    def fake(url: str, headers: dict, body: bytes):
        calls.append((url, headers, body))
        yield []

    monkeypatch.setattr(invoke, "_http_stream", fake)
    return calls


def test_open_stream_builds_the_dev_request(monkeypatch):
    calls = _recorded_stream(monkeypatch)
    args = invoke.build_parser().parse_args(["--dev", "--prompt", "hi", "--actor", "me"])
    with invoke.open_stream(args, "s" * 40):
        pass
    url, headers, body = calls[0]
    assert url == "http://localhost:8080/invocations"
    assert headers == {invoke.SESSION_HEADER: "s" * 40, invoke.ACTOR_HEADER: "me", "Content-Type": "application/json"}
    assert json.loads(body) == {"prompt": "hi"}


def test_open_stream_builds_the_https_request(monkeypatch):
    calls = _recorded_stream(monkeypatch)
    args = invoke.build_parser().parse_args(
        ["--transport", "https", "--arn", "arn:aws:x", "--region", "us-west-2", "--token", "jwt", "--prompt", "hi"]
    )
    with invoke.open_stream(args, "s" * 40):
        pass
    url, headers, body = calls[0]
    assert url == invoke.invocations_url("arn:aws:x", "us-west-2")
    assert headers == {
        "Authorization": "Bearer jwt",
        invoke.SESSION_HEADER: "s" * 40,
        "Content-Type": "application/json",
    }
    assert invoke.ACTOR_HEADER not in headers  # in the cloud the actor comes from the token's `sub`
    assert json.loads(body) == {"prompt": "hi"}


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


def test_card_url_puts_the_well_known_path_on_the_path_not_the_query():
    """A deployed AgentCore A2A runtime is addressed with a `?qualifier=` query, and its card is at
    `<invocations>/.well-known/agent-card.json?qualifier=...` -- the request URI of GetAgentCard."""
    assert card_url_for("http://localhost:9000") == "http://localhost:9000/.well-known/agent-card.json"
    assert card_url_for("http://localhost:9000/") == "http://localhost:9000/.well-known/agent-card.json"
    assert card_url_for(
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x%2Fy/invocations?qualifier=DEFAULT"
    ) == (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x%2Fy/invocations"
        "/.well-known/agent-card.json?qualifier=DEFAULT"
    )


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


def test_make_doctor_stops_on_a_fail_but_not_on_a_warning():
    """make collapses every non-zero recipe status to its own exit 2, so without the guard a single WARN
    ends `make setup && make doctor && make dev`. `$$?` is make's escape for the shell's `$?`."""
    lines = MAKEFILE.read_text().splitlines()
    recipe = next(line.strip() for line in lines if line.strip().startswith("uv run python doctor.py"))
    assert recipe.endswith("|| [ $$? -eq 1 ]"), recipe


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
