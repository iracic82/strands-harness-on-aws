from __future__ import annotations

import asyncio

from ops_investigator import agent as mod
from ops_investigator import platform
from ops_investigator.guard import ReadOnlyGuard
from strands.types.agent import Limits


def _offline_session_manager(monkeypatch):
    """Keep the AgentCore session manager off the network.

    `AgentCoreMemorySessionManager.__init__` calls `read_session`, which hits ListEvents (and CreateEvent when the
    session is new). Returning a session here exercises the real constructor and config without an API call.
    """
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from strands.types.session import Session, SessionType

    monkeypatch.setattr(
        AgentCoreMemorySessionManager,
        "read_session",
        lambda self, session_id, **kw: Session(session_id=session_id, session_type=SessionType.AGENT),
    )
    return AgentCoreMemorySessionManager


def test_limits_keys_are_valid():  # P12: unknown keys are silently ignored by the SDK
    for lim in (mod.LIMITS, mod.READER_LIMITS):
        assert set(lim) <= set(Limits.__annotations__)
        assert lim["turns"] > 0 and lim["output_tokens"] > 0 and lim["total_tokens"] > lim["output_tokens"]


def test_cloud_builtin_tools_pinned(cloud_env):
    rt = platform.detect("s" * 33, "igor")
    bt = platform.builtin_tools(rt)
    assert bt["*"] is False and bt["read"] is True
    assert bt["programmatic_tool_caller"]["allowed_tools"] == ["aws_call", "logs_insights", "cost_explorer"]
    for name in ("shell", "write", "edit", "web_fetch", "web_search", "subagent"):
        assert name not in bt or bt[name] is False


def test_local_builtin_tools_drop_subagent_and_web_search():
    rt = platform.detect("s" * 33, "igor")
    bt = platform.builtin_tools(rt)
    assert bt == {"subagent": False, "web_search": False}


def test_cloud_session_and_memory_options(cloud_env, monkeypatch, tmp_path):
    monkeypatch.setattr(platform.tempfile, "gettempdir", lambda: str(tmp_path))
    session_manager_cls = _offline_session_manager(monkeypatch)
    rt = platform.detect("s" * 33, "igor")
    sm = platform.session_option(rt)
    assert isinstance(sm, session_manager_cls)
    cfg = sm.config if hasattr(sm, "config") else sm._config
    assert cfg.async_mode is True and cfg.retrieval_config is None and cfg.filter_restored_tool_context is True
    mem = platform.memory_option(rt)
    assert set(mem) == {"stores"} and len(mem["stores"]) == 2
    assert all(getattr(s, "writable", False) is False for s in mem["stores"])


def test_cloud_guard_denies_dangerous_tools(cloud_env):
    rt = platform.detect("s" * 33, "igor")
    guard = platform.guard_for(rt)
    assert isinstance(guard, ReadOnlyGuard)
    assert {"shell", "write", "edit", "web_fetch", "web_search"} <= guard.denied_tools


def test_local_options():
    rt = platform.detect("s" * 33, "igor")
    assert platform.session_option(rt) == {"id": "s" * 33, "dir": str(platform.APP_DIR / ".agent" / "sessions")}
    assert platform.memory_option(rt) == {"dir": str(platform.APP_DIR / ".agent" / "memory")}
    assert platform.skills_option(rt) == str(platform.APP_DIR / "skills")
    assert isinstance(platform.guard_for(rt), ReadOnlyGuard) and platform.guard_for(rt).denied_tools == frozenset()


def test_build_agent_local_wires_tools_and_limits(monkeypatch):
    captured = {}

    def fake_create_harness(**kw):
        captured.update(kw)

        class A:  # minimal stand-in
            stream_async = None

        return A()

    monkeypatch.setattr(mod, "create_harness", fake_create_harness)
    mod.build_agent("s" * 33, "igor", env={})
    names = [getattr(t, "tool_name", getattr(t, "name", str(t))) for t in captured["tools"]]
    assert {"aws_call", "logs_insights", "cost_explorer", "reader"} <= set(names)
    assert captured["name"] == "ops-investigator"
    assert captured["trace_attributes"]["session.id"] == "s" * 33
    assert captured["trace_attributes"]["user.id"] == "igor"
    assert captured["background_tasks"] == mod.BACKGROUND_TASKS
    assert isinstance(captured["interventions"][0], ReadOnlyGuard)
    assert captured["model"] == platform.DEFAULT_MODEL and captured["effort"] == "medium"


def test_reader_stream_async_is_capped(monkeypatch):
    seen = {}

    class FakeAgent:
        async def stream_async(self, prompt, **kw):
            seen.update(kw)
            yield {"data": "ok"}

    monkeypatch.setattr(mod, "create_harness", lambda **kw: FakeAgent())
    from strands_harness.tools import AgentSpec

    spec = AgentSpec(
        task="q",
        agent_type="reader",
        instructions="x",
        tools=None,
        mcp_servers=None,
        model=None,
        context="none",
        last_messages=None,
    )
    child = mod.build_reader(spec)

    async def run():
        async for _ in child.stream_async("q"):
            pass

    asyncio.run(run())
    assert seen["limits"] == mod.READER_LIMITS


def test_reader_tool_is_named_reader():
    tool = mod.make_reader_tool()
    assert getattr(tool, "tool_name", getattr(tool, "name", "")) == "reader"
