from __future__ import annotations

from types import SimpleNamespace

from ops_investigator import sse


def test_text_delta():
    assert sse.map_event({"data": "hel", "delta": {}}) == {"v": 1, "type": "text", "delta": "hel"}


def test_reasoning_delta():
    assert sse.map_event({"reasoningText": "hmm", "reasoning": True}) == {"v": 1, "type": "thinking", "delta": "hmm"}


def test_tool_use_summarises_input_not_values():
    ev = {
        "type": "tool_use_stream",
        "current_tool_use": {"name": "aws_call", "input": {"service": "ec2", "operation": "describe_instances"}},
    }
    out = sse.map_event(ev)
    assert out == {"v": 1, "type": "tool_use", "name": "aws_call", "input_keys": ["operation", "service"]}


def test_tool_result_reports_size_only():
    ev = {
        "type": "tool_result",
        "tool_result": {
            "toolUseId": "t",
            "name": "aws_call",
            "status": "success",
            "content": [{"text": "x" * 1234}],
        },
    }
    out = sse.map_event(ev)
    assert out["type"] == "tool_result" and out["bytes"] == 1234 and "content" not in out


def test_unknown_events_dropped():
    assert sse.map_event({"init_event_loop": True}) is None
    assert sse.map_event("not a dict") is None


def test_metrics_frame():
    result = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(
            cycle_count=3,
            accumulated_usage={"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 5000},
        ),
    )
    out = sse.metrics_frame(result, model="bedrock/global.anthropic.claude-sonnet-5")
    assert out["type"] == "metrics" and out["cycles"] == 3 and out["stop_reason"] == "end_turn"
    assert 0 < out["est_usd"] < 1


# --- Edge cases: map_event/metrics_frame must never raise on odd input (spec: fail closed, not crash). ---


def test_map_event_handles_current_tool_use_missing_keys():
    assert sse.map_event({"type": "tool_use_stream", "current_tool_use": {}}) == {
        "v": 1,
        "type": "tool_use",
        "name": "",
        "input_keys": [],
    }
    assert sse.map_event({"type": "tool_use_stream"}) == {
        "v": 1,
        "type": "tool_use",
        "name": "",
        "input_keys": [],
    }
    assert sse.map_event({"type": "tool_use_stream", "current_tool_use": "oops"}) == {
        "v": 1,
        "type": "tool_use",
        "name": "",
        "input_keys": [],
    }


def test_map_event_handles_content_not_a_list():
    ev = {"type": "tool_result", "tool_result": {"name": "aws_call", "status": "success", "content": "oops"}}
    out = sse.map_event(ev)
    assert out["bytes"] == 0

    ev2 = {"type": "tool_result", "tool_result": {"name": "aws_call", "status": "success", "content": None}}
    assert sse.map_event(ev2)["bytes"] == 0

    ev3 = {"type": "tool_result"}
    out3 = sse.map_event(ev3)
    assert out3 == {"v": 1, "type": "tool_result", "name": "", "status": "", "bytes": 0}


def test_metrics_frame_handles_none_accumulated_usage():
    result = SimpleNamespace(stop_reason="end_turn", metrics=SimpleNamespace(cycle_count=0, accumulated_usage=None))
    out = sse.metrics_frame(result, model="bedrock/global.anthropic.claude-sonnet-5")
    assert out["input_tokens"] == 0 and out["output_tokens"] == 0 and out["cache_read_tokens"] == 0
    assert out["est_usd"] == 0.0


def test_metrics_frame_handles_missing_metrics_attribute():
    result = SimpleNamespace(stop_reason="end_turn")
    out = sse.metrics_frame(result, model="bedrock/global.anthropic.claude-sonnet-5")
    assert out["cycles"] == 0 and out["input_tokens"] == 0 and out["est_usd"] == 0.0
