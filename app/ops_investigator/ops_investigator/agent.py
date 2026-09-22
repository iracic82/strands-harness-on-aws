"""The one place the agent is defined (spec 4.2).

`build_agent()` is the single entry point: `platform.detect()` picks the backends, and the same
`create_harness()` call is configured for the `local` or the `cloud` profile.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping

from strands import Agent
from strands.types.agent import Limits
from strands.types.tools import AgentTool
from strands_harness import create_harness
from strands_harness.tools import AgentSpec, Fixed, Inherit, Preset, make_subagent

from ops_investigator import platform
from ops_investigator.prompts import LOCAL_TOOLS_NOTE, OPS_INSTRUCTIONS, READER_PROMPT
from ops_investigator.tool_events import ToolResultRecorder, attach
from ops_investigator.tools import AWS_TOOLS

LIMITS = Limits(turns=25, output_tokens=60_000, total_tokens=1_500_000)
READER_LIMITS = Limits(turns=12, output_tokens=20_000, total_tokens=400_000)
BACKGROUND_TASKS = {"agentic": [], "max_concurrency": 2, "timeout": 300}

# The subagent tool calls its builder with an AgentSpec only, so the runtime of the parent is parked here.
_current: dict[str, platform.Runtime] = {}


def _cap_stream(agent: Agent, limits: Limits) -> Agent:
    """The harness subagent tool calls child.stream_async(prompt, invocation_state=..., cancel_signal=...) without
    limits, so bind the cap here (the Agent constructor has no `limits` keyword). A caller that passes its own
    limits keeps them, so this is a floor for the uncapped call, not an override."""
    orig = agent.stream_async

    @functools.wraps(orig)
    def capped(prompt=None, **kw):
        kw.setdefault("limits", limits)
        return orig(prompt, **kw)

    agent.stream_async = capped  # type: ignore[method-assign]
    return agent


def build_reader(spec: AgentSpec, rt: platform.Runtime | None = None) -> Agent:
    """Build the reader delegate: same read-only tools, no session, no memory, its own turn/token cap.

    The runtime is required: without it the child would run with no guard and no profile, so fail closed.
    """
    rt = rt or _current.get("rt")
    if rt is None:
        raise RuntimeError("build_reader requires a Runtime; call build_agent first")
    # The reader's own recorder: its tool calls are its own, and only the parent's feed the parent's stream.
    recorder = ToolResultRecorder()
    child = create_harness(
        model=spec.model or rt.model,
        effort="low",
        instructions=spec.instructions or READER_PROMPT,
        tools=AWS_TOOLS,
        builtin_tools={"*": False, "read": True},
        session=False,
        memory=False,
        skills=False,
        interventions=[platform.guard_for(rt)],
        hooks=[recorder],
        context_manager="auto",
        name="reader",
        trace_attributes={"session.id": rt.session_id, "agent.role": "reader"},
    )
    return _cap_stream(attach(child, recorder), READER_LIMITS)


def make_reader_tool(rt: platform.Runtime) -> AgentTool:
    return make_subagent(
        builder=functools.partial(build_reader, rt=rt),
        presets={
            "reader": Preset(
                instructions=READER_PROMPT,
                context="none",
                description="reads and summarises AWS state for one narrow question",
            )
        },
        default_preset="reader",
        instructions=Fixed(None),
        tools=Fixed(None),  # the preset already fixes the tool set; an open `tools` param renders an empty enum
        model=Inherit(),
        max_depth=1,
        name="reader",
    )


def build_agent(session_id: str, actor_id: str, env: Mapping[str, str] | None = None) -> Agent:
    rt = platform.detect(session_id, actor_id, env=env)
    _current["rt"] = rt
    platform.enter_workspace(rt)
    # The stream never carries a tool result (P51); the hook that does is attached below for the runtimes.
    recorder = ToolResultRecorder()
    agent = create_harness(
        model=rt.model,
        effort=rt.effort,
        instructions=OPS_INSTRUCTIONS if rt.profile == "cloud" else OPS_INSTRUCTIONS + LOCAL_TOOLS_NOTE,
        tools=[*AWS_TOOLS, make_reader_tool(rt)],
        mcp_servers=rt.mcp_servers,
        builtin_tools=platform.builtin_tools(rt),
        background_tasks=BACKGROUND_TASKS,
        session=platform.session_option(rt),
        memory=platform.memory_option(rt),
        skills=platform.skills_option(rt),
        interventions=[platform.guard_for(rt)],
        # The harness forwards consumer hooks to subagents it builds itself, which is inert here: the
        # builtin `subagent` tool is off in both profiles, and the one delegate is `make_reader_tool`,
        # whose builder is `build_reader` above and which therefore gets its own recorder, not this one.
        hooks=[recorder],
        caching="auto",
        context_manager="auto",
        name="ops-investigator",
        trace_attributes={
            "session.id": session_id,
            "user.id": actor_id,
            "gen_ai.request.model": rt.model,
            "harness.effort": rt.effort,
        },
    )
    return attach(agent, recorder)
