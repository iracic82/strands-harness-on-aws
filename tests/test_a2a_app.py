from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.helpers import new_text_message, new_text_part
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import Message, Role, SendMessageRequest, TaskState
from google.protobuf.json_format import MessageToDict
from ops_investigator import a2a_app, agent, agent_cache, platform, runtime_common

CONTEXT = "c" * 36  # what the SDK generates: a uuid4, comfortably over AgentCore's 33-char floor
VERSION_HEADERS = {"A2A-Version": "1.0"}


@pytest.fixture(autouse=True)
def _clear_cache():
    """The agent cache is process-wide and keyed by context id, which these tests reuse."""
    agent_cache.CACHE.clear()
    yield
    agent_cache.CACHE.clear()


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


class BudgetAgent:
    """First turn stops on a turn-limit; the closing turn then summarises what it found (spec 4.4)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.limits: list[Any] = []

    async def stream_async(self, prompt, **kw):
        self.prompts.append(prompt)
        self.limits.append(kw["limits"])
        if len(self.prompts) == 1:
            yield {"data": "partial findings"}
            yield {"result": _result("limit_turns")}
        else:
            yield {"data": "and here is what remains unchecked"}
            yield {"result": _result()}


class Factory:
    """Stands in for `build_agent`, recording how and where it was called."""

    def __init__(self, agent: Any = None, error: Exception | None = None) -> None:
        self.agent = agent if agent is not None else FakeAgent()
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.threads: list[threading.Thread] = []

    def __call__(self, context_id: str, actor: str) -> Any:
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


def _send_body(text: str, context_id: str | None = CONTEXT, message=None, method: str = "SendMessage") -> dict:
    """Built with the SDK so the wire spelling of role/parts is the SDK's, not a guess."""
    message = new_text_message(text, role=Role.ROLE_USER) if message is None else message
    if context_id is not None:
        message.context_id = context_id
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": method,
        "params": MessageToDict(SendMessageRequest(message=message)),
    }


async def _send(client, text: str = "what is broken?", *, context_id: str | None = CONTEXT, headers=None, message=None):
    body = _send_body(text, context_id, message)
    response = await client.post("/", json=body, headers={**VERSION_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload["error"]
    return payload["result"]["task"]


async def _stream(client, text: str = "what is broken?", *, context_id: str | None = CONTEXT) -> list[dict]:
    body = _send_body(text, context_id, method="SendStreamingMessage")
    events: list[dict] = []
    async with client.stream("POST", "/", json=body, headers=VERSION_HEADERS) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:])["result"])
    return events


def _state(task: dict) -> str:
    return task["status"]["state"]


def _status_text(task: dict) -> str:
    return "".join(part["text"] for part in task["status"]["message"]["parts"])


def _artifact_text(task: dict) -> str:
    return "".join(part["text"] for art in task.get("artifacts", []) for part in art["parts"])


def _request_context(text: str = "what is broken?", *, context_id: str = CONTEXT) -> RequestContext:
    return RequestContext(
        call_context=ServerCallContext(state={"headers": {}}),
        request=SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER)),
        context_id=context_id,
    )


def test_constants():
    assert a2a_app.MIN_SESSION_LEN == runtime_common.MIN_SESSION_LEN == 33
    assert a2a_app.DEFAULT_MAX_SECONDS == 600.0  # MAX_SECONDS itself is bound from the ambient env
    assert a2a_app.LIMITS is agent.LIMITS


def test_the_executor_keeps_no_agents_of_its_own():
    """One shared, bounded cache; a private dict here would be unbounded and never closed."""
    executor = a2a_app.HarnessExecutor(Factory())
    assert not hasattr(executor, "_agents") and not hasattr(executor, "_locks")


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


