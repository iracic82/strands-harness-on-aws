from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from ops_investigator import preflight

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
    monkeypatch.setattr(preflight, "converse_probe", lambda model_id, region: "AccessDeniedException")
    rep = preflight.run_preflight(env=CLOUD, probe_model=True)
    model = next(c for c in rep.checks if c[0] == "model")
    assert model[1] == "WARN" and "AccessDeniedException" in model[2] and "model access" in model[2]
    assert rep.ok


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


def test_converse_probe_sends_one_token_and_returns_none(monkeypatch):
    seen = {}

    class FakeClient:
        def converse(self, **kw):
            seen.update(kw)
            return {}

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: FakeClient())
    assert preflight.converse_probe("model-x", "us-west-2") is None
    assert seen["modelId"] == "model-x" and seen["inferenceConfig"] == {"maxTokens": 1}


@pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException"])
def test_converse_probe_returns_the_error_code(monkeypatch, code):
    class FakeClient:
        def converse(self, **kw):
            raise ClientError({"Error": {"Code": code}}, "Converse")

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: FakeClient())
    assert preflight.converse_probe("model-x", "us-west-2") == code
