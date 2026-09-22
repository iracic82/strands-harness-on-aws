"""The local dev server against real Bedrock and real AWS reads (spec 7, 10.1). Marked `live`: it costs
money and needs credentials, so `addopts = -m 'not live'` keeps it out of the default run.

    AWS_PROFILE=<your-profile> AWS_REGION=us-west-2 uv run pytest -m live tests/live -v

It asserts the three things only a real turn can show: the agent reaches for `aws_call`, the read-only
guard refuses a mutating call instead of making it, and the harness leaves its session state on disk.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from scripts.invoke import ACTOR_HEADER, SESSION_HEADER, iter_frames

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "app" / "ops_investigator" / ".agent"
HOST, PORT = "127.0.0.1", 8080
BASE = f"http://{HOST}:{PORT}"
SESSION = "live" + "0" * 40  # AgentCore's floor is 33 characters (P9)
ACTOR = "live-test"
TURN_TIMEOUT = 300.0


def _listening() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


@pytest.fixture(scope="module")
def server():
    if not (os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID")):
        pytest.skip("no AWS credentials")
    if _listening():
        pytest.skip(f"something is already listening on {BASE}")
    shutil.rmtree(AGENT_DIR, ignore_errors=True)
    # AWS_DEFAULT_REGION as well as AWS_REGION: botocore 1.43 reads only the former, so a client built
    # without an explicit region (the harness's own Bedrock client) would otherwise land in whatever
    # region ~/.aws/config gives the profile, and the model may not be enabled there.
    region = os.environ.get("AWS_REGION", "us-west-2")
    env = {**os.environ, "AWS_REGION": region, "AWS_DEFAULT_REGION": region}
    process = subprocess.Popen(["uv", "run", "python", "app/ops_investigator/main.py"], cwd=ROOT, env=env)
    try:
        for _ in range(90):
            if process.poll() is not None:
                pytest.fail(f"dev server exited with {process.returncode} before serving")
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{BASE}/ping", timeout=1).status_code == 200:
                    break
            time.sleep(1)  # outside the suppress: a non-200 answer must back off too, not spin
        else:
            pytest.fail(f"{BASE}/ping never answered")
        yield BASE
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


def _turn(base: str, prompt: str) -> list[dict]:
    headers = {SESSION_HEADER: SESSION, ACTOR_HEADER: ACTOR}
    with httpx.stream(
        "POST", f"{base}/invocations", json={"prompt": prompt}, headers=headers, timeout=TURN_TIMEOUT
    ) as response:
        assert response.status_code == 200, response.read()
        frames = [frame for frame in iter_frames(response.iter_lines()) if isinstance(frame, dict)]
    print("\n".join(str(frame) for frame in frames))  # the frames are the evidence; keep them in -v output
    return frames


def _types(frames: list[dict]) -> list[str]:
    return [str(frame.get("type", "")) for frame in frames]


def _text(frames: list[dict]) -> str:
    return "".join(str(frame.get("delta", "")) for frame in frames if frame.get("type") == "text")


def test_three_prompts_and_local_state(server):
    identity = _turn(server, "Which AWS account and region are you looking at? Use aws_call sts get_caller_identity.")
    assert _types(identity)[-1] == "done", _types(identity)
    assert "error" not in _types(identity)[:-1], identity
    assert any(frame.get("name") == "aws_call" for frame in identity if frame.get("type") == "tool_use"), identity
    # `stream_async` never yields a tool result (P51): every `tool_result` frame here was sourced from the
    # after-tool hook, and it has to land after the `tool_use` frame for the same call.
    succeeded = [frame for frame in identity if frame.get("type") == "tool_result" and frame.get("name") == "aws_call"]
    assert succeeded and all(frame.get("status") == "success" for frame in succeeded), identity
    assert _types(identity).index("tool_use") < _types(identity).index("tool_result"), _types(identity)

    mutate = _turn(
        server, "Try to run aws_call ec2 terminate_instances with InstanceIds ['i-0']. Report what happened."
    )
    assert _types(mutate)[-1] == "done", _types(mutate)
    # `ReadOnlyGuard` denies in `before_tool_call`, so the tool never runs. The SDK's `tool_cancel_event`
    # for the denial reaches the wire as a `denied` frame (EventMapper._map_tool_cancel), and the error
    # `ToolResult` the executor synthesises for the same call reaches it through the after-tool hook.
    attempted = [frame for frame in mutate if frame.get("type") == "tool_use" and frame.get("name") == "aws_call"]
    denied = [frame for frame in mutate if frame.get("type") == "denied"]
    results = [frame for frame in mutate if frame.get("type") == "tool_result" and frame.get("name") == "aws_call"]
    ran = [frame for frame in results if frame.get("status") != "error"]
    assert attempted and not ran, mutate
    assert denied and denied[0].get("tool") == "aws_call", mutate
    assert "read-only" in denied[0].get("reason", "").lower(), mutate
    assert results and results[0].get("status") == "error", mutate
    said = _text(mutate).lower()
    assert any(marker in said for marker in ("denied", "not a permitted", "blocked", "read-only")), mutate

    summary = _turn(server, "Summarise what you learned about this account in one line.")
    assert _types(summary)[-1] == "done", _types(summary)
    assert _text(summary).strip(), summary

    sessions = AGENT_DIR / "sessions"
    assert sessions.is_dir() and any(sessions.rglob("*")), f"no harness session state under {sessions}"
