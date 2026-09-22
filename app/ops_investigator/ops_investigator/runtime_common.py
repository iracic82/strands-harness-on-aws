"""Settings and startup steps every protocol entrypoint needs (spec 4.4, 4.5).

`http_app` (AgentCore HTTP) and `a2a_app` (A2A 1.0) serve the same agent over different wires, so the
wall clock, the profile rule, the logging setup and the local workspace step live here rather than
being answered twice.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from ops_investigator import platform

log = logging.getLogger("ops_investigator.runtime")

DEFAULT_MAX_SECONDS = 600.0
MIN_SESSION_LEN = 33  # AgentCore's floor for runtimeSessionId, and so for any id used as one (P9)
CLOSING_PROMPT = "You hit the budget. Summarise findings so far, likely cause, and what remains unchecked."


def _max_seconds(raw: str) -> float:
    """A typo in HARNESS_MAX_SECONDS must not stop the container from serving."""
    try:
        return float(raw)
    except ValueError:
        log.error("HARNESS_MAX_SECONDS=%r is not a number; using %s", raw, DEFAULT_MAX_SECONDS)
        return DEFAULT_MAX_SECONDS


def max_seconds(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    return _max_seconds(env.get("HARNESS_MAX_SECONDS", str(DEFAULT_MAX_SECONDS)))


def profile(env: Mapping[str, str] | None = None) -> str:
    """The identity source depends on the profile, and the profile is known before the actor id is.

    Read per request, never frozen at import: `platform.detect` needs an actor id that a header has not
    been parsed for yet, and it raises when MEMORY_… is set without AWS_REGION — at import time that
    would kill the container before it could report the problem.
    """
    env = os.environ if env is None else env
    return "cloud" if env.get(platform.MEMORY_ENV) else "local"


def configure_logging() -> None:
    """Neither `BedrockAgentCoreApp` nor uvicorn installs a handler for this package: without this the
    preflight and turn lines are dropped. Only this package goes to INFO; the SDKs stay at WARNING."""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("ops_investigator").setLevel(logging.INFO)


def enter_local_app_dir() -> None:
    """The harness `environment` plugin reads AGENTS.md from cwd, and local runs start at the repo root."""
    if profile() != "local":
        return
    os.chdir(platform.APP_DIR)
    log.info("local profile: serving from %s so AGENTS.md is picked up from cwd", platform.APP_DIR)
