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


class Boom:
    """A client whose every operation raises an exception type none of doctor's own except clauses name —
    stands in for a bug or an unanticipated response shape, to exercise `_guard`'s catch-all."""

    def __getattr__(self, op):
        def call(**kw):
            raise RuntimeError("boom")

        return call


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
    assert all(c.fix == "" for c in checks)  # a PASS never carries a fix


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
    # WARN, not FAIL: every target in this repo reaches the CLI through `npx -y @aws/agentcore@0.30.0`,
    # which is immune to whatever shadows the name on PATH. Telling you is useful; stopping you is not.
    assert c.status == "WARN" and "pip uninstall bedrock-agentcore-starter-toolkit" in c.fix
    assert "make targets use npx" in c.fix


def test_missing_global_cli_is_a_warning_not_a_failure():
    """`npx -y @aws/agentcore@0.30.0` is how every target in this repo reaches the CLI, so a machine
    without a global install runs everything. Failing here would stop a working setup at the door."""

    def run_cmd(args, **k):
        from types import SimpleNamespace

        if args[:2] == ["agentcore", "--version"]:
            return SimpleNamespace(returncode=127, stdout="", stderr="command not found")
        return _run_ok(args, **k)

    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=run_cmd)
    c = next(c for c in checks if c.name == "agentcore_cli")
    assert c.status == "WARN"
    assert "npx" in c.detail and c.fix.startswith("optional:")
    assert doctor.worst(checks) == 1  # a WARN, so the quickstart's `make setup && make doctor` goes on
    assert doctor.exit_code(checks, "fail") == 0  # and `make doctor` (--exit-on fail) does not stop


def test_exit_on_fail_reports_only_a_failure():
    """`make doctor` uses it, so a WARN does not end the quickstart chain. It stays distinguishable
    from the exit 1 an interpreter gives for a traceback, which is why the shell guard was wrong."""
    checks = [doctor.Check("a", "PASS", "", ""), doctor.Check("b", "WARN", "", "")]
    assert doctor.exit_code(checks, "status") == 1
    assert doctor.exit_code(checks, "fail") == 0

    checks.append(doctor.Check("c", "FAIL", "", ""))
    assert doctor.exit_code(checks, "status") == 2
    assert doctor.exit_code(checks, "fail") == 2

    assert doctor.exit_code([], "fail") == 0 and doctor.exit_code([], "status") == 0


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
    monkeypatch.setenv("AWS_PROFILE", "sso-profile")  # not the owner's real profile name; see test_repo_hygiene
    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "credentials")
    assert c.status == "PASS"
    assert "account=1" in c.detail
    assert "arn=arn:aws:sts::1:assumed-role/x/y" in c.detail
    assert "profile=sso-profile" in c.detail


def test_python_check_notes_tested_versions():
    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "python")
    assert "3.12" in c.detail


def test_docker_check_skipped_for_codezip_build():
    checks = doctor.run("us-west-2", "m", "s", build="CodeZip", clients=_clients(), which=_tools(), run_cmd=_run_ok)
    assert not any(c.name == "docker_arm64" for c in checks)


def test_docker_arm64_missing_platform_warns_because_deploy_builds_on_codebuild():
    """A deploy builds both images on an ARM CodeBuild host, so no local Docker is involved and this
    must not be the FAIL that stops `make doctor` and scripts/smoke.sh (P55)."""

    def run_cmd(args, **k):
        from types import SimpleNamespace

        if args[:2] == ["docker", "buildx"]:
            return SimpleNamespace(returncode=0, stdout="linux/amd64\n", stderr="")
        return _run_ok(args, **k)

    checks = doctor.run("us-west-2", "m", "s", clients=_clients(), which=_tools(), run_cmd=run_cmd)
    c = next(c for c in checks if c.name == "docker_arm64")
    assert c.status == "WARN" and c.fix != ""
    assert doctor.worst(checks) < 2  # a WARN never stops a deploy that would have worked


def test_availability_id_strips_the_known_inference_profile_prefixes():
    for prefix in ("global.", "us.", "eu.", "apac."):
        assert doctor._availability_id(f"{prefix}anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"
    assert doctor._availability_id("anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"  # no prefix at all


def test_credentials_malformed_response_does_not_abort_the_run():
    checks = doctor.run(
        "us-west-2",
        "m",
        "s",
        clients=_clients({"sts": Fake({"get_caller_identity": {}})}),  # missing Account/Arn
        which=_tools(),
        run_cmd=_run_ok,
    )
    c = next(c for c in checks if c.name == "credentials")
    assert c.status == "PASS"  # no crash even though Account/Arn are missing
    assert "account=?" in c.detail and "arn=?" in c.detail
    names = [c.name for c in checks]
    assert "use_case_form" in names and "agent_dir_untracked" in names  # the rest of the run still happened


def test_unexpected_exception_in_one_check_becomes_warn_and_others_still_run():
    checks = doctor.run("us-west-2", "m", "s", clients=_clients({"xray": Boom()}), which=_tools(), run_cmd=_run_ok)
    c = next(c for c in checks if c.name == "transaction_search")
    assert c.status == "WARN" and "RuntimeError" in c.detail
    # later checks still ran despite the earlier one blowing up
    assert next(c for c in checks if c.name == "cdk_bootstrap").status == "PASS"
    assert next(c for c in checks if c.name == "agent_dir_untracked").status == "PASS"


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