async def test_streaming_send_emits_submitted_working_artifact_completed():
    async with _serve(Factory()) as client:
        events = await _stream(client)
    assert [next(iter(event)) for event in events] == ["task", "statusUpdate", "artifactUpdate", "statusUpdate"]
    assert events[0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert events[1]["statusUpdate"]["status"]["state"] == "TASK_STATE_WORKING"
    assert events[2]["artifactUpdate"]["artifact"]["name"] == "answer"
    assert events[3]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_the_agent_is_built_off_the_event_loop_with_the_context_id_as_session_id():
    """The cloud session manager runs ListEvents/CreateEvent in its constructor (P14)."""
    factory = Factory()
    async with _serve(factory) as client:
        await _send(client, headers={"X-Actor-Id": "igor"})
    assert factory.calls == [(CONTEXT, "igor")]
    assert factory.threads[0] is not threading.main_thread()
    assert CONTEXT in agent_cache.CACHE  # the shared, bounded cache owns it, not the executor


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


async def test_a_queued_turn_is_bounded_by_the_same_budget(monkeypatch):
    """The deadline starts when the request arrives, so waiting for the lock cannot outlive the budget.
    An unbounded wait here would hang until whatever holds the lock lets go, budget or no budget."""
    monkeypatch.setattr(a2a_app, "MAX_SECONDS", 0.1)
    factory = Factory()
    async with _serve(factory) as client:
        await agent_cache.CACHE.get_or_create(CONTEXT, "local-dev", lambda: factory.agent)
        async with agent_cache.CACHE.lock(CONTEXT):  # a turn is already running on this context
            task = await asyncio.wait_for(_send(client), 5)  # a regression here hangs; fail instead
    assert _state(task) == "TASK_STATE_FAILED"
    assert "timeout" in _status_text(task)
    assert factory.agent.prompts == []  # the queued turn never reached the agent


async def test_concurrent_turns_on_one_context_both_finish():
    """The queue is a queue, not a rejection: the HTTP path answers `busy`, A2A waits its turn."""
    factory = Factory(FakeAgent(delay=0.01))
    async with _serve(factory) as client:
        tasks = await asyncio.gather(_send(client, "a"), _send(client, "b"))
    assert [_state(t) for t in tasks] == ["TASK_STATE_COMPLETED"] * 2
    assert sorted(factory.agent.prompts) == ["a", "b"]


async def test_budget_exhaustion_runs_the_closing_turn_and_says_so():
    """A `limit_*` stop must not reach the caller looking like a finished answer (spec 4.4)."""
    factory = Factory(BudgetAgent())
    async with _serve(factory) as client:
        task = await _send(client, "why is orders-api slow?")
    assert _state(task) == "TASK_STATE_COMPLETED"
    assert "partial findings" in _artifact_text(task)
    assert "and here is what remains unchecked" in _artifact_text(task)
    assert "budget exhausted" in _status_text(task) and "limit_turns" in _status_text(task)
    assert factory.agent.prompts[1] == runtime_common.CLOSING_PROMPT
    assert factory.agent.limits[1]["turns"] == 1


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


async def test_an_empty_prompt_is_rejected_before_anything_is_built():
    """A data-only message reaches `execute` with no text for the agent to act on."""
    factory = Factory()
    message = Message(role=Role.ROLE_USER, message_id="m1", parts=[new_text_part(" ")])
    async with _serve(factory) as client:
        task = await _send(client, message=message)
    assert _state(task) == "TASK_STATE_REJECTED"
    assert _status_text(task) == a2a_app.EMPTY_PROMPT
    assert factory.calls == []


async def test_an_unusable_message_is_rejected_not_raised():
    """`new_task_from_user_message` raises on a non-user role; a rejected task is the A2A answer."""
    factory = Factory()
    message = Message(role=Role.ROLE_AGENT, message_id="m1", parts=[new_text_part("hi")])
    async with _serve(factory) as client:
        task = await _send(client, message=message)
    assert _state(task) == "TASK_STATE_REJECTED"
    assert "unusable message" in _status_text(task)
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


async def test_cancel_marks_the_task_canceled():
    queue = RecordingQueue()
    await a2a_app.HarnessExecutor(Factory()).cancel(_request_context(), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_CANCELED


def test_build_app_advertises_the_configured_urls():
    app = a2a_app.build_app(public_url="https://run.example", discovery_url="https://cognito/openid")
    paths = {route.path for route in app.routes}
    assert {"/", "/ping", "/.well-known/agent-card.json"} <= paths


async def test_shutdown_closes_the_request_handler(monkeypatch):
    """Starlette 1.6 has no `on_shutdown`; the handler's background tasks are cancelled by the lifespan."""
    closed: list[bool] = []

    async def fake_aclose(_self) -> None:
        closed.append(True)

    monkeypatch.setattr(a2a_app.DefaultRequestHandler, "aclose", fake_aclose)
    app = a2a_app.build_app(public_url="http://testserver", discovery_url=None, agent_factory=Factory())
    async with app.router.lifespan_context(app):
        pass
    assert closed == [True]


def test_a_broken_preflight_never_stops_the_server(monkeypatch):
    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(a2a_app, "run_preflight", boom)
    a2a_app._log_preflight()
    monkeypatch.setattr(a2a_app, "run_preflight", lambda: SimpleNamespace(ok=False, checks=[]))
    a2a_app._log_preflight()
