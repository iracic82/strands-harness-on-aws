from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace

import httpx
import pytest
from a2a.helpers import new_text_message
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import Message, Role, SendMessageRequest, TaskState
from google.protobuf.json_format import MessageToDict
from ops_investigator import a2a_app, agent, platform

CONTEXT = "c" * 36  # what the SDK generates: a uuid4, comfortably over AgentCore's 33-char floor
VERSION_HEADERS = {"A2A-Version": "1.0"}


def _result(stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, metrics=SimpleNamespace(cycle_count=1, accumulated_usage={}))


class FakeAgent:
    """Yields the harness event shapes the executor cares about: `{"data": ...}` deltas then the result."""

    def __init__(self, chunks: tuple[str, ...] = ("Findings: ", "all good"), delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.prompts: list[str] = []
        self.active = 0
        self.max_active = 0
        self.closed = 0

    async def stream_async(self, prompt, **kw):
        assert kw["limits"] is a2a_app.LIMITS
        self.prompts.append(prompt)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield {"data": chunk}
                yield {"type": "not-a-text-delta"}  # non-text events must not reach the artifact
            yield {"result": _result()}
        finally:
            self.active -= 1
            self.closed += 1


class Factory:
    """Stands in for `build_agent`, recording how and where it was called."""

    def __init__(self, agent: FakeAgent | None = None, error: Exception | None = None) -> None:
        self.agent = agent if agent is not None else FakeAgent()
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.threads: list[threading.Thread] = []

    def __call__(self, context_id: str, actor: str) -> FakeAgent:
        self.calls.append((context_id, actor))
        self.threads.append(threading.current_thread())
        if self.error:
            raise self.error
        return self.agent


class RecordingQueue:
    """The executor only ever enqueues, so the whole EventQueue surface it uses is this one method."""

    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


@contextlib.asynccontextmanager
async def _serve(factory: Factory, *, discovery_url: str | None = None):
    app = a2a_app.build_app(public_url="http://testserver", discovery_url=discovery_url, agent_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _send_body(text: str, context_id: str | None = CONTEXT, request_id: str = "1") -> dict:
    """Built with the SDK so the wire spelling of role/parts is the SDK's, not a guess."""
    message = new_text_message(text, role=Role.ROLE_USER)
    if context_id is not None:
        message.context_id = context_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": MessageToDict(SendMessageRequest(message=message)),
    }


async def _send(client, text: str = "what is broken?", *, context_id: str | None = CONTEXT, headers=None) -> dict:
    response = await client.post("/", json=_send_body(text, context_id), headers={**VERSION_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body["error"]
    return body["result"]["task"]


def _state(task: dict) -> str:
    return task["status"]["state"]


def _status_text(task: dict) -> str:
    return "".join(part["text"] for part in task["status"]["message"]["parts"])


def _artifact_text(task: dict) -> str:
    return "".join(part["text"] for art in task.get("artifacts", []) for part in art["parts"])


def _request_context(
    text: str = "what is broken?", *, context_id: str = CONTEXT, headers: dict | None = None, message=None
) -> RequestContext:
    message = message if message is not None else new_text_message(text, role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)
    return RequestContext(
        call_context=ServerCallContext(state={"headers": headers or {}}),
        request=request,
        context_id=context_id,
    )


def test_constants():
    assert a2a_app.MIN_CONTEXT_LEN == 33
    assert a2a_app.DEFAULT_MAX_SECONDS == 600.0
    assert a2a_app.LIMITS is agent.LIMITS


def test_max_seconds_falls_back_when_the_env_value_is_not_a_number():
    assert a2a_app._max_seconds("42.5") == 42.5
    assert a2a_app._max_seconds("ten minutes") == a2a_app.DEFAULT_MAX_SECONDS


async def test_well_known_card_is_served_as_spec_1_0():
    async with _serve(Factory()) as client:
        response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["url"] == "http://testserver"
    assert "protocolVersion" not in card and "supportsAuthenticatedExtendedCard" not in card


async def test_ping_reports_healthy():
    """AgentCore's health contract applies to every protocol the container serves."""
    async with _serve(Factory()) as client:
        response = await client.get("/ping")
    assert response.status_code == 200 and response.json() == {"status": "Healthy"}


async def test_send_message_returns_a_completed_task_with_the_answer_artifact():
    factory = Factory()
    async with _serve(factory) as client:
        task = await _send(client, "what is broken?")
    assert _state(task) == "TASK_STATE_COMPLETED"
    assert _artifact_text(task) == "Findings: all good"
    assert [art["name"] for art in task["artifacts"]] == ["answer"]
    assert factory.agent.prompts == ["what is broken?"]
    assert task["contextId"] == CONTEXT


async def test_a_0_3_client_is_answered_with_an_error_not_a_turn():
    """P22: this runtime is 1.0 only. 0.3 spells the method `message/send` and sends no version header;
    1.0 spells it `SendMessage` and requires `A2A-Version: 1.0`."""
    body = _send_body("what is broken?")
    async with _serve(Factory()) as client:
        old_method = await client.post("/", json={**body, "method": "message/send"}, headers=VERSION_HEADERS)
        no_version = await client.post("/", json=body)
    assert old_method.json()["error"]["code"] == -32601
    assert "1.0" in no_version.json()["error"]["message"]


async def test_the_agent_is_built_off_the_event_loop_with_the_context_id_as_session_id():
    """The cloud session manager runs ListEvents/CreateEvent in its constructor (P14)."""
    factory = Factory()
    async with _serve(factory) as client:
        await _send(client, headers={"X-Actor-Id": "igor"})
    assert factory.calls == [(CONTEXT, "igor")]
    assert factory.threads[0] is not threading.main_thread()


async def test_one_agent_is_reused_across_turns_on_the_same_context():
    factory = Factory()
    async with _serve(factory) as client:
        await _send(client, "first")
        await _send(client, "second")
    assert len(factory.calls) == 1
    assert factory.agent.prompts == ["first", "second"]


async def test_turns_on_one_context_do_not_interleave():
    factory = Factory(FakeAgent(delay=0.02))
    async with _serve(factory) as client:
        tasks = await asyncio.gather(_send(client, "a"), _send(client, "b"))
    assert [_state(t) for t in tasks] == ["TASK_STATE_COMPLETED"] * 2
    assert factory.agent.max_active == 1


async def test_a_second_actor_on_the_same_context_is_rejected():
    """A context is one conversation; in the local profile its session file is keyed by context id alone."""
    factory = Factory()
    async with _serve(factory) as client:
        first = await _send(client, headers={"X-Actor-Id": "igor"})
        second = await _send(client, headers={"X-Actor-Id": "someone-else"})
    assert _state(first) == "TASK_STATE_COMPLETED"
    assert _state(second) == "TASK_STATE_REJECTED"
    assert "actor" in _status_text(second)
    assert len(factory.calls) == 1


async def test_a_short_context_id_is_rejected():
    factory = Factory()
    async with _serve(factory) as client:
        task = await _send(client, context_id="short")
    assert _state(task) == "TASK_STATE_REJECTED"
    assert "33" in _status_text(task)
    assert factory.calls == []


async def test_the_cloud_profile_rejects_a_turn_with_no_bearer_token(monkeypatch):
    monkeypatch.setenv(platform.MEMORY_ENV, "mem-test-123")
    factory = Factory()
    async with _serve(factory) as client:
        task = await _send(client)
    assert _state(task) == "TASK_STATE_REJECTED"
    assert "Authorization" in _status_text(task)
    assert factory.calls == []


async def test_a_failed_build_fails_the_task_instead_of_raising():
    factory = Factory(error=RuntimeError("memory AccessDenied"))
    async with _serve(factory) as client:
        task = await _send(client)
    assert _state(task) == "TASK_STATE_FAILED"
    assert "RuntimeError" in _status_text(task)
    assert "AccessDenied" not in _status_text(task)  # provider text stays in the logs


async def test_a_failing_stream_fails_the_task_instead_of_raising():
    class BrokenAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            yield {"data": "partial"}
            raise ValueError("boom")

    async with _serve(Factory(BrokenAgent())) as client:
        task = await _send(client)
    assert _state(task) == "TASK_STATE_FAILED"
    assert "ValueError" in _status_text(task)


async def test_the_wall_clock_fails_the_task_and_closes_the_stream(monkeypatch):
    monkeypatch.setattr(a2a_app, "MAX_SECONDS", 0.05)
    factory = Factory(FakeAgent(chunks=("tick",) * 50, delay=0.01))
    async with _serve(factory) as client:
        task = await _send(client)
    assert _state(task) == "TASK_STATE_FAILED"
    assert "timeout" in _status_text(task)
    assert factory.agent.closed == 1  # the agent stream was closed, not abandoned mid-flight


async def test_a_stalled_first_pull_still_times_out(monkeypatch):
    """The wall clock bounds each pull, not just the gaps between them: an agent that hangs before its
    first event would never reach a loop-top deadline check."""

    class StalledAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            await asyncio.sleep(3600)
            yield {"data": "never"}  # pragma: no cover

    monkeypatch.setattr(a2a_app, "MAX_SECONDS", 0.05)
    async with _serve(Factory(StalledAgent())) as client:
        task = await asyncio.wait_for(_send(client), 5)  # a regression here hangs; fail instead
    assert _state(task) == "TASK_STATE_FAILED"
    assert "timeout" in _status_text(task)


async def test_the_timeout_does_not_swallow_someone_elses_timeout(monkeypatch):
    class StalledAgent(FakeAgent):
        async def stream_async(self, prompt, **kw):
            raise asyncio.TimeoutError("a boto read timed out")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(a2a_app, "MAX_SECONDS", 600.0)
    async with _serve(Factory(StalledAgent())) as client:
        task = await _send(client)
    assert _state(task) == "TASK_STATE_FAILED"
    assert "timeout after" not in _status_text(task)


async def test_an_unusable_message_is_rejected_not_raised():
    """`new_task_from_user_message` raises on an empty message; a rejected task is the A2A answer."""
    executor = a2a_app.HarnessExecutor(Factory())
    queue = RecordingQueue()
    await executor.execute(_request_context(message=Message(role=Role.ROLE_USER)), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_REJECTED


async def test_cancel_marks_the_task_canceled():
    queue = RecordingQueue()
    await a2a_app.HarnessExecutor(Factory()).cancel(_request_context(), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_CANCELED


def test_build_app_advertises_the_configured_urls():
    app = a2a_app.build_app(public_url="https://run.example", discovery_url="https://cognito/openid")
    paths = {route.path for route in app.routes}
    assert {"/", "/ping", "/.well-known/agent-card.json"} <= paths


@pytest.mark.parametrize("env,expected", [({}, "local"), ({platform.MEMORY_ENV: "mem-1"}, "cloud")])
def test_profile_is_read_per_request(env, expected):
    assert a2a_app._profile(env) == expected
