from __future__ import annotations

import logging
from pathlib import Path

from ops_investigator import platform, runtime_common


def test_max_seconds_falls_back_when_the_env_value_is_not_a_number():
    assert runtime_common._max_seconds("42.5") == 42.5
    assert runtime_common._max_seconds("ten minutes") == runtime_common.DEFAULT_MAX_SECONDS == 600.0


def test_max_seconds_reads_the_env():
    assert runtime_common.max_seconds({}) == 600.0
    assert runtime_common.max_seconds({"HARNESS_MAX_SECONDS": "90"}) == 90.0


def test_profile_is_cloud_only_when_the_memory_env_is_set():
    assert runtime_common.profile({}) == "local"
    assert runtime_common.profile({platform.MEMORY_ENV: "mem-1"}) == "cloud"


def test_enter_local_app_dir_chdirs_locally(monkeypatch, tmp_path):
    """The harness `environment` plugin reads AGENTS.md from cwd, and local runs start at the repo root."""
    monkeypatch.chdir(tmp_path)
    runtime_common.enter_local_app_dir()
    assert Path.cwd() == platform.APP_DIR


def test_enter_local_app_dir_leaves_the_cloud_workspace_alone(monkeypatch, tmp_path):
    """In the cloud `platform.enter_workspace` owns the cwd: /app is read-only for the runtime user (P2)."""
    monkeypatch.setenv(platform.MEMORY_ENV, "mem-test-123")
    monkeypatch.chdir(tmp_path)
    runtime_common.enter_local_app_dir()
    assert Path.cwd() == tmp_path


def test_configure_logging_sends_this_package_to_info():
    logging.getLogger("ops_investigator").setLevel(logging.NOTSET)
    runtime_common.configure_logging()
    assert logging.getLogger("ops_investigator").getEffectiveLevel() == logging.INFO
    assert logging.getLogger("botocore").getEffectiveLevel() >= logging.WARNING
