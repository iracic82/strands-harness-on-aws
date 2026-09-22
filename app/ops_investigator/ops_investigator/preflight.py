"""Startup self-checks logged to CloudWatch (spec 5). Hard failures keep /ping unhealthy; warnings never crash."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ops_investigator import platform

log = logging.getLogger("ops_investigator.preflight")

# The fix a human should apply, keyed by the Bedrock error code a 1-token Converse returns (P25).
MODEL_FIXES = {
    "AccessDeniedException": (
        "request model access in the Bedrock console and give the execution role bedrock:InvokeModel* on this model"
    ),
    "ValidationException": (
        "the model id is not usable in this region; check HARNESS_MODEL and whether it needs an inference profile"
    ),
}
DEFAULT_MODEL_FIX = "check HARNESS_MODEL, AWS_REGION, and the execution role's Bedrock permissions"


@dataclass
class PreflightReport:
    ok: bool = True
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append((name, status, detail))
        if status == "FAIL":
            self.ok = False
        level = {"PASS": "info", "WARN": "warning", "FAIL": "error"}[status]
        getattr(log, level)("preflight %s=%s %s", name, status, detail)


def converse_probe(model_id: str, region: str) -> str | None:
    """1-token Converse call: the only reliable model-access check (P25). Returns error code or None."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        boto3.client("bedrock-runtime", region_name=region).converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        return None
    except ClientError as e:
        return e.response.get("Error", {}).get("Code", "ClientError")
    except BotoCoreError as e:
        return type(e).__name__


def _bare_model_id(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _check_model(rep: PreflightReport, rt: platform.Runtime) -> None:
    try:
        err = converse_probe(_bare_model_id(rt.model), rt.region or "")
    except Exception as e:  # a broken probe must never take the container down (spec 5)
        err = type(e).__name__
    if err:
        rep.add("model", "WARN", f"{rt.model}: {err}. Fix: {MODEL_FIXES.get(err, DEFAULT_MODEL_FIX)}.")
    else:
        rep.add("model", "PASS", rt.model)


def run_preflight(env: Mapping[str, str] | None = None, *, probe_model: bool = True) -> PreflightReport:
    env = os.environ if env is None else env
    rep = PreflightReport()
    try:
        rt = platform.detect("preflight-" + "0" * 30, "preflight", env=env)
    except platform.RuntimeConfigError as e:
        rep.add("region" if "AWS_REGION" in str(e) else "config", "FAIL", str(e))
        return rep
    rep.add("profile", "PASS", rt.profile)
    if rt.profile == "local":
        return rep
    tmp = Path(tempfile.gettempdir())
    try:
        probe = tmp / ".preflight-write"
        probe.write_text("ok")
        probe.unlink()
        rep.add("tempdir", "PASS", str(tmp))
    except OSError as e:
        rep.add("tempdir", "FAIL", f"{tmp} is not writable: {e}. The harness needs a writable workspace (P2).")
    rep.add("memory_id", "PASS", rt.memory_id or "")
    if env.get("OTEL_TRACES_EXPORTER"):
        rep.add("otel_exporter", "WARN", "OTEL_TRACES_EXPORTER is set; ADOT should own the exporter in AgentCore (P8).")
    else:
        rep.add("otel_exporter", "PASS", "unset; ADOT owns tracing")
    if probe_model:
        _check_model(rep, rt)
    return rep
