from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from bedrock_agentcore.runtime.models import PingStatus
from ops_investigator import http_app, platform

SESSION = "s" * 33


def _result(stop_reason: str = "end_turn", cycles: int = 1) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, metrics=SimpleNamespace(cycle_count=cycles, accumulated_usage={}))


class FakeAgent:
    def __init__(self, events):
        self._events = events
        self.memory_manager = None
        self.session_manager = None

    async def stream_async(self, prompt, **kw):
        assert kw["limits"] == http_app.LIMITS
        for e in self._events:
            yield e


def _use(monkeypatch, agent) -> None:
    monkeypatch.setattr(http_app, "build_agent", lambda sid, actor: agent)
    http_app.CACHE.clear()


def _ctx(session_id: str = SESSION, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id, request_headers=headers, request=None)


async def _collect(gen) -> list[dict]:
    return [frame async for frame in gen]


def test_limits_are_the_agent_limits():
    assert http_app.MAX_PROMPT_BYTES == 32_768 and http_app.MIN_SESSION_LEN == 33
    assert http_app.MAX_SECONDS == 600.0


def test_invoke_is_the_registered_entrypoint():
    assert http_app.app.handlers["main"] is http_app.invoke


def test_validate_payload_ok():
    assert http_app.validate_payload({"prompt": "hi"}) == {"prompt": "hi", "actor_id": None}
    assert http_app.validate_payload({"prompt": "hi", "actor_id": "x"})["actor_id"] == "x"


@pytest.mark.parametrize(
    "bad",
    [None, "str", ["prompt"], {"prompt": 5}, {"prompt": "x", "extra": 1}, {"prompt": "p" * 40000}, {}, {"prompt": " "}],
)
def test_validate_payload_rejects(bad):
    with pytest.raises(http_app.PayloadError):
        http_app.validate_payload(bad)


def test_validate_payload_rejects_non_string_actor():
    with pytest.raises(http_app.PayloadError):
        http_app.validate_payload({"prompt": "hi", "actor_id": 7})


async def test_agent_cache_actor_mismatch_and_lru():
    cache = http_app.AgentCache(maxsize=2)
    a = await cache.get_or_create("s1", "igor", object)
    assert await cache.get_or_create("s1", "igor", object) is a
    with pytest.raises(http_app.ActorMismatch):
        await cache.get_or_create("s1", "someone-else", object)
    await cache.get_or_create("s2", "igor", object)
    await cache.get_or_create("s3", "igor", object)
    assert "s1" not in cache and "s2" in cache and "s3" in cache


async def test_agent_cache_builds_off_the_event_loop():
    """P: the cloud session manager does blocking ListEvents/CreateEvent in its constructor (T6 ruling)."""
    seen: dict[str, threading.Thread] = {}

    def factory():
        seen["thread"] = threading.current_thread()
        return object()

    await http_app.AgentCache().get_or_create("s1", "igor", factory)
    assert seen["thread"] is not threading.main_thread()


async def test_agent_cache_builds_once_when_invokes_race():
    built = []

    def factory():
        built.append(1)
        return object()

    cache = http_app.AgentCache()
    agents = await asyncio.gather(*(cache.get_or_create("s1", "igor", factory) for _ in range(4)))
    assert len(built) == 1 and len({id(a) for a in agents}) == 1


async def test_invoke_streams_frames_and_metrics(monkeypatch):
    _use(monkeypatch, FakeAgent([{"data": "hi"}, {"result": _result()}]))
    frames = await _collect(http_app.invoke({"prompt": "hello"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["text", "metrics", "done"]
    assert all(f["v"] == http_app.sse.SCHEMA_VERSION for f in frames)
    assert frames[0] == {"v": 1, "type": "text", "delta": "hi"}


async def test_invoke_uses_one_event_mapper_per_turn(monkeypatch):
    """A shared mapper dedupes tool_use frames and names tool_result frames (T7 ruling)."""
    tool_use = {"type": "tool_use_stream", "current_tool_use": {"toolUseId": "t1", "name": "aws_call"}}
    events = [
        {**tool_use, "current_tool_use": {**tool_use["current_tool_use"], "input": '{"service":'}},
        {**tool_use, "current_tool_use": {**tool_use["current_tool_use"], "input": '{"service": "ec2"}'}},
        {**tool_use, "current_tool_use": {**tool_use["current_tool_use"], "input": '{"service": "ec2"}'}},
        {"type": "tool_result", "tool_result": {"toolUseId": "t1", "status": "success", "content": [{"text": "x"}]}},
        {"result": _result()},
    ]
    _use(monkeypatch, FakeAgent(events))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["tool_use", "tool_result", "metrics", "done"]
    assert frames[0]["input_keys"] == ["service"]
    assert frames[1]["name"] == "aws_call" and frames[1]["bytes"] == 1


async def test_invoke_bad_payload_yields_error():
    frames = await _collect(http_app.invoke({"nope": 1}, _ctx()))
    assert frames[0]["type"] == "error" and "prompt" in frames[0]["message"]


async def test_invoke_short_session_rejected():
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(session_id="short")))
    assert frames[0]["type"] == "error" and "33" in frames[0]["hint"]


async def test_invoke_cloud_without_authorization_yields_error(monkeypatch, cloud_env):
    monkeypatch.setattr(http_app, "build_agent", lambda sid, actor: pytest.fail("must not build an agent"))
    http_app.CACHE.clear()
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx()))
    assert frames[0]["type"] == "error" and "Authorization" in frames[0]["message"]


