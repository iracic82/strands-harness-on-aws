from __future__ import annotations

from ops_investigator import sse
from ops_investigator.tool_events import ToolResultRecorder, attach, recorder_for
from strands.hooks import AfterToolCallEvent, HookRegistry


def _event(
    *,
    tool_use: object = None,
    result: object = None,
    cancel_message: str | None = None,
) -> AfterToolCallEvent:
    """A real `AfterToolCallEvent`; only `tool_use`, `result` and `cancel_message` matter to the recorder."""
    return AfterToolCallEvent(
        agent=None,
        selected_tool=None,
        tool_use=tool_use if tool_use is not None else {"toolUseId": "t1", "name": "aws_call", "input": {}},
        invocation_state={},
        result=result
        if result is not None
        else {"toolUseId": "t1", "status": "success", "content": [{"text": "x" * 10}]},
        cancel_message=cancel_message,
    )


def _fire(recorder: ToolResultRecorder, event: AfterToolCallEvent) -> None:
    """Drive the recorder the way the SDK does: through a registry it registered itself on."""
    registry = HookRegistry()
    registry.add_hook(recorder)
    registry.invoke_callbacks(event)


def test_register_hooks_subscribes_to_after_tool_call():
    registry = HookRegistry()
    recorder = ToolResultRecorder()
    recorder.register_hooks(registry)
    assert list(registry.get_callbacks_for(_event())) == [recorder._on_after_tool]


def test_a_finished_call_drains_in_the_shape_the_mapper_reads():
    recorder = ToolResultRecorder()
    _fire(recorder, _event())
    assert recorder.drain() == [
        {
            "type": "tool_result",
            "tool_result": {
                "toolUseId": "t1",
                "name": "aws_call",
                "status": "success",
                "content": [{"text": "x" * 10}],
            },
        }
    ]


def test_drain_clears_and_preserves_order():
    recorder = ToolResultRecorder()
    assert recorder.drain() == []
    _fire(recorder, _event(tool_use={"toolUseId": "a", "name": "aws_call"}))
    _fire(recorder, _event(tool_use={"toolUseId": "b", "name": "logs_insights"}))
    assert [e["tool_result"]["name"] for e in recorder.drain()] == ["aws_call", "logs_insights"]
    assert recorder.drain() == []


def test_a_failed_call_is_recorded_with_its_status():
    recorder = ToolResultRecorder()
    _fire(recorder, _event(result={"toolUseId": "t1", "status": "error", "content": [{"text": "DENIED: read-only"}]}))
    assert recorder.drain()[0]["tool_result"]["status"] == "error"


def test_odd_hook_payloads_do_not_raise():
    """A hook that throws takes the tool loop down with it, so nothing here may assume a shape."""
    recorder = ToolResultRecorder()
    _fire(recorder, _event(tool_use="not-a-dict", result=None))
    _fire(recorder, _event(tool_use={}, result={}))
    names = [e["tool_result"]["name"] for e in recorder.drain()]
    assert names == ["", ""]


def test_only_the_name_status_and_a_byte_count_reach_the_wire():
    """The recorder holds the body; `sse.EventMapper` is what decides none of it is published (spec 6.4)."""
    recorder = ToolResultRecorder()
    secret = "arn:aws:iam::123456789012:user/alice"
    _fire(recorder, _event(result={"toolUseId": "t1", "status": "success", "content": [{"text": secret}]}))
    frame = sse.map_event(recorder.drain()[0], sse.EventMapper())
    assert frame == {
        "v": 1,
        "type": "tool_result",
        "name": "aws_call",
        "status": "success",
        "bytes": len(secret),
    }
    assert secret not in str(frame)


def test_a_denial_is_recorded_as_a_cancel_ahead_of_its_result():
    """`ToolExecutor._stream` sets `cancel_message` and synthesises an error result for an intervention's
    `Deny`. Recording the cancel first is what puts `denied` ahead of `tool_result` on the wire."""
    recorder = ToolResultRecorder()
    _fire(
        recorder,
        _event(
            result={"toolUseId": "t1", "status": "error", "content": [{"text": "DENIED: x"}]},
            cancel_message="DENIED: x",
        ),
    )
    drained = recorder.drain()
    assert len(drained) == 2 and "tool_cancel_event" in drained[0] and drained[1]["type"] == "tool_result"
    assert drained[0] == {
        "tool_cancel_event": {
            "tool_use": {"toolUseId": "t1", "name": "aws_call", "input": {}},
            "message": "DENIED: x",
        }
    }
    assert drained[1]["tool_result"]["status"] == "error"


def test_an_allowed_call_records_no_cancel():
    recorder = ToolResultRecorder()
    _fire(recorder, _event())
    drained = recorder.drain()
    assert len(drained) == 1 and drained[0]["type"] == "tool_result"


def test_a_call_the_mapper_never_saw_is_still_named():
    """`programmatic_tool_caller` runs its inner calls through `ToolExecutor._stream` itself and consumes
    those events, so the hook fires for a toolUseId no `tool_use_stream` event ever announced. The name
    has to come from the recorder, because the mapper's id map has never heard of this call."""
    recorder = ToolResultRecorder()
    _fire(recorder, _event(tool_use={"toolUseId": "tooluse_aws_call_123456789", "name": "aws_call"}))
    frame = sse.map_event(recorder.drain()[0], sse.EventMapper())
    assert frame["type"] == "tool_result" and frame["name"] == "aws_call"


def test_attach_and_recorder_for_round_trip():
    class FakeAgent:
        pass

    agent = FakeAgent()
    assert recorder_for(agent) is None  # an agent built without one (a test double) is not an error
    recorder = ToolResultRecorder()
    assert attach(agent, recorder) is agent
    assert recorder_for(agent) is recorder
