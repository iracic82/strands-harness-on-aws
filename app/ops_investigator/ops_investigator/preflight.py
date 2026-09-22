"""Startup self-checks logged to CloudWatch (spec 5). Hard failures keep /ping unhealthy; warnings never crash."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops_investigator import platform

log = logging.getLogger("ops_investigator.preflight")

# What ADOT sets `OTEL_TRACES_EXPORTER` to, itself, when it owns tracing. The container never sets this
# variable, but `opentelemetry-instrument` loads the ADOT distro before the app starts and the distro does
# `os.environ.setdefault(OTEL_TRACES_EXPORTER, "otlp")` under AgentCore's agent-observability mode
# (amazon/opentelemetry/distro/aws_opentelemetry_distro.py). So "set to otlp" is the healthy state, and
# only some *other* value means a human took the exporter away from ADOT (P8, P52).
ADOT_TRACES_EXPORTER = "otlp"

# The fix a human should apply, keyed by the Bedrock error code a 1-token ConverseStream returns (P25).
ACCOUNT_DENIED = "not available for this account"
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
    """1-token ConverseStream: the only reliable model-access check (P25). Returns `Code: message` or None.

    Streaming, not `Converse`: it is the call the harness actually makes, and an account can be refused
    ConverseStream on a model whose plain Converse succeeds.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        response = boto3.client("bedrock-runtime", region_name=region).converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        _drain(response.get("stream"))
        return None
    except ClientError as e:
        err = e.response.get("Error", {})
        return f"{err.get('Code', 'ClientError')}: {err.get('Message', '')}".strip(": ")
    except BotoCoreError as e:
        return f"{type(e).__name__}: {e}"


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


def _bare_model_id(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _model_fix(err: str, model: str) -> str:
    code = err.split(":", 1)[0]
    if code == "AccessDeniedException" and ACCOUNT_DENIED in err:
        return (
            f"This account has no access to {model}. Bedrock console > Model access (or request access via "
            f"AWS Sales); or set HARNESS_MODEL to an available model such as {platform.DEFAULT_MODEL}"
        )
    return MODEL_FIXES.get(code, DEFAULT_MODEL_FIX)


def _check_model(rep: PreflightReport, rt: platform.Runtime) -> None:
    try:
        err = converse_probe(_bare_model_id(rt.model), rt.region or "")
    except Exception as e:  # a broken probe must never take the container down (spec 5)
        err = type(e).__name__
    if err:
        rep.add("model", "WARN", f"{rt.model}: {err[:200]}. Fix: {_model_fix(err, rt.model)}.")
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
    exporter = env.get("OTEL_TRACES_EXPORTER", "")
    if exporter and exporter != ADOT_TRACES_EXPORTER:
        rep.add(
            "otel_exporter",
            "WARN",
            f"OTEL_TRACES_EXPORTER={exporter!r}; ADOT should own the exporter in AgentCore (P8).",
        )
    else:
        rep.add("otel_exporter", "PASS", f"{exporter or 'unset'}; ADOT owns tracing")
    if probe_model:
        _check_model(rep, rt)
    return rep
