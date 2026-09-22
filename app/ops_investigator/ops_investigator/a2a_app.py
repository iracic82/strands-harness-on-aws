"""A2A 1.0 server over the same `build_agent()` (spec 4.5).

One A2A context is one conversation: its context id is the AgentCore session id, its agent comes from
the shared `agent_cache.CACHE`, and that cache's per-session turn lock keeps two `SendMessage` calls
from interleaving on one session. A2A callers queue for the lock rather than being told "busy" — there
is no SSE stream to lose — but the wall clock starts when the request arrives, so the wait is inside
the same budget. Every failure is reported as a task state, never as a transport error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import uvicorn
from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import Task, TaskState, TaskStatus
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ops_investigator import tool_events
from ops_investigator.agent import LIMITS, build_agent
from ops_investigator.agent_cache import CACHE, ActorMismatch
from ops_investigator.agent_card import build_agent_card
from ops_investigator.auth import ActorError, actor_from_headers
from ops_investigator.preflight import run_preflight
from ops_investigator.runtime_common import (
    CLOSING_PROMPT,
    DEFAULT_MAX_SECONDS,
    MIN_SESSION_LEN,
    configure_logging,
    enter_local_app_dir,
    max_seconds,
    profile,
)

log = logging.getLogger("ops_investigator.a2a")

__all__ = ["DEFAULT_MAX_SECONDS", "MIN_SESSION_LEN", "HarnessExecutor", "build_app", "main"]

PORT = 9000
DEFAULT_PUBLIC_URL = "http://localhost:9000"
RPC_PATH = "/"
WORKING_MESSAGE = "Investigating..."
NO_ANSWER = "(no answer)"
ANSWER_ARTIFACT = "answer"
EMPTY_PROMPT = "message has no text to act on"

MAX_SECONDS = max_seconds()


def _headers(context: RequestContext) -> dict[str, str]:
    """`DefaultServerCallContextBuilder` stores `dict(request.headers)` in the call context state (P24)."""
    state = getattr(getattr(context, "call_context", None), "state", None)
    headers = state.get("headers") if isinstance(state, dict) else None
    return dict(headers or {})


def _task_for(context: RequestContext) -> tuple[Task, str | None]:
    """The task to report on, plus the reason it cannot be run (None when the message is usable).

    `new_task_from_user_message` raises on a message with no parts, an empty text part, or a non-user
    role. The A2A answer to that is a rejected task, so build a bare one to carry the rejection.
    """
    if context.current_task:
        return context.current_task, None
    try:
        return new_task_from_user_message(context.message), None
    except (AttributeError, ValueError) as e:
        task = Task(
            id=context.task_id or "",
            context_id=context.context_id or "",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )
        return task, f"unusable message: {e}"


async def _reject(updater: TaskUpdater, reason: str) -> None:
    await updater.update_status(TaskState.TASK_STATE_REJECTED, message=new_text_message(reason))


async def _fail_on_timeout(updater: TaskUpdater) -> None:
    await updater.update_status(
        TaskState.TASK_STATE_FAILED, message=new_text_message(f"timeout after {MAX_SECONDS:.0f}s")
    )


async def _text_chunks(agent: Any, prompt: str, limits: dict, results: list[Any]) -> AsyncIterator[str]:
    """Only the assistant's text deltas: the artifact is the answer, not the raw harness event stream.

    The AgentResult arrives as the last stream event rather than as a chunk, and an async generator
    cannot return a value, so it lands in `results` (the same shape `http_app._turn` uses).
    """
    # A2A publishes one text artifact, so a finished tool call has no frame to land in here the way it
    # does in `http_app._turn`. Drain the after-tool hook's buffer anyway (P51), on every event and once
    # more when the turn ends, or it grows for the life of the cached agent.
    recorder = tool_events.recorder_for(agent)
    try:
        async for event in agent.stream_async(prompt, limits=limits):
            if recorder is not None:
                recorder.drain()
            if not isinstance(event, dict):
                continue
            if "result" in event:
                results.append(event["result"])
            elif isinstance(event.get("data"), str):
                yield event["data"]
    finally:
        if recorder is not None:
            recorder.drain()


async def _collect(agent: Any, prompt: str, deadline: float, limits: dict) -> tuple[str, str, bool]:
    """Drain one turn under the wall clock; returns the text, the stop reason, and whether time ran out.

    The deadline is applied to each pull rather than wrapped around the loop: `asyncio.timeout` does not
    exist on Python 3.10 (the floor), and there `asyncio.TimeoutError` is not the builtin `TimeoutError`.
    """
    loop = asyncio.get_running_loop()
    chunks: list[str] = []
    results: list[Any] = []
    timed_out = False
    stream = _text_chunks(agent, prompt, limits, results).__aiter__()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunks.append(await asyncio.wait_for(stream.__anext__(), remaining))
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:  # on 3.10 this is not the builtin TimeoutError
                if loop.time() < deadline:
                    raise  # someone else's timeout (a tool, a boto read) — report it as itself
                timed_out = True
                break
    finally:
        await stream.aclose()
    stop_reason = str(getattr(results[-1], "stop_reason", "")) if results else ""
    return "".join(chunks), stop_reason, timed_out


class HarnessExecutor(AgentExecutor):
    """Runs one A2A turn against the cached agent for the request's context."""

    def __init__(self, agent_factory: Callable[[str, str], Any] | None = None) -> None:
        self._factory = agent_factory or build_agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Never raises: an A2A caller learns what went wrong from the task state, not a transport error."""
        deadline = asyncio.get_running_loop().time() + MAX_SECONDS  # queueing is inside the budget
        task, unusable = _task_for(context)
        if task is not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        try:
            if unusable:
                await _reject(updater, unusable)
                return
            if len(task.context_id) < MIN_SESSION_LEN:
                await _reject(
                    updater,
                    f"context id must be at least {MIN_SESSION_LEN} characters: it is the "
                    "AgentCore session id, and the SDK's own uuid4 context ids qualify",
                )
                return
            prompt = get_message_text(context.message)
            if not prompt.strip():
                await _reject(updater, EMPTY_PROMPT)
                return
            try:
                actor = actor_from_headers(_headers(context), profile=profile(), payload_actor=None)
            except ActorError as e:
                await _reject(updater, str(e))
                return
            await self._turn(updater, task.context_id, actor, prompt, deadline)
        except ActorMismatch as e:
            await _reject(updater, str(e))
        except Exception as e:  # the task state is the only channel back to an A2A caller
            log.exception("a2a turn failed context=%s", task.context_id)
            with contextlib.suppress(Exception):  # a terminal state may already have been published
                await updater.update_status(TaskState.TASK_STATE_FAILED, message=new_text_message(type(e).__name__))

    async def _turn(self, updater: TaskUpdater, context_id: str, actor: str, prompt: str, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        try:
            agent, lock = await CACHE.get_or_create(context_id, actor, lambda: self._factory(context_id, actor))
        except ActorMismatch:
            raise  # execute turns this into a rejection
        except Exception as e:  # session manager init (memory AccessDenied etc.) fails closed (spec 5)
            log.exception("agent construction failed context=%s", context_id)
            await updater.update_status(
                TaskState.TASK_STATE_FAILED, message=new_text_message(f"agent unavailable: {type(e).__name__}")
            )
            return
        try:
            await asyncio.wait_for(lock.acquire(), max(deadline - loop.time(), 0.0))
        except asyncio.TimeoutError:  # the queue ahead of this turn used up its whole budget
            await _fail_on_timeout(updater)
            return
        try:
            await updater.update_status(TaskState.TASK_STATE_WORKING, message=new_text_message(WORKING_MESSAGE))
            log.info("a2a turn start context=%s actor=%s prompt_bytes=%d", context_id, actor, len(prompt.encode()))
            # One artifact at the end of the turn. Incremental artifact chunks are a follow-up; streaming
            # callers get the status events in the meantime.
            answer, stop_reason, timed_out = await _collect(agent, prompt, deadline, LIMITS)
            if not timed_out and stop_reason.startswith("limit_"):
                # The same closing turn the HTTP path runs (spec 4.4): a budget stop must not reach the
                # caller looking like a finished answer. Best effort — its own timeout must not sink
                # findings that are already in hand.
                extra, _, _ = await _collect(agent, CLOSING_PROMPT, deadline, dict(LIMITS, turns=1))
                answer = f"{answer}\n\n{extra}" if extra else answer
        finally:
            lock.release()
        if timed_out:
            await _fail_on_timeout(updater)
            return
        await updater.add_artifact(
            parts=[new_text_part(answer or NO_ANSWER, media_type="text/plain")], name=ANSWER_ARTIFACT
        )
        budget = (
            new_text_message(f"budget exhausted ({stop_reason}); findings are partial")
            if stop_reason.startswith("limit_")
            else None
        )
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, message=budget)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue=event_queue, task_id=context.task_id or "", context_id=context.context_id or ""
        )
        await updater.update_status(TaskState.TASK_STATE_CANCELED)


async def _ping(_request: Request) -> JSONResponse:
    """AgentCore's health contract applies to every protocol the container serves."""
    return JSONResponse({"status": "Healthy"})


