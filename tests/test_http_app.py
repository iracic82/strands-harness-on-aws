from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from bedrock_agentcore.runtime.models import PingStatus
from ops_investigator import agent, http_app, platform, tool_events
from strands.hooks import AfterToolCallEvent

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


def test_constants():
    assert http_app.MAX_PROMPT_BYTES == 32_768 and http_app.MIN_SESSION_LEN == 33
    assert http_app.LIMITS is agent.LIMITS
    assert http_app.DEFAULT_MAX_SECONDS == 600.0  # MAX_SECONDS itself is bound from the ambient env


def test_max_seconds_falls_back_when_the_env_value_is_not_a_number():
    assert http_app._max_seconds("42.5") == 42.5
    assert http_app._max_seconds("ten minutes") == http_app.DEFAULT_MAX_SECONDS == 600.0


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
    a, lock = await cache.get_or_create("s1", "igor", object)
    assert await cache.get_or_create("s1", "igor", object) == (a, lock)
    assert lock is cache.lock("s1")
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
    results = await asyncio.gather(*(cache.get_or_create("s1", "igor", factory) for _ in range(4)))
    assert len(built) == 1 and len({id(a) for a, _ in results}) == 1


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


# --- tool_result frames come from the after-tool hook, not the stream: `ToolResultEvent` is not a
# callback event in strands 1.56, so a real `stream_async` never yields one (P51). ---


def _finished(tool_use_id: str = "t1", name: str = "aws_call", status: str = "success", text: str = "hello"):
    return AfterToolCallEvent(
        agent=None,
        selected_tool=None,
        tool_use={"toolUseId": tool_use_id, "name": name},
        invocation_state={},
        result={"toolUseId": tool_use_id, "status": status, "content": [{"text": text}]},
    )


FIRE = object()  # a marker in RecordingAgent's event list: "the tool finished here"


class RecordingAgent(FakeAgent):
    """Runs the hook mid-stream, where a real tool call would: after its `tool_use_stream` deltas."""

    def __init__(self, events, finished, after_last_event=False):
        super().__init__(events)
        self._finished = finished
        self._after_last_event = after_last_event
        self.recorder = tool_events.ToolResultRecorder()
        tool_events.attach(self, self.recorder)

    async def stream_async(self, prompt, **kw):
        assert kw["limits"] == http_app.LIMITS
        for event in self._events:
            if event is FIRE:
                self.recorder._on_after_tool(self._finished)
                continue
            yield event
        if self._after_last_event:
            self.recorder._on_after_tool(self._finished)


async def test_invoke_emits_a_tool_result_frame_from_the_after_tool_hook(monkeypatch):
    tool_use = {"toolUseId": "t1", "name": "aws_call", "input": '{"service": "sts"}'}
    events = [
        {"type": "tool_use_stream", "current_tool_use": tool_use},
        FIRE,
        {"data": "account 123456789012"},
        {"result": _result()},
    ]
    agent = RecordingAgent(events, _finished())
    _use(monkeypatch, agent)
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    # The result lands before the text that followed it, not at the end of the turn.
    assert [f["type"] for f in frames] == ["tool_use", "tool_result", "text", "metrics", "done"]
    assert frames[1] == {"v": 1, "type": "tool_result", "name": "aws_call", "status": "success", "bytes": 5}
    assert agent.recorder.drain() == []  # the stream loop drained it; nothing accumulates on the agent


async def test_invoke_emits_a_tool_result_recorded_after_the_last_stream_event(monkeypatch):
    """The hook can fire after the stream is done, so the loop drains once more before metrics."""
    agent = RecordingAgent([{"result": _result()}], _finished(status="error", text="DENIED"), after_last_event=True)
    _use(monkeypatch, agent)
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["tool_result", "metrics", "done"]
    assert frames[0]["status"] == "error" and frames[0]["name"] == "aws_call"


