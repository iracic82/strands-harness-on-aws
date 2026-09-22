"""A2A 1.0 server over the same `build_agent()` (spec 4.5).

One A2A context is one conversation: its context id is the AgentCore session id, its agent is built
once and reused for every turn on it, and a per-context lock keeps two `SendMessage` calls from
interleaving on one session. Every failure is reported as a task state, never as a transport error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
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

from ops_investigator import platform
from ops_investigator.agent import LIMITS, build_agent
from ops_investigator.agent_card import build_agent_card
from ops_investigator.auth import ActorError, actor_from_headers

log = logging.getLogger("ops_investigator.a2a")

DEFAULT_MAX_SECONDS = 600.0
MIN_CONTEXT_LEN = 33  # AgentCore's session id floor; the SDK's own context ids are uuid4 (P9)
PORT = 9000
DEFAULT_PUBLIC_URL = "http://localhost:9000"
RPC_PATH = "/"
WORKING_MESSAGE = "Investigating..."
NO_ANSWER = "(no answer)"
ANSWER_ARTIFACT = "answer"


def _max_seconds(raw: str) -> float:
    """A typo in HARNESS_MAX_SECONDS must not stop the container from serving."""
    try:
        return float(raw)
    except ValueError:
        log.error("HARNESS_MAX_SECONDS=%r is not a number; using %s", raw, DEFAULT_MAX_SECONDS)
        return DEFAULT_MAX_SECONDS


MAX_SECONDS = _max_seconds(os.environ.get("HARNESS_MAX_SECONDS", str(DEFAULT_MAX_SECONDS)))


class ActorMismatch(PermissionError):
    """Two actors on one A2A context: in the local profile they would share one session file."""


def _profile(env: Mapping[str, str] | None = None) -> str:
    """Read per request, never frozen at import: the identity source depends on the profile."""
    env = os.environ if env is None else env
    return "cloud" if env.get(platform.MEMORY_ENV) else "local"


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


async def _text_chunks(agent: Any, prompt: str) -> AsyncIterator[str]:
    """Only the assistant's text deltas: the artifact is the answer, not the raw harness event stream."""
    async for event in agent.stream_async(prompt, limits=LIMITS):
        if isinstance(event, dict) and isinstance(event.get("data"), str):
            yield event["data"]


async def _collect(agent: Any, prompt: str) -> tuple[str, bool]:
    """Drain one turn under a wall clock; returns the answer and whether the clock ran out.

    The deadline is applied to each pull rather than wrapped around the loop: `asyncio.timeout` does not
    exist on Python 3.10 (the floor), and there `asyncio.TimeoutError` is not the builtin `TimeoutError`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_SECONDS
    chunks: list[str] = []
    stream = _text_chunks(agent, prompt).__aiter__()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "".join(chunks), True
            try:
                chunks.append(await asyncio.wait_for(stream.__anext__(), remaining))
            except StopAsyncIteration:
                return "".join(chunks), False
            except asyncio.TimeoutError:  # on 3.10 this is not the builtin TimeoutError
                if loop.time() < deadline:
                    raise  # someone else's timeout (a tool, a boto read) — report it as itself
                return "".join(chunks), True
    finally:
        await stream.aclose()


class HarnessExecutor(AgentExecutor):
    """One agent per A2A context, built off the event loop and reused for every turn on that context."""

    def __init__(self, agent_factory: Callable[[str, str], Any] | None = None) -> None:
        self._factory = agent_factory or build_agent
        self._agents: dict[str, tuple[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _claim(self, context_id: str, actor: str) -> Any | None:
        """The context's agent if it has one; raises when another actor already owns the context."""
        owner, agent = self._agents.get(context_id, (actor, None))
        if owner != actor:
            raise ActorMismatch("this A2A context belongs to a different actor; start a new context id")
        return agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Never raises: an A2A caller learns what went wrong from the task state, not from a transport error."""
        task, unusable = _task_for(context)
        if task is not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        try:
            if unusable:
                await _reject(updater, unusable)
                return
            if len(task.context_id) < MIN_CONTEXT_LEN:
                await _reject(
                    updater,
                    f"context id must be at least {MIN_CONTEXT_LEN} characters: it is the "
                    "AgentCore session id, and the SDK's own uuid4 context ids qualify",
                )
                return
            try:
                actor = actor_from_headers(_headers(context), profile=_profile(), payload_actor=None)
            except ActorError as e:
                await _reject(updater, str(e))
                return
            await self._turn(updater, task.context_id, actor, get_message_text(context.message))
        except ActorMismatch as e:
            await _reject(updater, str(e))
        except Exception as e:  # the task state is the only channel back to an A2A caller
            log.exception("a2a turn failed context=%s", task.context_id)
            with contextlib.suppress(Exception):  # a terminal state may already have been published
                await updater.update_status(TaskState.TASK_STATE_FAILED, message=new_text_message(type(e).__name__))

    async def _turn(self, updater: TaskUpdater, context_id: str, actor: str, prompt: str) -> None:
        async with self._locks.setdefault(context_id, asyncio.Lock()):
            agent = self._claim(context_id, actor)
            if agent is None:
                try:
                    # Building is blocking work: the cloud session manager runs ListEvents/CreateEvent in
                    # its constructor, so it never happens on the event loop (P14).
                    agent = await asyncio.to_thread(self._factory, context_id, actor)
                except Exception as e:  # session manager init (memory AccessDenied etc.) fails closed (spec 5)
                    log.exception("agent construction failed context=%s", context_id)
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=new_text_message(f"agent unavailable: {type(e).__name__}"),
                    )
                    return
                self._agents[context_id] = (actor, agent)
            await updater.update_status(TaskState.TASK_STATE_WORKING, message=new_text_message(WORKING_MESSAGE))
            log.info("a2a turn start context=%s actor=%s prompt_bytes=%d", context_id, actor, len(prompt.encode()))
            answer, timed_out = await _collect(agent, prompt)
        if timed_out:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED, message=new_text_message(f"timeout after {MAX_SECONDS:.0f}s")
            )
            return
        await updater.add_artifact(
            parts=[new_text_part(answer or NO_ANSWER, media_type="text/plain")], name=ANSWER_ARTIFACT
        )
        await updater.update_status(TaskState.TASK_STATE_COMPLETED)

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
    return Starlette(routes=routes)


def _configure_logging() -> None:
    """uvicorn installs handlers for its own loggers only; without this every turn line is dropped."""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("ops_investigator").setLevel(logging.INFO)


def main() -> None:
    _configure_logging()
    if _profile() == "local":
        # The harness `environment` plugin reads AGENTS.md from cwd, and local runs start at the repo root.
        os.chdir(platform.APP_DIR)
        log.info("local profile: serving from %s so AGENTS.md is picked up from cwd", platform.APP_DIR)
    app = build_app(
        public_url=os.environ.get("A2A_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        discovery_url=os.environ.get("COGNITO_DISCOVERY_URL") or None,
    )
    log.info("A2A 1.0 on 0.0.0.0:%d — card at %s, JSON-RPC at %s", PORT, AGENT_CARD_WELL_KNOWN_PATH, RPC_PATH)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
