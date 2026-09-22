#!/usr/bin/env python3
"""doctor.py: preflight for running the Strands harness on AgentCore. Drop into any harness project.

Usage: python doctor.py --region us-west-2 [--model bedrock/global.anthropic.claude-sonnet-4-6]
       [--build Container|CodeZip] [--json]
Exit code: 0 all PASS, 1 any WARN, 2 any FAIL. Every FAIL/WARN prints the fix. Needs only boto3.
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
    for prefix in ("global.", "us.", "eu."):
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


def _model_check(name: str, clients: ClientFactory, region: str, model_id: str) -> Check:
    status = _model_availability(clients, region, model_id)
    if status is not None and status != "AVAILABLE":
        return Check(name, "FAIL", f"{model_id}: agreementAvailability={status}", _availability_fix(model_id, status))
    err = _converse(clients, region, model_id)
    return Check(
        name,
        "PASS" if err is None else "FAIL",
        model_id if err is None else f"{model_id}: {err}",
        "Bedrock console > Model access: enable the model (and its global/us inference profile); the harness "
        "uses Haiku 4.5 for web_fetch/memory (P4, P25).",
    )


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

    v = sys.version_info
    out.append(
        Check(
            "python",
            "PASS" if v >= (3, 10) else "FAIL",
            f"{v.major}.{v.minor} (tested on 3.12 and 3.14; 3.12 recommended)",
            "Install Python 3.10+ (3.12 recommended).",
        )
    )
    out.append(
        Check(
            "uv",
            "PASS" if which("uv") else "WARN",
            which("uv") or "missing",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
    )
    out.append(
        Check(
            "node",
            "PASS" if which("node") else "FAIL",
            which("node") or "missing",
            "Install Node 20+ (needed for the @aws/agentcore CLI and CDK).",
        )
    )

    code, text = _cmd(run_cmd, ["agentcore", "--version"])
    if code != 0:
        out.append(Check("agentcore_cli", "FAIL", "agentcore not found", "npm install -g @aws/agentcore"))
    elif (
        "Usage: agentcore [OPTIONS]" in text
    ):  # the deprecated Python starter toolkit prints Click-style usage (P1/P20)
        out.append(
            Check(
                "agentcore_cli",
                "FAIL",
                "deprecated Python starter toolkit is first on PATH",
                "pip uninstall bedrock-agentcore-starter-toolkit; npm install -g @aws/agentcore",
            )
        )
    else:
        out.append(Check("agentcore_cli", "PASS", text.strip().splitlines()[0][:40], ""))

    # Credentials run first among the AWS checks and name the account + caller ARN + profile prominently: a
    # wrong AWS_PROFILE silently points every later check at the wrong account.
    profile = os.environ.get("AWS_PROFILE", "default")
    try:
        ident = clients("sts", region).get_caller_identity()
        out.append(Check("credentials", "PASS", f"account={ident['Account']} arn={ident['Arn']} profile={profile}", ""))
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        out.append(
            Check(
                "credentials",
                "FAIL",
                f"{type(e).__name__} profile={profile}",
                "aws sso login --profile <profile>; export AWS_PROFILE=<profile>",
            )
        )
        return out

    out.append(_use_case_form_check(clients, region))

    out.append(Check("region_set", "PASS" if region else "FAIL", region or "unset", "export AWS_REGION=us-west-2 (P5)"))

    try:
        clients("bedrock-agentcore-control", region).list_agent_runtimes(maxResults=1)
        out.append(Check("agentcore_region", "PASS", region, ""))
    except (ClientError, BotoCoreError) as e:
        out.append(
            Check(
                "agentcore_region",
                "FAIL",
                type(e).__name__,
                f"AgentCore Runtime is not reachable in {region}; pick a supported region (P7).",
            )
        )

    bare_model = model.split("/", 1)[-1]
    out.append(_model_check("model_main", clients, region, bare_model))
    out.append(_model_check("model_small", clients, region, small_model))

    try:
        dest = clients("xray", region).get_trace_segment_destination()
        active = dest.get("Status") == "ACTIVE" and dest.get("Destination") == "CloudWatchLogs"
        out.append(
            Check(
                "transaction_search",
                "PASS" if active else "FAIL",
                json.dumps(dest, default=str)[:80],
                "Enable CloudWatch Transaction Search once per region: aws xray update-trace-segment-destination "
                "--destination CloudWatchLogs (P8)",
            )
        )
    except (ClientError, BotoCoreError) as e:
        out.append(
            Check(
                "transaction_search",
                "WARN",
                type(e).__name__,
                "Could not query X-Ray; verify Transaction Search in the CloudWatch console (P8).",
            )
        )

    try:
        clients("cloudformation", region).describe_stacks(StackName="CDKToolkit")
        out.append(Check("cdk_bootstrap", "PASS", "CDKToolkit present", ""))
    except (ClientError, BotoCoreError):
        out.append(
            Check(
                "cdk_bootstrap",
                "WARN",
                "CDKToolkit stack missing",
                f"npx cdk bootstrap aws://<account>/{region} (agentcore deploy runs it if allowed)",
            )
        )

    if build == "Container":
        code, text = _cmd(run_cmd, ["docker", "buildx", "inspect", "--bootstrap"])
        ok = code == 0 and "linux/arm64" in text
        out.append(
            Check(
                "docker_arm64",
                "PASS" if ok else "FAIL",
                "linux/arm64 available" if ok else text.strip()[-80:],
                "AgentCore needs arm64 images. Install Docker with buildx and QEMU (docker run --privileged "
                "tonistiigi/binfmt --install arm64) (P16).",
            )
        )

    code, text = _cmd(run_cmd, ["git", "ls-files", ".agent", "app/ops_investigator/.agent"])
    out.append(
        Check(
            "agent_dir_untracked",
            "PASS" if not text.strip() else "FAIL",
            "clean" if not text.strip() else text.strip()[:80],
            "Local sessions/memory are plaintext transcripts: git rm -r --cached .agent && add .agent/ to .gitignore",
        )
    )
    return out


def worst(checks: list[Check]) -> int:
    return max((STATUS_ORDER[c.status] for c in checks), default=0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--small-model", default=DEFAULT_SMALL)
    p.add_argument("--build", default="Container", choices=["Container", "CodeZip"])
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    region = a.region or boto3.session.Session().region_name or ""
    checks = run(region, a.model, a.small_model, a.build)
    if a.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            line = f"[{c.status:4}] {c.name:20} {c.detail}"
            print(line if c.status == "PASS" else f"{line}\n       fix: {c.fix}")
    return worst(checks)


if __name__ == "__main__":
    sys.exit(main())
