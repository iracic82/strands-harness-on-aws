"""Versioned SSE frames (spec 4.4). Never put tool bodies or prompts on the wire beyond text deltas."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("ops_investigator.sse")

SCHEMA_VERSION = 1

# USD per 1M tokens: (input, output, cache_read). Verify against the Bedrock price list before release (spec 10.5).
PRICES = {
    "sonnet": (3.0, 15.0, 0.30),
    "opus": (15.0, 75.0, 1.50),
    "haiku": (1.0, 5.0, 0.10),
}


def frame(type_: str, **fields: Any) -> dict:
    return {"v": SCHEMA_VERSION, "type": type_, **fields}


def _content_bytes(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, dict) and "text" in block:
            total += len(str(block["text"]))
        elif isinstance(block, dict) and "json" in block:
            total += len(str(block["json"]))
        elif isinstance(block, dict):
            total += len(str(block))  # document/image/other blocks: approximate size, not exact bytes
    return total


class EventMapper:
    """Stateful per-turn mapper. Real `tool_use_stream` events arrive once per delta with `input` as a
    growing JSON *string*; this tracks toolUseId -> name (so `tool_result` frames, which carry no name
    of their own, can report one) and emits exactly one `tool_use` frame per call, once its input has
    streamed in as a complete JSON object. Use one instance per turn.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._emitted: set[str | None] = set()
        self._denied: set[str] = set()

    def map(self, event: Any) -> dict | None:
        if not isinstance(event, dict):
            return None
        if isinstance(event.get("data"), str):
            return frame("text", delta=event["data"])
        if event.get("reasoning") and isinstance(event.get("reasoningText"), str):
            return frame("thinking", delta=event["reasoningText"])
        if event.get("type") == "tool_use_stream":
            return self._map_tool_use_stream(event)
        if "tool_cancel_event" in event:
            return self._map_tool_cancel(event)
        if event.get("type") == "tool_result":
            return self._map_tool_result(event)
        return None

    def _map_tool_use_stream(self, event: dict) -> dict | None:
        tu = event.get("current_tool_use")
        tu = tu if isinstance(tu, dict) else {}
        tid = tu.get("toolUseId")
        name = tu.get("name", "")
        if tid and name:
            self._names[tid] = name
        raw_input = tu.get("input")
        if isinstance(raw_input, dict):
            inp: dict | None = raw_input
        elif isinstance(raw_input, str):
            try:
                parsed = json.loads(raw_input)
            except (ValueError, TypeError):
                parsed = None
            inp = parsed if isinstance(parsed, dict) else None
        else:
            inp = None
        if isinstance(inp, dict) and tid not in self._emitted:
            self._emitted.add(tid)
            return frame("tool_use", name=str(name), input_keys=sorted(inp.keys()))
        return None  # partial chunk (input hasn't streamed in as a full JSON object yet), or a repeat

    def _map_tool_cancel(self, event: dict) -> dict | None:
        """An intervention (`ReadOnlyGuard`) denied the call before it ran: the SDK yields
        `ToolCancelEvent` followed by an error `tool_result` for the same toolUseId. Emit exactly one
        `denied` frame per toolUseId; the following `tool_result` is still mapped normally (it just
        carries `status="error"` like any other failed call).
        """
        tc = event.get("tool_cancel_event")
        tc = tc if isinstance(tc, dict) else {}
        tu = tc.get("tool_use")
        tu = tu if isinstance(tu, dict) else {}
        tid = tu.get("toolUseId")
        name = tu.get("name", "")
        if tid is not None:
            if tid in self._denied:
                return None
            self._denied.add(tid)
        message = tc.get("message")
        reason = message if isinstance(message, str) else ""
        return frame("denied", tool=str(name), reason=reason[:300])

    def _map_tool_result(self, event: dict) -> dict | None:
        tr = event.get("tool_result")
        tr = tr if isinstance(tr, dict) else {}
        tool_use_id = tr.get("toolUseId", "")
        name = tr.get("name") or self._names.get(tool_use_id, "")
        return frame(
            "tool_result",
            name=str(name),
            status=str(tr.get("status", "")),
            bytes=_content_bytes(tr.get("content")),
        )


def map_event(event: Any, mapper: EventMapper | None = None) -> dict | None:
    """Map a single Strands stream event to a v1 SSE frame, or None to drop it. Never raises on odd input.

    Pass the same `EventMapper` across a turn's events so `tool_use`/`tool_result` frames can dedupe and
    resolve names across the stream; without one, each call gets a fresh, single-event mapper.
    """
    return (mapper or EventMapper()).map(event)


def estimate_usd(usage: dict, model: str) -> float | None:
    """Estimate cost in USD, or None (logging a warning) if `model` doesn't match a known price family."""
    usage = usage if isinstance(usage, dict) else {}
    model = model if isinstance(model, str) else ""
    family = next((k for k in PRICES if k in model), None)
    if family is None:
        log.warning("unknown model family for pricing: %s", model)
        return None
    inp, out, cache = PRICES[family]
    return round(
        usage.get("inputTokens", 0) / 1e6 * inp
        + usage.get("outputTokens", 0) / 1e6 * out
        + usage.get("cacheReadInputTokens", 0) / 1e6 * cache,
        4,
    )


def metrics_frame(result: Any, model: str) -> dict:
    """Build the metrics frame from an AgentResult. Never raises: missing/odd metrics collapse to zeros."""
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    usage = usage if isinstance(usage, dict) else {}
    return frame(
        "metrics",
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0),
        cycles=getattr(metrics, "cycle_count", 0),
        stop_reason=str(getattr(result, "stop_reason", "")),
        est_usd=estimate_usd(usage, model),
    )