async def test_a_turn_discards_tool_results_left_behind_by_an_abandoned_turn():
    """A disconnect closes the stream at a `yield`, before its last drain: those results are that turn's."""
    agent = RecordingAgent([{"result": _result()}], _finished())
    agent.recorder._on_after_tool(_finished())  # left over from the turn that was abandoned
    frames = [frame async for frame in http_app._turn(agent, "x", http_app.LIMITS, [])]
    assert frames == []


async def test_an_abandoned_turn_frees_its_buffer_without_waiting_for_the_next_one():
    """A turn cancelled mid-tool is closed at a pull, after its last drain ran. The `finally` frees the
    buffer there and then, rather than leaving it on the cached agent until something opens a turn."""

    class StallingAgent(RecordingAgent):
        async def stream_async(self, prompt, **kw):
            yield {"data": "thinking"}
            self.recorder._on_after_tool(self._finished)
            await asyncio.Event().wait()  # the tool finished; the turn is cancelled before the next event

    agent = StallingAgent([], _finished())
    turn = http_app._turn(agent, "x", http_app.LIMITS, []).__aiter__()
    assert await turn.__anext__() == {"v": 1, "type": "text", "delta": "thinking"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(turn.__anext__(), 0.05)
    assert agent.recorder.drain() == []


async def test_invoke_works_for_an_agent_with_no_recorder(monkeypatch):
    """`recorder_for` returns None for any agent not built by `build_agent` (P51)."""
    _use(monkeypatch, FakeAgent([{"data": "hi"}, {"result": _result()}]))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["text", "metrics", "done"]


async def test_invoke_bad_payload_yields_error():
    frames = await _collect(http_app.invoke({"nope": 1}, _ctx()))
    assert frames[0]["type"] == "error" and "prompt" in frames[0]["message"]
    assert frames[-1]["type"] == "done"


async def test_invoke_short_session_rejected():
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(session_id="short")))
    assert frames[0]["type"] == "error" and "33" in frames[0]["hint"]
    assert frames[-1]["type"] == "done"


async def test_invoke_cloud_without_authorization_yields_error(monkeypatch, cloud_env):
    monkeypatch.setattr(http_app, "build_agent", lambda sid, actor: pytest.fail("must not build an agent"))
    http_app.CACHE.clear()
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx()))
    assert frames[0]["type"] == "error" and "Authorization" in frames[0]["message"]
    assert frames[-1]["type"] == "done"


async def test_invoke_actor_mismatch(monkeypatch):
    _use(monkeypatch, FakeAgent([{"result": _result()}]))
    ctx = _ctx(headers={"X-Actor-Id": "igor"})
    await _collect(http_app.invoke({"prompt": "x"}, ctx))
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "someone-else"})))
    assert frames == [
        {"v": 1, "type": "error", "message": "actor_mismatch", "hint": frames[0]["hint"]},
        {"v": 1, "type": "done"},
    ]
    assert "session" in frames[0]["hint"]


async def test_invoke_agent_construction_failure_fails_closed(monkeypatch):
    def boom(session_id, actor_id):
        raise RuntimeError("AccessDeniedException on CreateEvent")

    monkeypatch.setattr(http_app, "build_agent", boom)
    http_app.CACHE.clear()
    frames = await _collect(http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})))
    assert [f["type"] for f in frames] == ["error", "done"]
    assert frames[0]["message"] == "agent unavailable: RuntimeError"
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
    assert [f["type"] for f in frames] == ["error", "done"] and frames[0]["message"] == "busy"
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


async def test_invoke_wall_clock_with_a_slow_consumer(monkeypatch):
    """The deadline must never span a `yield`: a slow consumer would otherwise take the cancellation,
    skip the error frame, and leave the turn lock held so every later request answers `busy`."""

    class ChattyAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            for _ in range(50):
                await asyncio.sleep(0.01)
                yield {"data": "tick"}
            yield {"result": _result()}

    _use(monkeypatch, ChattyAgent([]))
    monkeypatch.setattr(http_app, "MAX_SECONDS", 0.05)
    frames = []
    async for frame in http_app.invoke({"prompt": "x"}, _ctx(headers={"X-Actor-Id": "igor"})):
        frames.append(frame)
        await asyncio.sleep(0.02)  # a consumer that is slower than the agent, e.g. a stalled SSE client
    assert [f["type"] for f in frames[-2:]] == ["error", "done"]
    assert "timeout" in frames[-2]["message"]
    assert not http_app.CACHE.lock(SESSION).locked()


