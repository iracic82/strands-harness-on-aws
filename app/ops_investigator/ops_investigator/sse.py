"""Versioned SSE frames (spec 4.4). Never put tool bodies or prompts on the wire beyond text deltas."""

from __future__ import annotations

from typing import Any

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
    return total


def map_event(event: Any) -> dict | None:
    """Map a Strands stream event to a v1 SSE frame, or None to drop it. Never raises on odd input."""
    if not isinstance(event, dict):
        return None
    if isinstance(event.get("data"), str):
        return frame("text", delta=event["data"])
    if event.get("reasoning") and isinstance(event.get("reasoningText"), str):
        return frame("thinking", delta=event["reasoningText"])
    if event.get("type") == "tool_use_stream":
        tu = event.get("current_tool_use")
        tu = tu if isinstance(tu, dict) else {}
        inp = tu.get("input")
        inp = inp if isinstance(inp, dict) else {}
        return frame("tool_use", name=str(tu.get("name", "")), input_keys=sorted(inp.keys()))
    if event.get("type") == "tool_result":
        tr = event.get("tool_result")
        tr = tr if isinstance(tr, dict) else {}
        return frame(
            "tool_result",
            name=str(tr.get("name", "")),
            status=str(tr.get("status", "")),
            bytes=_content_bytes(tr.get("content")),
        )
    return None


def estimate_usd(usage: dict, model: str) -> float:
    usage = usage if isinstance(usage, dict) else {}
    model = model if isinstance(model, str) else ""
    family = next((k for k in PRICES if k in model), "sonnet")
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
