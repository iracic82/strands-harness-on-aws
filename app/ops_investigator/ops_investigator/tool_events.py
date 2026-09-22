"""Finished tool calls for the SSE layer (spec 4.4, 6.4).

`agent.stream_async()` never yields a tool result: in `strands-agents` 1.56 `ToolResultEvent`
is not a callback event, so the only place a completed call surfaces is the `AfterToolCallEvent`
hook (P51). `ToolResultRecorder` buffers each completed call in the shape `sse.EventMapper`
already maps, and the runtime's stream loop drains it between events, so a `tool_result` frame
normally lands right after the `tool_use` frame for the same call. Not always: on the cloud
profile `programmatic_tool_caller` runs its inner calls through `ToolExecutor._stream` itself and
consumes those events, so the hook fires for a call whose `tool_use_stream` events never reached
the runtime. Those results are reported with no `tool_use` frame ahead of them.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

# The recorder has to travel with the agent, because the agent cache hands the stream loop an `Agent`
# and nothing else. `agent.state` cannot hold it (session state must be JSON), so it is a plain
# attribute set by `agent.build_agent`/`build_reader` once `create_harness` has returned.
RECORDER_ATTR = "_ops_tool_recorder"


class ToolResultRecorder(HookProvider):
    """Records completed tool calls so the runtime can emit `tool_result` frames, and denied ones so it
    can emit `denied` frames for calls the SDK's own cancel event never reaches.

    Nothing here reaches the wire except what `sse.EventMapper` keeps: the tool name, the status, a byte
    count, and a denial's reason. The tool body is held only until the next drain (spec 6.4).
    """

    def __init__(self) -> None:
        self._pending: list[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    def _on_after_tool(self, event: AfterToolCallEvent, **kwargs: Any) -> None:
        """Cannot raise on an odd payload shape: a hook that throws takes the tool loop down with it."""
        tool_use = event.tool_use if isinstance(event.tool_use, dict) else {}
        result = event.result if isinstance(event.result, dict) else {}
        if event.cancel_message is not None:
            # An intervention denied the call. The SDK yields its own `ToolCancelEvent` for a call the
            # model made directly, and `EventMapper` dedupes the two by `toolUseId` — but not for a call
            # made inside `programmatic_tool_caller`, whose events never leave it. Replaying it here is
            # what makes a denial in there reach the wire as `denied` then `tool_result`, like any other.
            self._pending.append({"tool_cancel_event": {"tool_use": tool_use, "message": str(event.cancel_message)}})
        self._pending.append(
            {
                "type": "tool_result",
                "tool_result": {
                    "toolUseId": result.get("toolUseId") or tool_use.get("toolUseId", ""),
                    # The hook knows the name the stream event does not carry (P35); pass it through.
                    "name": tool_use.get("name", ""),
                    "status": result.get("status", ""),
                    "content": result.get("content", []),
                },
            }
        )

    def drain(self) -> list[dict]:
        """Return the calls recorded since the last drain, and forget them."""
        pending, self._pending = self._pending, []
        return pending


def attach(agent: Any, recorder: ToolResultRecorder) -> Any:
    """Bind `recorder` to `agent` so `recorder_for` can find it later. Returns the agent."""
    setattr(agent, RECORDER_ATTR, recorder)
    return agent


def recorder_for(agent: Any) -> ToolResultRecorder | None:
    """The agent's recorder, or None for an agent built without one (a test double, say)."""
    recorder = getattr(agent, RECORDER_ATTR, None)
    return recorder if isinstance(recorder, ToolResultRecorder) else None
