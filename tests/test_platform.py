from __future__ import annotations

from pathlib import Path

import pytest
from ops_investigator import platform


def test_detect_local_when_memory_env_absent(tmp_path, monkeypatch):
    rt = platform.detect("s" * 33, "igor", env={"AWS_REGION": "us-west-2"})
    assert rt.profile == "local"
    assert rt.memory_id is None
    assert rt.model == "bedrock/global.anthropic.claude-sonnet-5"
    assert rt.effort == "medium"
    assert rt.workspace == platform.APP_DIR


def test_detect_cloud_when_memory_env_present(tmp_path, monkeypatch):
    monkeypatch.setattr(platform.tempfile, "gettempdir", lambda: str(tmp_path))
    rt = platform.detect("s" * 33, "igor", env={platform.MEMORY_ENV: "mem-123", "AWS_REGION": "us-west-2"})
    assert rt.profile == "cloud"
    assert rt.memory_id == "mem-123"
    assert rt.region == "us-west-2"
    assert rt.workspace == tmp_path / "ops-workspace"


def test_detect_cloud_requires_region():
    with pytest.raises(platform.RuntimeConfigError, match="AWS_REGION"):
        platform.detect("s" * 33, "igor", env={platform.MEMORY_ENV: "mem-123"})


def test_env_overrides_model_and_effort():
    rt = platform.detect(
        "s" * 33,
        "igor",
        env={"HARNESS_MODEL": "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0", "HARNESS_EFFORT": "low"},
    )
    assert rt.model.endswith("haiku-4-5-20251001-v1:0")
    assert rt.effort == "low"


def test_invalid_effort_rejected():
    with pytest.raises(platform.RuntimeConfigError, match="HARNESS_EFFORT"):
        platform.detect("s" * 33, "igor", env={"HARNESS_EFFORT": "turbo"})


def test_mcp_servers_opt_in():
    assert platform.detect("s" * 33, "igor", env={}).mcp_servers is None
    rt = platform.detect("s" * 33, "igor", env={"ENABLE_AWS_DOCS_MCP": "1"})
    assert "aws-docs" in rt.mcp_servers
    assert rt.mcp_servers["aws-docs"]["continue_on_error"] is True


def test_enter_workspace_cloud_creates_dir_and_copies_agents_md(tmp_path, monkeypatch):
    monkeypatch.setattr(platform.tempfile, "gettempdir", lambda: str(tmp_path))
    rt = platform.detect("s" * 33, "igor", env={platform.MEMORY_ENV: "m", "AWS_REGION": "us-west-2"})
    ws = platform.enter_workspace(rt)
    assert ws.is_dir()
    assert (ws / "AGENTS.md").exists() == (platform.APP_DIR / "AGENTS.md").exists()
    assert Path.cwd() == ws
