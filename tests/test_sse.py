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
    # No input at all yet (a real partial chunk) -> not a complete JSON object, so no frame, no crash.
    assert sse.map_event({"type": "tool_use_stream", "current_tool_use": {}}) is None
    assert sse.map_event({"type": "tool_use_stream"}) is None
    assert sse.map_event({"type": "tool_use_stream", "current_tool_use": "oops"}) is None
    # A genuinely zero-argument tool call *does* stream in as a complete (empty) JSON object and emits.
    ev = {"type": "tool_use_stream", "current_tool_use": {"name": "no_arg_tool", "input": "{}"}}
    assert sse.map_event(ev) == {"v": 1, "type": "tool_use", "name": "no_arg_tool", "input_keys": []}


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


# --- Realistic streaming shapes: current_tool_use.input arrives as a growing JSON *string*, one event per
# delta, and tool_result never carries its own name -- both require a shared EventMapper across the turn. ---


def test_streaming_tool_use_input_string_and_tool_result_name_resolution():
    mapper = sse.EventMapper()

    def tool_use_event(partial_input: str) -> dict:
        return {
            "type": "tool_use_stream",
            "current_tool_use": {"toolUseId": "t1", "name": "aws_call", "input": partial_input},
        }

    # (a) Three deltas for the same call; only the third has a complete JSON object -> exactly one frame.
    assert sse.map_event(tool_use_event('{"serv'), mapper=mapper) is None
    assert sse.map_event(tool_use_event('{"service": "ec2", "oper'), mapper=mapper) is None
    out = sse.map_event(tool_use_event('{"service": "ec2", "operation": "describe_instances"}'), mapper=mapper)
    assert out == {"v": 1, "type": "tool_use", "name": "aws_call", "input_keys": ["operation", "service"]}

    # A repeat of the same (now-complete) input for the same toolUseId must not re-emit.
    assert sse.map_event(tool_use_event('{"service": "ec2", "operation": "describe_instances"}'), mapper=mapper) is None

    # (b) The matching tool_result carries no name of its own -> resolved from the mapper's toolUseId map.
    result_event = {
        "type": "tool_result",
        "tool_result": {"toolUseId": "t1", "status": "success", "content": [{"text": "x" * 10}]},
    }
    out = sse.map_event(result_event, mapper=mapper)
    assert out["name"] == "aws_call" and out["bytes"] == 10 and out["status"] == "success"

    # (c) A tool_result for an id the mapper never saw falls back to an empty name, not a crash.
    unknown_result = {"type": "tool_result", "tool_result": {"toolUseId": "unknown_id", "status": "success"}}
    assert sse.map_event(unknown_result, mapper=mapper)["name"] == ""


def test_map_event_without_mapper_gets_a_fresh_one_shot_mapper_each_call():
    # No shared mapper: each call starts over, so a lone partial-JSON chunk never resolves to a tool_use frame.
    ev = {"type": "tool_use_stream", "current_tool_use": {"toolUseId": "t1", "name": "aws_call", "input": '{"a": 1'}}
    assert sse.map_event(ev) is None


def test_content_bytes_falls_back_for_non_text_non_json_blocks():
    ev = {
        "type": "tool_result",
        "tool_result": {"toolUseId": "t", "name": "reader", "status": "success", "content": [{"document": {}}]},
    }
    out = sse.map_event(ev)
    assert out["bytes"] > 0


# --- tool_cancel_event: an intervention denied the call before it ran (spec: denials visible on the
# wire), the SDK's real sequence is tool_use_stream -> tool_cancel_event -> error tool_result. ---


def test_tool_cancel_event_yields_denied_frame_then_error_tool_result():
    mapper = sse.EventMapper()
    tool_use_event = {
        "type": "tool_use_stream",
        "current_tool_use": {
            "toolUseId": "t1",
            "name": "aws_call",
            "input": '{"service": "ec2", "operation": "terminate_instances"}',
        },
    }
    assert sse.map_event(tool_use_event, mapper=mapper) == {
        "v": 1,
        "type": "tool_use",
        "name": "aws_call",
        "input_keys": ["operation", "service"],
    }

    reason = "DENIED: read-only: ec2:terminate_instances is not a permitted read operation"
    cancel_event = {
        "tool_cancel_event": {
            "tool_use": {
                "toolUseId": "t1",
                "name": "aws_call",
                "input": {"service": "ec2", "operation": "terminate_instances"},
            },
            "message": reason,
        }
    }
    out = sse.map_event(cancel_event, mapper=mapper)
    assert out == {"v": 1, "type": "denied", "tool": "aws_call", "reason": reason}

    result_event = {
        "type": "tool_result",
        "tool_result": {"toolUseId": "t1", "status": "error", "content": [{"text": reason}]},
    }
    out = sse.map_event(result_event, mapper=mapper)
    assert out["type"] == "tool_result" and out["status"] == "error" and out["name"] == "aws_call"


def test_tool_cancel_event_dedupes_by_tool_use_id():
    mapper = sse.EventMapper()
    ev = {"tool_cancel_event": {"tool_use": {"toolUseId": "t2", "name": "shell"}, "message": "DENIED: no"}}
    first = sse.map_event(ev, mapper=mapper)
    second = sse.map_event(ev, mapper=mapper)
    assert first == {"v": 1, "type": "denied", "tool": "shell", "reason": "DENIED: no"}
    assert second is None


def test_tool_cancel_event_reason_truncated_to_300_chars():
    long_message = "DENIED: " + "x" * 400
    ev = {"tool_cancel_event": {"tool_use": {"toolUseId": "t3", "name": "shell"}, "message": long_message}}
    out = sse.map_event(ev)
    assert out["reason"] == long_message[:300] and len(out["reason"]) == 300


def test_tool_cancel_event_handles_odd_shapes_without_raising():
    assert sse.map_event({"tool_cancel_event": {}}) == {"v": 1, "type": "denied", "tool": "", "reason": ""}
    assert sse.map_event({"tool_cancel_event": "not-a-dict"}) == {"v": 1, "type": "denied", "tool": "", "reason": ""}
    out = sse.map_event({"tool_cancel_event": {"tool_use": "nope", "message": 123}})
    assert out == {"v": 1, "type": "denied", "tool": "", "reason": ""}


def test_map_event_tool_cancel_without_mapper_still_maps():
    ev = {
        "tool_cancel_event": {
            "tool_use": {"toolUseId": "t4", "name": "shell"},
            "message": "DENIED: shell is disabled for the read-only investigator in this profile",
        }
    }
    out = sse.map_event(ev)
    assert out == {
        "v": 1,
        "type": "denied",
        "tool": "shell",
        "reason": "DENIED: shell is disabled for the read-only investigator in this profile",
    }


def test_estimate_usd_returns_none_and_warns_for_unknown_model_family(caplog):
    with caplog.at_level("WARNING", logger="ops_investigator.sse"):
        result = sse.estimate_usd({"inputTokens": 1000}, "bedrock/amazon.nova-pro-v1:0")
    assert result is None
    assert "unknown model family" in caplog.text


def test_metrics_frame_carries_none_est_usd_for_unknown_model_family():
    result = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(cycle_count=1, accumulated_usage={"inputTokens": 10}),
    )
    out = sse.metrics_frame(result, model="bedrock/amazon.nova-pro-v1:0")
    assert out["est_usd"] is None
