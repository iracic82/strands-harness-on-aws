"""Where am I running, and which backends should the harness use?

`cloud` profile = inside AgentCore Runtime (the CDK stack injects MEMORY_OPSINVESTIGATORMEMORY_ID).
`local` profile = a laptop. See spec P2, P5, P6, P21, P23.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from strands.session.session_manager import SessionManager

    from ops_investigator.guard import ReadOnlyGuard

MEMORY_ENV = "MEMORY_OPSINVESTIGATORMEMORY_ID"
DEFAULT_MODEL = "bedrock/global.anthropic.claude-sonnet-4-6"  # the Claude 5 family is refused on many accounts
DEFAULT_EFFORT = "medium"
EFFORTS = ("auto", "off", "minimal", "low", "medium", "high", "xhigh", "max")
APP_DIR = Path(__file__).resolve().parent.parent  # app/ops_investigator
AWS_DOCS_MCP_URL = "https://knowledge-mcp.global.api.aws"


class RuntimeConfigError(RuntimeError):
    """Unrecoverable configuration problem; the message carries the fix."""


@dataclass(frozen=True)
class Runtime:
    profile: Literal["local", "cloud"]
    region: str | None
    memory_id: str | None
    workspace: Path
    app_dir: Path
    model: str
    effort: str
    session_id: str
    actor_id: str
    mcp_servers: dict | None


def detect(session_id: str, actor_id: str, env: Mapping[str, str] | None = None) -> Runtime:
    env = os.environ if env is None else env
    memory_id = env.get(MEMORY_ENV) or None
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None
    profile: Literal["local", "cloud"] = "cloud" if memory_id else "local"
    if profile == "cloud" and not region:
        raise RuntimeConfigError(
            "AWS_REGION is not set inside the runtime. Fix: add {name: AWS_REGION, value: <region>} to the runtime's "
            "envVars in agentcore/agentcore.json (P5)."
        )
    effort = env.get("HARNESS_EFFORT", DEFAULT_EFFORT)
    if effort not in EFFORTS:
        raise RuntimeConfigError(f"HARNESS_EFFORT={effort!r} is not one of {EFFORTS}.")
    workspace = Path(tempfile.gettempdir()) / "ops-workspace" if profile == "cloud" else APP_DIR
    mcp_servers = None
    if env.get("ENABLE_AWS_DOCS_MCP") == "1":
        mcp_servers = {"aws-docs": {"url": AWS_DOCS_MCP_URL, "transport": "streamable-http", "continue_on_error": True}}
    return Runtime(
        profile=profile,
        region=region,
        memory_id=memory_id,
        workspace=workspace,
        app_dir=APP_DIR,
        model=env.get("HARNESS_MODEL", DEFAULT_MODEL),
        effort=effort,
        session_id=session_id,
        actor_id=actor_id,
        mcp_servers=mcp_servers,
    )


def enter_workspace(rt: Runtime) -> Path:
    """Make the harness's cwd writable (P2). In cloud, /app is read-only for the non-root user.

    The harness `environment` plugin reads AGENTS.md from cwd, so copy it along.
    """
    if rt.profile == "local":
        return rt.workspace
    rt.workspace.mkdir(parents=True, exist_ok=True)
    src = rt.app_dir / "AGENTS.md"
    if src.exists():
        shutil.copyfile(src, rt.workspace / "AGENTS.md")
    os.chdir(rt.workspace)
    return rt.workspace


def builtin_tools(rt: Runtime) -> dict[str, Any]:
    """P21: a mapping *edits* the harness defaults; only `{"*": False, ...}` pins the set."""
    if rt.profile == "cloud":
        return {
            "*": False,
            "read": True,
            "programmatic_tool_caller": {
                "allowed_tools": ["aws_call", "logs_insights", "cost_explorer"],
                "timeout": 60,
            },
        }
    # Local keeps shell (the guard vets it) and read/web_fetch for exploration; write and edit have no read-only use.
    return {"subagent": False, "web_search": False, "write": False, "edit": False}


def guard_for(rt: Runtime) -> ReadOnlyGuard:
    """Cloud denies the dangerous builtins outright; local keeps them (the guard still vets aws_call and shell)."""
    from ops_investigator.guard import ReadOnlyGuard

    return ReadOnlyGuard() if rt.profile == "cloud" else ReadOnlyGuard(denied_tools=frozenset())


def skills_option(rt: Runtime) -> str:
    return str(rt.app_dir / "skills")


def session_option(rt: Runtime) -> SessionManager | dict[str, str]:
    if rt.profile == "local":
        return {"id": rt.session_id, "dir": str(rt.app_dir / ".agent" / "sessions")}
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    return AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(
            memory_id=rt.memory_id,
            session_id=rt.session_id,
            actor_id=rt.actor_id,
            retrieval_config=None,  # single reader: the harness stores (P3)
            async_mode=True,  # never block the SSE stream (P14)
            filter_restored_tool_context=True,  # dead offload references after a microVM restart (P23)
        ),
        rt.region,
    )


def memory_option(rt: Runtime) -> dict[str, Any]:
    if rt.profile == "local":
        return {"dir": str(rt.app_dir / ".agent" / "memory")}
    from bedrock_agentcore.memory.integrations.strands.memorystore.factory import create_agentcore_memory_stores

    stores = create_agentcore_memory_stores(
        memory_id=rt.memory_id,
        actor_id=rt.actor_id,
        session_id=rt.session_id,
        namespaces=[
            {"namespace": f"/preferences/{rt.actor_id}/", "name": "preferences", "max_search_results": 5},
            {"namespace": f"/facts/{rt.actor_id}/", "name": "facts", "max_search_results": 5},
        ],
        extraction=False,  # AgentCore strategies are the single writer (P3)
        region_name=rt.region,
    )
    return {"stores": stores}