async def test_invoke_actor_mismatch(monkeypatch):
    _use(monkeypatch, FakeAgent([{"result": _result()}]))
    ctx = _ctx(headers={"X-Actor-Id": "igor"})
    await _collect(http_app.invoke({"prompt": "x"}, ctx))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "someone-else"})))
    assert frames == [{"v": 1, "type": "error", "message": "actor_mismatch", "hint": frames[0]["hint"]}]
    assert "session" in frames[0]["hint"]


async def test_invoke_agent_construction_failure_fails_closed(monkeypatch):
    def boom(session_id, actor_id):
        raise RuntimeError("AccessDeniedException on CreateEvent")

    monkeypatch.setattr(http_app, "build_agent", boom)
    http_app.CACHE.clear()
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert frames[0]["type"] == "error" and frames[0]["message"] == "agent unavailable: RuntimeError"
    assert SESSION not in http_app.CACHE


async def test_invoke_busy_session_rejected(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class SlowAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            started.set()
            await release.wait()
            yield {"result": _result()}

    _use(monkeypatch, SlowAgent([]))
    ctx = _ctx(headers={"X-Actor-Id": "igor"})
    first = asyncio.create_task(_collect(http_app.invoke({"prompt": "x"}, ctx)))
    await started.wait()
    frames = await _collect(http_app.invoke({"prompt": "x"}, ctx))
    assert frames[0]["type"] == "error" and frames[0]["message"] == "busy"
    release.set()
    assert [f["type"] for f in await first] == ["metrics", "done"]


async def test_invoke_limit_trip_runs_closing_turn(monkeypatch):
    calls = []

    class LimitAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            calls.append(kw["limits"])
            if len(calls) == 1:
                yield {"result": _result("limit_turns", cycles=25)}
            else:
                yield {"data": "summary"}
                yield {"result": _result()}

    _use(monkeypatch, LimitAgent([]))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["metrics", "error", "text", "done"]
    assert frames[0]["stop_reason"] == "limit_turns"
    assert "limit_turns" in frames[1]["message"] and frames[1]["hint"]
    assert calls[0] == http_app.LIMITS
    assert calls[1] == {
        "turns": 1,
        "output_tokens": http_app.LIMITS["output_tokens"],
        "total_tokens": http_app.LIMITS["total_tokens"],
    }


async def test_invoke_wall_clock(monkeypatch):
    class SlowAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            await asyncio.sleep(5)
            yield {"data": "late"}

    _use(monkeypatch, SlowAgent([]))
    monkeypatch.setattr(http_app, "MAX_SECONDS", 0.05)
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert frames[-2]["type"] == "error" and "timeout" in frames[-2]["message"]
    assert frames[-1]["type"] == "done"


async def test_invoke_stream_failure_yields_error_then_done(monkeypatch):
    class BrokenAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            raise RuntimeError("model blew up")
            yield {}  # pragma: no cover - unreachable, keeps this an async generator

    _use(monkeypatch, BrokenAgent([]))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["error", "done"]
    assert frames[0]["message"].startswith("RuntimeError:")


def test_startup_chdirs_to_the_app_dir_locally(monkeypatch, tmp_path):
    """The harness `environment` plugin reads AGENTS.md from cwd, and local runs start at the repo root."""
    monkeypatch.chdir(tmp_path)
    http_app.startup()
    assert Path.cwd() == platform.APP_DIR


def test_startup_sends_this_package_to_info(monkeypatch, tmp_path):
    """The preflight lines are the runtime's only diagnostics, and nothing else installs a handler."""
    monkeypatch.chdir(tmp_path)
    logging.getLogger("ops_investigator").setLevel(logging.NOTSET)
    http_app.startup()
    assert logging.getLogger("ops_investigator").getEffectiveLevel() == logging.INFO
    assert logging.getLogger("botocore").getEffectiveLevel() >= logging.WARNING


def test_startup_forces_healthy_busy_when_preflight_fails(monkeypatch):
    forced = []
    monkeypatch.setattr(http_app, "run_preflight", lambda: SimpleNamespace(ok=False, checks=[]))
    monkeypatch.setattr(http_app.app, "force_ping_status", forced.append)
    http_app.startup()
    assert forced == [PingStatus.HEALTHY_BUSY]
