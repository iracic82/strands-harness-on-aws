"""AgentCore HTTP entrypoint (spec 4.4, 5). Streams versioned SSE frames; never raw Strands events."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus

from ops_investigator import platform, sse
from ops_investigator.agent import LIMITS, build_agent
from ops_investigator.auth import ActorError, actor_from_headers
from ops_investigator.preflight import run_preflight

log = logging.getLogger("ops_investigator.http")

MAX_PROMPT_BYTES = 32_768
DEFAULT_MAX_SECONDS = 600.0
MIN_SESSION_LEN = 33
CACHE_SIZE = 16
CLOSING_PROMPT = "You hit the budget. Summarise findings so far, likely cause, and what remains unchecked."


def _max_seconds(raw: str) -> float:
    """A typo in HARNESS_MAX_SECONDS must not stop the container from serving."""
    try:
        return float(raw)
    except ValueError:
        log.error("HARNESS_MAX_SECONDS=%r is not a number; using %s", raw, DEFAULT_MAX_SECONDS)
        return DEFAULT_MAX_SECONDS


MAX_SECONDS = _max_seconds(os.environ.get("HARNESS_MAX_SECONDS", str(DEFAULT_MAX_SECONDS)))


class PayloadError(ValueError):
    pass


class ActorMismatch(PermissionError):
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


def _close(agent: Any) -> None:
    """Blocking: the session manager flushes pending events to AgentCore. Call it off the event loop."""
    sm = getattr(agent, "session_manager", None)
    if sm is not None and hasattr(sm, "close"):
        with contextlib.suppress(Exception):
            sm.close()


class AgentCache:
    """Small LRU keyed by session id; a session belongs to exactly one actor (spec 5).

    Building an agent is blocking work — the cloud session manager runs ListEvents/CreateEvent in its
    constructor — so the factory runs in a worker thread and a per-session build lock keeps two racing
    first invokes from building (and billing) twice.
    """

    def __init__(self, maxsize: int = CACHE_SIZE) -> None:
        self.maxsize = maxsize
        self._items: OrderedDict[str, tuple[str, Any, asyncio.Lock]] = OrderedDict()
        self._build_locks: dict[str, asyncio.Lock] = {}
        self._build_waiters: Counter[str] = Counter()
        self._closing: set[asyncio.Task] = set()

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        """Drop every entry *without* closing its session manager; for tests and a hard reset only."""
        self._items.clear()
        self._build_locks.clear()
        self._build_waiters.clear()

    def lock(self, session_id: str) -> asyncio.Lock:
        """The turn lock for a cached session: held for one invocation, so `locked()` means busy."""
        return self._items[session_id][2]

    def _hit(self, session_id: str, actor_id: str) -> tuple[Any, asyncio.Lock] | None:
        if session_id not in self._items:
            return None
        actor, agent, turn_lock = self._items[session_id]
        if actor != actor_id:
            raise ActorMismatch("this session belongs to a different actor")
        self._items.move_to_end(session_id)
        return agent, turn_lock

    def _evict(self, protect: str) -> None:
        """Evict the oldest sessions that are *not* mid-turn, never the one just handed to a caller. Closing a
        live agent would kill its stream and let a second turn interleave on the same session id, so a cache
        whose entries are all in use runs over its size instead.

        Synchronous on purpose: there must be no await between inserting an entry and returning it, or a
        concurrent cache miss could evict the brand-new entry before its caller ever holds its lock. The
        blocking close still happens off the loop, as a background task.
        """
        while len(self._items) > self.maxsize:
            victim = next((sid for sid, (_, _, lk) in self._items.items() if sid != protect and not lk.locked()), None)
            if victim is None:
                log.warning("agent cache over %d entries: every cached session is mid-turn", self.maxsize)
                return
            _, evicted, _ = self._items.pop(victim)
            task = asyncio.create_task(asyncio.to_thread(_close, evicted))
            self._closing.add(task)  # hold a reference: the loop only keeps weak ones
            task.add_done_callback(self._closing.discard)

    async def drain_closes(self) -> None:
        """Wait for the background session-manager closes. For tests and an orderly shutdown."""
        while self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)

    async def get_or_create(
        self, session_id: str, actor_id: str, factory: Callable[[], Any]
    ) -> tuple[Any, asyncio.Lock]:
        """Return the session's agent and its turn lock together, so the caller never has to look the entry
        up a second time (by then the cache may legitimately have evicted it)."""
        hit = self._hit(session_id, actor_id)
        if hit is not None:
            return hit
        build_lock = self._build_locks.setdefault(session_id, asyncio.Lock())
        self._build_waiters[session_id] += 1
        try:
            async with build_lock:
                hit = self._hit(session_id, actor_id)
                if hit is not None:
                    return hit
                agent = await asyncio.to_thread(factory)  # never construct on the event loop (P14)
                turn_lock = asyncio.Lock()
                self._items[session_id] = (actor_id, agent, turn_lock)
                self._evict(protect=session_id)
                return agent, turn_lock
        finally:
            self._build_waiters[session_id] -= 1
            if self._build_waiters[session_id] <= 0:
                del self._build_waiters[session_id]
                self._build_locks.pop(session_id, None)


CACHE = AgentCache(maxsize=CACHE_SIZE)
app = BedrockAgentCoreApp()


def _profile(env: Mapping[str, str] | None = None) -> str:
    """The identity source depends on the profile, and the profile is known before the actor id is."""
    env = os.environ if env is None else env
    return "cloud" if env.get(platform.MEMORY_ENV) else "local"


async def _turn(agent: Any, prompt: str, limits: dict, results: list[Any]) -> AsyncIterator[dict]:
    """Yield one turn's SSE frames. An async generator cannot return a value, so the AgentResult
    (which arrives as the last stream event, not as a frame) lands in `results`."""
    mapper = sse.EventMapper()  # one per turn: dedupes tool_use frames and names tool_result frames
    async for event in agent.stream_async(prompt, limits=limits):
        if isinstance(event, dict) and "result" in event:
            results.append(event["result"])
            continue
        frame = sse.map_event(event, mapper)
        if frame:
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
        actor = actor_from_headers(headers, profile=_profile(), payload_actor=body["actor_id"])
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


def _configure_logging() -> None:
    """AgentCore ships stdout/stderr to CloudWatch, but nothing installs a handler: without this the
    preflight and turn lines are dropped. Only this package goes to INFO; the SDKs stay at WARNING."""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("ops_investigator").setLevel(logging.INFO)


def startup() -> None:
    _configure_logging()
    if _profile() == "local":
        # The harness `environment` plugin reads AGENTS.md from cwd, and local runs start at the repo root.
        os.chdir(platform.APP_DIR)
        log.info("local profile: serving from %s so AGENTS.md is picked up from cwd", platform.APP_DIR)
    if not run_preflight().ok:
        # No "unhealthy" enum exists; keep the failure loud in logs and refuse work (spec 5).
        log.error("preflight failed; invocations will return errors until it is fixed")
        app.force_ping_status(PingStatus.HEALTHY_BUSY)
