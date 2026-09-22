from __future__ import annotations

import doctor


class Fake:
    """Stands in for a boto3 client: `responses` keyed by operation name, `errors` by operation -> error code."""

    def __init__(self, responses: dict, errors: dict | None = None):
        self.r, self.e = responses, errors or {}

    def __getattr__(self, op):
        def call(**kw):
            if op in self.e:
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": self.e[op], "Message": ""}}, op)
            return self.r.get(op, {})

        return call


def _clients(overrides=None):
    base = {
        "sts": Fake({"get_caller_identity": {"Account": "1", "Arn": "arn:aws:sts::1:assumed-role/x/y"}}),
        "bedrock": Fake(
            {
                "get_use_case_for_model_access": {"formData": b"submitted"},
                "get_foundation_model_availability": {"agreementAvailability": {"status": "AVAILABLE"}},
            }
        ),
        "bedrock-agentcore-control": Fake({"list_agent_runtimes": {"agentRuntimes": []}}),
        "bedrock-runtime": Fake({"converse_stream": {"stream": iter([])}}),
        "xray": Fake({"get_trace_segment_destination": {"Destination": "CloudWatchLogs", "Status": "ACTIVE"}}),
        "cloudformation": Fake({"describe_stacks": {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}}),
    }
    base.update(overrides or {})
    return lambda service, region: base[service]


def _tools(missing=()):
    return lambda name: None if name in missing else f"/usr/bin/{name}"


def _run_ok(args=(), **k):
    from types import SimpleNamespace

    if list(args[:2]) == ["git", "ls-files"]:  # a clean tree: nothing tracked under .agent
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return SimpleNamespace(returncode=0, stdout="0.30.0\nlinux/arm64, linux/amd64\nv25.6.1\n", stderr="")


def test_all_pass():
    checks = doctor.run(
        "us-west-2",
        "bedrock/global.anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        clients=_clients(),
        which=_tools(),
        run_cmd=_run_ok,
    )
    assert all(c.status == "PASS" for c in checks), [(c.name, c.status, c.detail) for c in checks if c.status != "PASS"]


def test_transaction_search_inactive_is_fail():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"xray": Fake({"get_trace_segment_destination": {"Status": "INACTIVE"}})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    c = next(c for c in checks if c.name == "transaction_search")
    assert c.status == "FAIL" and "Transaction Search" in c.fix


def test_model_access_denied_is_fail():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"bedrock-runtime": Fake({}, {"converse_stream": "AccessDeniedException"})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    assert next(c for c in checks if c.name == "model_main").status == "FAIL"


def test_model_availability_not_available_is_fail_before_any_converse_call():
    calls = []
    real_clients = _clients(
        {
            "bedrock": Fake(
                {
                    "get_use_case_for_model_access": {"formData": b"submitted"},
                    "get_foundation_model_availability": {"agreementAvailability": {"status": "NOT_AVAILABLE"}},
                }
            )
        }
    )

    def clients(service, region):
        calls.append(service)
        return real_clients(service, region)

    checks = doctor.run("us-west-2", "m", "s", clients=clients, which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "model_main")
    assert c.status == "FAIL"
    assert "agreementAvailability=NOT_AVAILABLE" in c.detail
    assert "Bedrock console > Model access" in c.fix
    assert "bedrock-runtime" not in calls  # the availability gate short-circuits before a real Converse


def test_use_case_form_missing_is_fail_with_console_fix():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"bedrock": Fake({}, {"get_use_case_for_model_access": "ResourceNotFoundException"})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    c = next(c for c in checks if c.name == "use_case_form")
    assert c.status == "FAIL"
    assert "use case form" in c.fix and "Submit use case details" in c.fix


def test_use_case_form_other_error_is_warn():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"bedrock": Fake({}, {"get_use_case_for_model_access": "ThrottlingException"})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    c = next(c for c in checks if c.name == "use_case_form")
    assert c.status == "WARN"


def test_use_case_form_runs_right_after_credentials():
    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    names = [c.name for c in checks]
    assert names.index("use_case_form") == names.index("credentials") + 1


def test_missing_cdk_bootstrap_is_warn():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"cloudformation": Fake({}, {"describe_stacks": "ValidationError"})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    assert next(c for c in checks if c.name == "cdk_bootstrap").status == "WARN"


def test_old_python_toolkit_detected():
    def run_cmd(args, **k):
        from types import SimpleNamespace

        if args[:2] == ["agentcore", "--version"]:
            return SimpleNamespace(returncode=0, stdout="Usage: agentcore [OPTIONS] COMMAND", stderr="")
        return _run_ok(args, **k)

    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=run_cmd)
    c = next(c for c in checks if c.name == "agentcore_cli")
    assert c.status == "FAIL" and "npm install -g @aws/agentcore" in c.fix


def test_credentials_failure_short_circuits_and_reports_profile(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"sts": Fake({}, {"get_caller_identity": "ExpiredToken"})}),
        which=_tools(),
        run_cmd=_run_ok,
    )
    assert [c.name for c in checks] == ["python", "uv", "node", "agentcore_cli", "credentials"]
    c = checks[-1]
    assert c.status == "FAIL" and "profile=default" in c.detail


def test_credentials_pass_reports_account_arn_and_profile(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "my-profile")
    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "credentials")
    assert c.status == "PASS"
    assert "account=1" in c.detail
    assert "arn=arn:aws:sts::1:assumed-role/x/y" in c.detail
    assert "profile=my-profile" in c.detail


def test_python_check_notes_tested_versions():
    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "python")
    assert "3.12" in c.detail


def test_docker_check_skipped_for_codezip_build():
    checks = doctor.run("us-west-2", "m", "s", build="CodeZip", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    assert not any(c.name == "docker_arm64" for c in checks)


def test_exit_code_is_worst_status():
    assert doctor.worst([doctor.Check("a", "PASS", "", ""), doctor.Check("b", "WARN", "", "")]) == 1
    assert doctor.worst([doctor.Check("a", "FAIL", "", "")]) == 2


def test_main_json_output_and_exit_code(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda *a, **k: [doctor.Check("x", "WARN", "d", "f")])
    code = doctor.main(["--region", "us-west-2", "--json"])
    out = capsys.readouterr().out
    assert code == 1
    assert '"status": "WARN"' in out


def test_main_prints_fix_for_non_pass(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda *a, **k: [doctor.Check("x", "FAIL", "d", "the fix")])
    code = doctor.main(["--region", "us-west-2"])
    out = capsys.readouterr().out
    assert code == 2
    assert "fix: the fix" in out
