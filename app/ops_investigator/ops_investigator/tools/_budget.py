"""Per-invocation API budget kept in the agent's invocation_state (spec: reviewer M5)."""

from __future__ import annotations


def take(invocation_state: dict | None, key: str, limit: int) -> bool:
    """Consume one unit; return False when the budget for `key` is exhausted."""
    if invocation_state is None:
        return True
    budget = invocation_state.setdefault("budget", {})
    used = budget.get(key, 0)
    if used >= limit:
        return False
    budget[key] = used + 1
    return True
