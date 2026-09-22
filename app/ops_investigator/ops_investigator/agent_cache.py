"""The per-session agent cache shared by every protocol entrypoint (spec 5).

Building an agent is expensive and stateful — in the cloud profile the session manager runs
ListEvents/CreateEvent in its constructor — so one agent is built per session id and reused, and a
session belongs to exactly one actor for as long as it is cached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter, OrderedDict
from collections.abc import Callable
from typing import Any

log = logging.getLogger("ops_investigator.cache")

CACHE_SIZE = 16


class ActorMismatch(PermissionError):
    pass


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
