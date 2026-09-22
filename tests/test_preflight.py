from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from ops_investigator import platform, preflight

CLOUD = {"MEMORY_OPSINVESTIGATORMEMORY_ID": "m", "AWS_REGION": "us-west-2"}


def test_local_profile_needs_nothing():
    rep = preflight.run_preflight(env={}, probe_model=False)
    assert rep.ok
    assert ("profile", "PASS", "local") in rep.checks


def test_cloud_missing_region_fails():
    rep = preflight.run_preflight(env={"MEMORY_OPSINVESTIGATORMEMORY_ID": "m"}, probe_model=False)
    assert not rep.ok and any(c[0] == "region" and c[1] == "FAIL" and "AWS_REGION" in c[2] for c in rep.checks)


def test_bad_effort_fails_as_config():
    rep = preflight.run_preflight(env={**CLOUD, "HARNESS_EFFORT": "turbo"}, probe_model=False)
    assert not rep.ok and any(c[0] == "config" and c[1] == "FAIL" for c in rep.checks)


def test_cloud_checks_tempdir_and_otel(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.tempfile, "gettempdir", lambda: str(tmp_path))
    rep = preflight.run_preflight(env={**CLOUD, "OTEL_TRACES_EXPORTER": "otlp"}, probe_model=False)
    names = {c[0]: c[1] for c in rep.checks}
    assert names["tempdir"] == "PASS"
    assert names["otel_exporter"] == "WARN"  # harness would fight ADOT for the exporter (P8)
    assert rep.ok  # warnings never make preflight fail


def test_cloud_unwritable_tempdir_fails(monkeypatch, tmp_path):
    unwritable = tmp_path / "nope"
    monkeypatch.setattr(preflight.tempfile, "gettempdir", lambda: str(unwritable))
    rep = preflight.run_preflight(env=CLOUD, probe_model=False)
    assert not rep.ok and any(c[0] == "tempdir" and c[1] == "FAIL" for c in rep.checks)


def test_model_probe_reports_error(monkeypatch):
    monkeypatch.setattr(preflight, "converse_probe", lambda model_id, region: "AccessDeniedException: denied")
    rep = preflight.run_preflight(env=CLOUD, probe_model=True)
    model = next(c for c in rep.checks if c[0] == "model")
    assert model[1] == "WARN" and "AccessDeniedException" in model[2] and "model access" in model[2]
    assert rep.ok


def test_model_probe_explains_an_account_level_denial(monkeypatch):
    err = f"AccessDeniedException: anthropic.claude-sonnet-5 is {preflight.ACCOUNT_DENIED}."
    monkeypatch.setattr(preflight, "converse_probe", lambda model_id, region: err)
    rep = preflight.run_preflight(env={**CLOUD, "HARNESS_MODEL": "bedrock/global.anthropic.claude-sonnet-5"})
    detail = next(c for c in rep.checks if c[0] == "model")[2]
    assert "This account has no access to bedrock/global.anthropic.claude-sonnet-5" in detail
    assert "Bedrock console > Model access" in detail
    assert platform.DEFAULT_MODEL in detail  # the suggested fallback tracks the current default


def test_model_probe_passes(monkeypatch):
    monkeypatch.setattr(preflight, "converse_probe", lambda model_id, region: None)
    rep = preflight.run_preflight(env=CLOUD, probe_model=True)
    assert any(c[0] == "model" and c[1] == "PASS" for c in rep.checks)


def test_model_probe_never_raises(monkeypatch):
    def boom(model_id, region):
        raise RuntimeError("boto3 is unhappy")

    monkeypatch.setattr(preflight, "converse_probe", boom)
    rep = preflight.run_preflight(env=CLOUD, probe_model=True)
    assert any(c[0] == "model" and c[1] == "WARN" and "RuntimeError" in c[2] for c in rep.checks)


def test_model_probe_strips_the_provider_prefix(monkeypatch):
    seen = {}
    monkeypatch.setattr(preflight, "converse_probe", lambda model_id, region: seen.update(id=model_id, region=region))
    preflight.run_preflight(env={**CLOUD, "HARNESS_MODEL": "bedrock/global.anthropic.claude-sonnet-5"})
    assert seen == {"id": "global.anthropic.claude-sonnet-5", "region": "us-west-2"}


class FakeStream:
    """Stands in for botocore's EventStream: iterable once, and closeable."""

    def __init__(self):
        self.closed = False
        self.read = 0

    def __iter__(self):
        for event in ({"messageStart": {}}, {"messageStop": {}}):
            self.read += 1
            yield event

    def close(self):
        self.closed = True


def test_converse_probe_streams_one_token_and_drains_the_stream(monkeypatch):
    seen, stream = {}, FakeStream()

    class FakeClient:
        def converse_stream(self, **kw):
            seen.update(kw)
            return {"stream": stream}

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: FakeClient())
    assert preflight.converse_probe("model-x", "us-west-2") is None
    assert seen["modelId"] == "model-x" and seen["inferenceConfig"] == {"maxTokens": 1}
    assert stream.read == 2 and stream.closed  # the harness's own call path, not a plain Converse


@pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException"])
def test_converse_probe_returns_the_error_code_and_message(monkeypatch, code):
    class FakeClient:
        def converse_stream(self, **kw):
            raise ClientError({"Error": {"Code": code, "Message": "model-x is not available"}}, "ConverseStream")

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: FakeClient())
    assert preflight.converse_probe("model-x", "us-west-2") == f"{code}: model-x is not available"