async def test_cache_never_evicts_a_session_that_is_mid_turn():
    closed = []

    def agent_for(name):
        return SimpleNamespace(session_manager=SimpleNamespace(close=lambda: closed.append(name)))

    cache = http_app.AgentCache(maxsize=1)
    await cache.get_or_create("busy-session", "igor", lambda: agent_for("busy-session"))
    async with cache.lock("busy-session"):  # a turn is running on it
        await cache.get_or_create("s2", "igor", lambda: agent_for("s2"))
        assert "busy-session" in cache and "s2" in cache and closed == []  # over maxsize, nothing killed
    await cache.get_or_create("s3", "igor", lambda: agent_for("s3"))
    await cache.drain_closes()
    assert closed == ["busy-session", "s2"]  # free again, so both trim away in LRU order
    assert "busy-session" not in cache and list(cache._items) == ["s3"]


async def test_cache_does_not_evict_a_concurrent_caller_while_closing_a_victim():
    """Closing an evicted agent must not open a window in which a second caller evicts the entry the first
    caller was just handed: that caller would stream on an agent the cache no longer knows about."""

    def sleepy_close():
        time.sleep(0.05)  # a real session manager flushes to AgentCore here

    def plain():
        return SimpleNamespace(session_manager=None)

    cache = http_app.AgentCache(maxsize=2)
    await cache.get_or_create("locked", "igor", plain)
    victim = SimpleNamespace(session_manager=SimpleNamespace(close=sleepy_close))
    await cache.get_or_create("victim", "igor", lambda: victim)

    async def turn(session_id):
        agent, lock = await cache.get_or_create(session_id, "igor", plain)
        async with lock:  # exactly what _handle does, with no await between the two
            await asyncio.sleep(0.02)
            return session_id in cache, agent is not None

    async with cache.lock("locked"):  # one entry is mid-turn for the whole test
        results = await asyncio.gather(turn("n1"), turn("n2"))
    assert results == [(True, True), (True, True)]
    assert "victim" not in cache
    await cache.drain_closes()


async def test_cache_closes_evicted_agents_off_the_event_loop():
    seen = {}

    def agent_for():
        return SimpleNamespace(session_manager=SimpleNamespace(close=lambda: seen.update(t=threading.current_thread())))

    cache = http_app.AgentCache(maxsize=1)
    await cache.get_or_create("s1", "igor", agent_for)
    await cache.get_or_create("s2", "igor", agent_for)
    await cache.drain_closes()  # the close is a background task now, not awaited inline
    assert seen["t"] is not threading.main_thread()


async def test_invoke_releases_the_turn_lock_when_the_consumer_disconnects(monkeypatch):
    """A client that walks away mid-stream must not leave the session answering `busy` forever."""

    class ChattyAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            for _ in range(20):
                await asyncio.sleep(0.01)
                yield {"data": "tick"}
            yield {"result": _result()}

    _use(monkeypatch, ChattyAgent([]))
    ctx = _ctx(headers={"X-Actor-Id": "igor"})
    gen = http_app.invoke({"prompt": "x"}, ctx)
    assert (await gen.__anext__())["type"] == "text"
    assert http_app.CACHE.lock(SESSION).locked()
    await gen.aclose()
    assert not http_app.CACHE.lock(SESSION).locked()
    frames = await _collect(http_app.invoke({"prompt": "x"}, ctx))
    assert [f["type"] for f in frames[-2:]] == ["metrics", "done"]
    assert not any(f["type"] == "error" for f in frames)


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
