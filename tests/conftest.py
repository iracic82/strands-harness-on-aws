from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "MEMORY_OPSINVESTIGATORMEMORY_ID",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "HARNESS_MODEL",
        "HARNESS_EFFORT",
        "ENABLE_AWS_DOCS_MCP",
        "OTEL_TRACES_EXPORTER",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def cloud_env(monkeypatch):
    monkeypatch.setenv("MEMORY_OPSINVESTIGATORMEMORY_ID", "mem-test-123")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    return dict(os.environ)


SESSION_ID = "a" * 33