def build_app(
    *, public_url: str, discovery_url: str | None, agent_factory: Callable[[str, str], Any] | None = None
) -> Starlette:
    card = build_agent_card(public_url=public_url, discovery_url=discovery_url)
    handler = DefaultRequestHandler(
        agent_executor=HarnessExecutor(agent_factory), task_store=InMemoryTaskStore(), agent_card=card
    )
    # `/ping` is a plain Route beside the SDK's: Starlette's `@app.route` decorator is deprecated.
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, RPC_PATH),
        Route("/ping", _ping, methods=["GET"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        """Starlette 1.6 dropped `on_shutdown`, so the shutdown half of the lifespan carries it: the
        handler cancels its background producer/consumer tasks, the cache flushes its session managers."""
        yield
        await handler.aclose()
        await CACHE.drain_closes()

    return Starlette(routes=routes, lifespan=lifespan)


def _log_preflight() -> None:
    """A2A has no ping-status enum to flip, so a failed preflight is logged loudly and serving continues:
    a server that refuses to start cannot tell anyone why."""
    try:
        report = run_preflight()
    except Exception:  # a broken self-check must never be the reason the server is down
        log.exception("preflight could not run")
        return
    if not report.ok:
        log.error("preflight failed; turns will error until it is fixed (see the preflight lines above)")


def main() -> None:
    configure_logging()
    enter_local_app_dir()
    _log_preflight()
    app = build_app(
        public_url=os.environ.get("A2A_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        discovery_url=os.environ.get("COGNITO_DISCOVERY_URL") or None,
    )
    log.info("A2A 1.0 on 0.0.0.0:%d — card at %s, JSON-RPC at %s", PORT, AGENT_CARD_WELL_KNOWN_PATH, RPC_PATH)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
