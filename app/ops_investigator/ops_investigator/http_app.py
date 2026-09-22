"""AgentCore HTTP entrypoint (spec 4.4, 5). Streams versioned SSE frames; never raw Strands events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus

from ops_investigator import platform, sse, tool_events
from ops_investigator.agent import LIMITS, build_agent
from ops_investigator.agent_cache import CACHE, CACHE_SIZE, ActorMismatch, AgentCache
from ops_investigator.auth import ActorError, actor_from_headers
from ops_investigator.preflight import run_preflight
from ops_investigator.runtime_common import (
    CLOSING_PROMPT,
    DEFAULT_MAX_SECONDS,
    MIN_SESSION_LEN,
    _max_seconds,
    configure_logging,
    enter_local_app_dir,
    max_seconds,
    profile,
)

log = logging.getLogger("ops_investigator.http")

# Re-exported for callers and tests that have always read these off this module.
__all__ = [
    "CACHE",
    "CACHE_SIZE",
    "CLOSING_PROMPT",
    "DEFAULT_MAX_SECONDS",
    "MIN_SESSION_LEN",
    "ActorMismatch",
    "AgentCache",
    "PayloadError",
    "_max_seconds",
    "app",
    "invoke",
    "startup",
    "validate_payload",
]

MAX_PROMPT_BYTES = 32_768
MAX_SECONDS = max_seconds()


class PayloadError(ValueError):
    pass


def validate_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object with a string 'prompt'")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        # Checked before the unknown-key check: a caller who misspelled the key needs to hear "prompt".
        raise PayloadError("'prompt' must be a non-empty string")
    unknown = set(payload) - {"prompt", "actor_id"}
    if unknown:
        raise PayloadError(f"unknown payload keys: {sorted(unknown)}")
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise PayloadError(f"'prompt' exceeds {MAX_PROMPT_BYTES} bytes")
    actor = payload.get("actor_id")
    if actor is not None and not isinstance(actor, str):
        raise PayloadError("'actor_id' must be a string when present")
    return {"prompt": prompt, "actor_id": actor}


app = BedrockAgentCoreApp()


def _recorded(recorder: tool_events.ToolResultRecorder | None, mapper: sse.EventMapper) -> Iterator[dict]:
    """Frames for the tool calls that finished since the last drain (P51)."""
    for event in recorder.drain() if recorder else ():
        frame = sse.map_event(event, mapper)
        if frame:
            yield frame


async def _turn(agent: Any, prompt: str, limits: dict, results: list[Any]) -> AsyncIterator[dict]:
    """Yield one turn's SSE frames. An async generator cannot return a value, so the AgentResult
    (which arrives as the last stream event, not as a frame) lands in `results`."""
    mapper = sse.EventMapper()  # one per turn: dedupes tool_use frames and names tool_result frames
    recorder = tool_events.recorder_for(agent)
    async for event in agent.stream_async(prompt, limits=limits):
        # A finished tool call reaches us through the after-tool hook, never the stream (P51). Drain
        # before this event is mapped, so its `tool_result` frame precedes whatever the model said next.
        for frame in _recorded(recorder, mapper):
            yield frame
        if isinstance(event, dict) and "result" in event:
            results.append(event["result"])
            continue
        frame = sse.map_event(event, mapper)
        if frame:
            yield frame
    for frame in _recorded(recorder, mapper):  # a call that finished after the stream's last event
        yield frame


async def _stream(agent: Any, prompt: str, limits: dict, model: str) -> AsyncIterator[dict]:
    results: list[Any] = []
    async for frame in _turn(agent, prompt, limits, results):
        yield frame
    if not results:
        return
    result = results[-1]
    yield sse.metrics_frame(result, model)
    stop = str(getattr(result, "stop_reason", ""))
    if not stop.startswith("limit_"):
        return
    yield sse.frame(
        "error",
        message=f"budget exhausted ({stop})",
        hint="the agent hit its turn/token limit; asking it to summarise what it found",
    )
    # One closing turn so the caller gets findings instead of a bare budget error (spec 4.4).
    async for frame in _turn(agent, CLOSING_PROMPT, dict(limits, turns=1), []):
        yield frame


@app.entrypoint
async def invoke(payload: Any, context: Any) -> AsyncIterator[dict]:
    """Every request — a full turn or an immediate rejection — ends with exactly one `done` frame."""
    handler = _handle(payload, context)
    try:
        async for frame in handler:
            yield frame
    finally:
        # A consumer that walks away mid-stream (client disconnect) must not leave the turn lock held.
        await handler.aclose()
    yield sse.frame("done")


async def _handle(payload: Any, context: Any) -> AsyncIterator[dict]:
    session_id = getattr(context, "session_id", None) or ""
    headers = getattr(context, "request_headers", None) or {}
    if len(session_id) < MIN_SESSION_LEN:
        yield sse.frame(
            "error",
            message="runtimeSessionId too short",
            hint=f"AgentCore requires >= {MIN_SESSION_LEN} characters; use scripts/invoke.py, which generates one (P9)",
        )
        return
    try:
        body = validate_payload(payload)
        actor = actor_from_headers(headers, profile=profile(), payload_actor=body["actor_id"])
    except (PayloadError, ActorError) as e:
        yield sse.frame("error", message=str(e), hint="fix the request and retry")
        return
    try:
        rt = platform.detect(session_id, actor)
        agent, lock = await CACHE.get_or_create(session_id, actor, lambda: build_agent(session_id, actor))
    except ActorMismatch:
        yield sse.frame(
            "error",
            message="actor_mismatch",
            hint="this session id already belongs to another actor; start a new session id",
        )
        return
    except Exception as e:  # session manager init (memory AccessDenied etc.) fails closed (spec 5)
        log.exception("agent construction failed session=%s", session_id)
        yield sse.frame(
            "error",
            message=f"agent unavailable: {type(e).__name__}",
            hint="check the preflight lines in the runtime logs (agentcore logs)",
        )
        return
    if lock.locked():
        yield sse.frame("error", message="busy", hint="a turn is already running on this session; wait for done")
        return
    log.info("turn start session=%s actor=%s prompt_bytes=%d", session_id, actor, len(body["prompt"].encode()))
    # The wall clock is applied to each pull, never across a `yield`: a timeout that fired while the consumer
    # held the generator suspended would be delivered to the consumer, skipping the cleanup below entirely.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_SECONDS
    stream = _stream(agent, body["prompt"], LIMITS, rt.model).__aiter__()
    async with lock:
        try:
            timed_out = False
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    frame = await asyncio.wait_for(stream.__anext__(), remaining)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:  # on 3.10 this is not the builtin TimeoutError
                    if loop.time() < deadline:
                        raise  # someone else's timeout (a tool, a boto read) — report it as itself
                    timed_out = True
                    break
                yield frame
            if timed_out:
                yield sse.frame(
                    "error",
                    message=f"timeout after {MAX_SECONDS:.0f}s",
                    hint="narrow the question or raise HARNESS_MAX_SECONDS",
                )
        except Exception as e:
            log.exception("turn failed session=%s", session_id)
            yield sse.frame("error", message=f"{type(e).__name__}: {e}"[:300], hint="see the runtime logs")
        finally:
            await stream.aclose()


def startup() -> None:
    configure_logging()
    enter_local_app_dir()
    if not run_preflight().ok:
        # No "unhealthy" enum exists; keep the failure loud in logs and refuse work (spec 5).
        log.error("preflight failed; invocations will return errors until it is fixed")
        app.force_ping_status(PingStatus.HEALTHY_BUSY)
