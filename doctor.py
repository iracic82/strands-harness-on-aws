#!/usr/bin/env python3
"""doctor.py: preflight for running the Strands harness on AgentCore. Drop into any harness project.

Usage: python doctor.py --region us-west-2 [--model bedrock/global.anthropic.claude-sonnet-4-6]
       [--small-model ID] [--build Container|CodeZip] [--json] [--exit-on status|fail]
Exit code: 0 all PASS, 1 any WARN, 2 any FAIL. Every FAIL/WARN prints the fix. Needs only boto3.
`--exit-on fail` reports 2 for a FAIL and 0 otherwise, for callers that treat a WARN as "carry on"
without having to tell a WARN apart from the interpreter's own exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

# The Claude 5 family is refused on many accounts (P25); sonnet-4-6 streams everywhere it's been probed.
DEFAULT_MODEL = "bedrock/global.anthropic.claude-sonnet-4-6"
DEFAULT_SMALL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"  # the harness's hidden second model (P4)
STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}

USE_CASE_FORM_FIX = (
    "Anthropic models need the one-time use case form: Bedrock console > Model access > (any Anthropic model) > "
    "Submit use case details, then retry."
)

ClientFactory = Callable[[str, str], Any]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str


def _default_clients(service: str, region: str) -> Any:
    return boto3.client(service, region_name=region or None)


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    """Any single check's own bug (a malformed AWS response the check's specific except clauses didn't
    anticipate, a broken `which`/`run_cmd` injection, etc.) must never take down the rest of the run — convert
    an unexpected exception into a WARN instead of letting it escape `run()`."""
    try:
        return fn()
    except Exception as e:  # deliberately broad: this is the last-resort net for one check's own bug
        return Check(
            name,
            "WARN",
            f"{type(e).__name__}: {e}"[:120],
            "unexpected error in this check; rerun with --json and report it",
        )


def _cmd(run_cmd: Callable[..., subprocess.CompletedProcess[str]], args: list[str]) -> tuple[int, str]:
    try:
        r = run_cmd(args, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def _drain(stream: Any) -> None:
    """Read the event stream to the end (and close it) so the connection goes back to the pool."""
    if stream is None:
        return
    try:
        for _ in stream:
            pass
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def _converse(clients: ClientFactory, region: str, model_id: str) -> str | None:
    """1-token ConverseStream: the call the harness actually makes (P25). Streaming, not plain Converse — an
    account can be refused ConverseStream on a model whose plain Converse succeeds."""
    try:
        resp = clients("bedrock-runtime", region).converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        _drain(resp.get("stream"))
        return None
    except ClientError as e:
        return e.response.get("Error", {}).get("Code", "ClientError")
    except (BotoCoreError, NoCredentialsError) as e:
        return type(e).__name__


def _availability_id(model_id: str) -> str:
    """Bare model id for GetFoundationModelAvailability: no provider or inference-profile prefix."""
    bare = model_id.split("/", 1)[-1]
    for prefix in ("global.", "us.", "eu.", "apac."):
        if bare.startswith(prefix):
            return bare[len(prefix) :]
    return bare


def _model_availability(clients: ClientFactory, region: str, model_id: str) -> str | None:
    """agreementAvailability.status, or None if the call itself failed. GetFoundationModelAvailability can be
    ambiguous (P25), so a failed lookup here falls through to the definitive ConverseStream probe rather than
    reporting anything on its own."""
    try:
        resp = clients("bedrock", region).get_foundation_model_availability(modelId=_availability_id(model_id))
    except (ClientError, BotoCoreError):
        return None
    return resp.get("agreementAvailability", {}).get("status")


def _availability_fix(model_id: str, status: str | None) -> str:
    return (
        f"This account has no access to {model_id} (agreementAvailability={status}). Request access in Bedrock "
        f"console > Model access, or set HARNESS_MODEL to an available model such as {DEFAULT_MODEL}."
    )


def _python_check() -> Check:
    v = sys.version_info
    ok = v >= (3, 10)
    return Check(
        "python",
        "PASS" if ok else "FAIL",
        f"{v.major}.{v.minor} (tested on 3.12 and 3.14; 3.12 recommended)",
        "" if ok else "Install Python 3.10+ (3.12 recommended).",
    )


def _uv_check(which: Callable[[str], str | None]) -> Check:
    path = which("uv")
    return Check(
        "uv",
        "PASS" if path else "WARN",
        path or "missing",
        "" if path else "curl -LsSf https://astral.sh/uv/install.sh | sh",
    )


def _node_check(which: Callable[[str], str | None]) -> Check:
    path = which("node")
    return Check(
        "node",
        "PASS" if path else "FAIL",
        path or "missing",
        "" if path else "Install Node 20+ (needed for the @aws/agentcore CLI and CDK).",
    )


def _agentcore_cli_check(run_cmd: Callable[..., subprocess.CompletedProcess[str]]) -> Check:
    """Neither outcome here is fatal, because nothing in this repo calls a global `agentcore`.

    Every Makefile target and script goes through `npx -y @aws/agentcore@0.30.0`, which fetches the
    pinned version on demand -- that indirection exists precisely because the deprecated Python starter
    toolkit installs a binary of the same name and shadows the npm one (P1/P20). So a missing global CLI
    and a shadowing one are both WARN: worth telling you about, not worth stopping for. `node` is the
    check that fails when `npx` itself is unavailable.
    """
    code, text = _cmd(run_cmd, ["agentcore", "--version"])
    if code != 0:
        return Check(
            "agentcore_cli",
            "WARN",
            "not on PATH; make targets use npx",
            "optional: npm install -g @aws/agentcore",
        )
    if "Usage: agentcore [OPTIONS]" in text:  # the deprecated Python starter toolkit prints Click-style usage (P1/P20)
        return Check(
            "agentcore_cli",
            "WARN",
            "deprecated Python starter toolkit is first on PATH",
            "pip uninstall bedrock-agentcore-starter-toolkit, or ignore: make targets use npx",
        )
    return Check("agentcore_cli", "PASS", text.strip().splitlines()[0][:40], "")


def _credentials_check(clients: ClientFactory, region: str, profile: str) -> Check:
    """Runs first among the AWS checks and names the account + caller ARN + profile prominently: a wrong
    AWS_PROFILE silently points every later check at the wrong account."""
    try:
        ident = clients("sts", region).get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        return Check(
            "credentials",
            "FAIL",
            f"{type(e).__name__} profile={profile}",
            "aws sso login --profile <profile>; export AWS_PROFILE=<profile>",
        )
    account = ident.get("Account", "?")
    arn = ident.get("Arn", "?")
    return Check("credentials", "PASS", f"account={account} arn={arn} profile={profile}", "")


def _use_case_form_check(clients: ClientFactory, region: str) -> Check:
    try:
        resp = clients("bedrock", region).get_use_case_for_model_access()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        if code == "ResourceNotFoundException":
            return Check("use_case_form", "FAIL", code, USE_CASE_FORM_FIX)
        return Check(
            "use_case_form", "WARN", code, "Could not verify the Anthropic use-case form; check manually and retry."
        )
    except BotoCoreError as e:
        return Check(
            "use_case_form",
            "WARN",
            type(e).__name__,
            "Could not verify the Anthropic use-case form; check manually and retry.",
        )
    ok = bool(resp.get("formData"))
    return Check(
        "use_case_form",
        "PASS" if ok else "FAIL",
        "form submitted" if ok else "no formData in response",
        "" if ok else USE_CASE_FORM_FIX,
    )


def _region_set_check(region: str) -> Check:
    return Check(
        "region_set",
        "PASS" if region else "FAIL",
        region or "unset",
        "" if region else "export AWS_REGION=us-west-2 (P5)",
    )


def _agentcore_region_check(clients: ClientFactory, region: str) -> Check:
    try:
        clients("bedrock-agentcore-control", region).list_agent_runtimes(maxResults=1)
    except (ClientError, BotoCoreError) as e:
        return Check(
            "agentcore_region",
            "FAIL",
            type(e).__name__,
            f"AgentCore Runtime is not reachable in {region}; pick a supported region (P7).",
        )
    return Check("agentcore_region", "PASS", region, "")


def _model_check(name: str, clients: ClientFactory, region: str, model_id: str) -> Check:
    status = _model_availability(clients, region, model_id)
    if status is not None and status != "AVAILABLE":
        return Check(name, "FAIL", f"{model_id}: agreementAvailability={status}", _availability_fix(model_id, status))
    err = _converse(clients, region, model_id)
    return Check(
        name,
        "PASS" if err is None else "FAIL",
        model_id if err is None else f"{model_id}: {err}",
        ""
        if err is None
        else (
            "Bedrock console > Model access: enable the model (and its global/us inference profile); the harness "
            "uses Haiku 4.5 for web_fetch/memory (P4, P25)."
        ),
    )


def _transaction_search_check(clients: ClientFactory, region: str) -> Check:
    try:
        dest = clients("xray", region).get_trace_segment_destination()
    except (ClientError, BotoCoreError) as e:
        return Check(
            "transaction_search",
            "WARN",
            type(e).__name__,
            "Could not query X-Ray; verify Transaction Search in the CloudWatch console (P8).",
        )
    active = dest.get("Status") == "ACTIVE" and dest.get("Destination") == "CloudWatchLogs"
    return Check(
        "transaction_search",
        "PASS" if active else "FAIL",
        json.dumps(dest, default=str)[:80],
        ""
        if active
        else (
            "Enable CloudWatch Transaction Search once per region: aws xray update-trace-segment-destination "
            "--destination CloudWatchLogs (P8)"
        ),
    )


def _cdk_bootstrap_check(clients: ClientFactory, region: str) -> Check:
    try:
        clients("cloudformation", region).describe_stacks(StackName="CDKToolkit")
    except (ClientError, BotoCoreError):
        return Check(
            "cdk_bootstrap",
            "WARN",
            "CDKToolkit stack missing",
            f"npx cdk bootstrap aws://<account>/{region} (agentcore deploy runs it if allowed)",
        )
    return Check("cdk_bootstrap", "PASS", "CDKToolkit present", "")


def _docker_arm64_check(run_cmd: Callable[..., subprocess.CompletedProcess[str]]) -> Check:
    """WARN, not FAIL: `make deploy` does not build anything on this machine (P55).

    The CDK stack the CLI synthesises carries a CodeBuild project (`ContainerBuildProject`, environment
    type ARM_CONTAINER) and a custom resource that triggers it, so both arm64 images are built and pushed
    in the account. A deploy from a laptop with no Docker at all succeeds. Docker is still needed to run
    the container locally (`agentcore dev`) and by the CI job that builds the images directly, which is
    why the check stays -- but failing it used to stop `make doctor`, and so `scripts/smoke.sh`, on a
    machine where the deploy would have worked.
    """
    code, text = _cmd(run_cmd, ["docker", "buildx", "inspect", "--bootstrap"])
    ok = code == 0 and "linux/arm64" in text
    return Check(
        "docker_arm64",
        "PASS" if ok else "WARN",
        "linux/arm64 available" if ok else text.strip()[-80:],
        ""
        if ok
        else (
            "`make deploy` builds the arm64 images on a CodeBuild ARM host and does not need this. "
            "Docker with buildx and QEMU (docker run --privileged tonistiigi/binfmt --install arm64) is "
            "needed only to run the container locally (P16, P55)."
        ),
    )


def _agent_dir_untracked_check(run_cmd: Callable[..., subprocess.CompletedProcess[str]]) -> Check:
    code, text = _cmd(run_cmd, ["git", "ls-files", ".agent", "app/ops_investigator/.agent"])
    clean = not text.strip()
    return Check(
        "agent_dir_untracked",
        "PASS" if clean else "FAIL",
        "clean" if clean else text.strip()[:80],
        ""
        if clean
        else "Local sessions/memory are plaintext transcripts: git rm -r --cached .agent && add .agent/ to .gitignore",
    )


def run(
    region: str,
    model: str,
    small_model: str,
    build: str = "Container",
    *,
    clients: ClientFactory | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Check]:
    clients = clients or _default_clients
    out: list[Check] = []

    out.append(_guard("python", _python_check))
    out.append(_guard("uv", lambda: _uv_check(which)))
    out.append(_guard("node", lambda: _node_check(which)))
    out.append(_guard("agentcore_cli", lambda: _agentcore_cli_check(run_cmd)))

    profile = os.environ.get("AWS_PROFILE", "default")
    cred = _guard("credentials", lambda: _credentials_check(clients, region, profile))
    out.append(cred)
    if cred.status == "FAIL":
        return out

    out.append(_guard("use_case_form", lambda: _use_case_form_check(clients, region)))
    out.append(_guard("region_set", lambda: _region_set_check(region)))
    out.append(_guard("agentcore_region", lambda: _agentcore_region_check(clients, region)))

    bare_model = model.split("/", 1)[-1]
    out.append(_guard("model_main", lambda: _model_check("model_main", clients, region, bare_model)))
    out.append(_guard("model_small", lambda: _model_check("model_small", clients, region, small_model)))

    out.append(_guard("transaction_search", lambda: _transaction_search_check(clients, region)))
    out.append(_guard("cdk_bootstrap", lambda: _cdk_bootstrap_check(clients, region)))

    if build == "Container":
        out.append(_guard("docker_arm64", lambda: _docker_arm64_check(run_cmd)))

    out.append(_guard("agent_dir_untracked", lambda: _agent_dir_untracked_check(run_cmd)))
    return out


def worst(checks: list[Check]) -> int:
    return max((STATUS_ORDER[c.status] for c in checks), default=0)


def exit_code(checks: list[Check], exit_on: str) -> int:
    """`status` reports the worst check (0/1/2). `fail` reports 2 for a FAIL and 0 for anything else.

    A caller that wants "stop only on a FAIL" would otherwise have to write `|| [ $? -eq 1 ]`, which
    also swallows an exit 1 this script never produced -- a uv resolution error, an import error, an
    unhandled traceback -- and reports a broken preflight as a clean one.
    """
    return (2 if worst(checks) >= STATUS_ORDER["FAIL"] else 0) if exit_on == "fail" else worst(checks)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--small-model", default=DEFAULT_SMALL)
    p.add_argument("--build", default="Container", choices=["Container", "CodeZip"])
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--exit-on",
        default="status",
        choices=["status", "fail"],
        help="status: 0/1/2 for PASS/WARN/FAIL (default). fail: 2 on any FAIL, 0 otherwise",
    )
    a = p.parse_args(argv)
    region = a.region or boto3.session.Session().region_name or ""
    checks = run(region, a.model, a.small_model, a.build)
    if a.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            line = f"[{c.status:4}] {c.name:20} {c.detail}"
            print(line if c.status == "PASS" else f"{line}\n       fix: {c.fix}")
    return exit_code(checks, a.exit_on)


if __name__ == "__main__":
    sys.exit(main())
