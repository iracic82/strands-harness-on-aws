#!/usr/bin/env python3
"""Send one prompt to the agent and print the v1 SSE frames it streams back (spec 4.4).

Usage:
    # deployed, CUSTOM_JWT runtime (the usual case): plain HTTPS with the Cognito bearer
    uv run python scripts/invoke.py --transport https --arn "$ARN" --region us-west-2 \\
        --token "$(uv run python scripts/get_token.py --region us-west-2 --client-id "$CLIENT_ID")" --prompt "..."

    # deployed, IAM-authorized runtime: the SigV4-signed data-plane API
    uv run python scripts/invoke.py --arn "$ARN" --region us-west-2 --prompt "..."

    # whatever `make dev` is serving on localhost
    uv run python scripts/invoke.py --dev --prompt "..."

Transports
    boto3 (default)  `bedrock-agentcore.invoke_agent_runtime`, SigV4-signed with the caller's IAM identity.
                     With `--token` the client is switched to UNSIGNED and the bearer is injected just
                     before signing: SigV4 deletes any Authorization header it finds (botocore/auth.py,
                     `_modify_request_before_signing`), so the two cannot be combined.
    https            The same data-plane endpoint by hand, with `Authorization: Bearer <jwt>`. This is the
                     transport a CUSTOM_JWT runtime expects; `--token` is required.
    --dev            POST to the local dev server, which authenticates nobody and takes the actor id from
                     `X-Actor-Id`.

The session id is printed to stderr as `session_id=<id>` before the first frame so a caller (demo.sh) can
reuse it for a follow-up turn. Exit code is 1 if the stream carried any `error` frame.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import urllib.parse
import uuid
from collections.abc import Iterable, Iterator
from typing import Any, TextIO

import boto3
import httpx
from botocore import UNSIGNED
from botocore.config import Config

DEV_URL = "http://localhost:8080"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
ACTOR_HEADER = "X-Actor-Id"
DATA_PREFIX = "data: "
# The runtime's own wall clock (HARNESS_MAX_SECONDS) is 600s and it reports its timeout as a frame;
# the client has to outlive that or it hides the answer it was waiting for.
TIMEOUT = 900.0
INLINE_TYPES = ("text", "thinking")


class InvokeError(RuntimeError):
    """The transport failed before any frame arrived (bad status, refused connection, no credentials)."""


def new_session_id() -> str:
    """40 chars: AgentCore rejects a runtimeSessionId shorter than 33 (P9)."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def invocations_url(arn: str, region: str, qualifier: str = "DEFAULT") -> str:
    """The data-plane URL boto3 would sign, built by hand for the bearer-token path."""
    encoded = urllib.parse.quote(arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier={qualifier}"


def iter_frames(lines: Iterable[str | bytes]) -> Iterator[dict | str]:
    """Parse an SSE body into frames. A line that is not `data: <json object>` is yielded as raw text, so a
    proxy error page or a stack trace is shown rather than swallowed."""
    for line in lines:
        text = (line.decode() if isinstance(line, bytes) else line).rstrip("\r\n")
        if not text.strip():
            continue
        payload = text[len(DATA_PREFIX) :] if text.startswith(DATA_PREFIX) else text
        try:
            frame = json.loads(payload)
        except ValueError:
            yield text
            continue
        yield frame if isinstance(frame, dict) else text


class FramePrinter:
    """Renders v1 frames for a terminal.

    Stateful on purpose: `text` and `thinking` deltas stream inline with no newline of their own, so a
    bracketed line (a tool call, the metrics) has to close the current run first.
    """

    def __init__(self, raw: bool = False) -> None:
        self.raw = raw
        self.errors = 0
        self.done = False
        self._inline = ""  # the type currently streaming inline, or "" when the cursor is at a line start

    def render(self, frame: dict | str) -> str:
        if isinstance(frame, str):
            return self._break() + frame + "\n"
        type_ = str(frame.get("type", ""))
        if type_ == "error":
            self.errors += 1
        if self.raw:
            return self._break() + json.dumps(frame) + "\n"
        if type_ in INLINE_TYPES:
            return self._inline_run(type_, str(frame.get("delta", "")))
        if type_ == "done":
            self.done = True
            return self._break()
        return self._break() + self._line(type_, frame) + "\n"

    def finish(self) -> str:
        """Close a half-written inline run so the shell prompt starts on its own line."""
        return self._break()

    def _break(self) -> str:
        if not self._inline:
            return ""
        self._inline = ""
        return "\n"

    def _inline_run(self, type_: str, delta: str) -> str:
        if self._inline == type_:
            return delta
        prefix = self._break() + ("[thinking] " if type_ == "thinking" else "")
        self._inline = type_
        return prefix + delta

    def _line(self, type_: str, frame: dict) -> str:
        if type_ == "tool_use":
            keys = frame.get("input_keys")
            keys = ", ".join(str(k) for k in keys) if isinstance(keys, list) else ""
            return f"[tool_use] {frame.get('name', '')}({keys})"
        if type_ == "tool_result":
            return (
                f"[tool_result] {frame.get('name', '')} status={frame.get('status', '')} bytes={frame.get('bytes', 0)}"
            )
        if type_ == "denied":
            return f"[denied] {frame.get('name', '')}: {frame.get('reason', frame.get('message', ''))}"
        if type_ == "error":
            return f"[error] {frame.get('message', '')} (hint: {frame.get('hint', '')})"
        fields = {k: v for k, v in frame.items() if k not in ("v", "type")}
        return f"[{type_ or 'frame'}] " + json.dumps(fields, sort_keys=True)


def print_frames(lines: Iterable[str | bytes], *, raw: bool = False, out: TextIO | None = None) -> FramePrinter:
    """Stream frames to `out` as they arrive, stopping at `done` (always the last frame the app sends)."""
    out = sys.stdout if out is None else out
    printer = FramePrinter(raw)
    for frame in iter_frames(lines):
        out.write(printer.render(frame))
        out.flush()
        if printer.done:
            break
    out.write(printer.finish())
    out.flush()
    return printer


def boto3_config(token: str | None) -> Config | None:
    """UNSIGNED when a bearer token is in play: SigV4 deletes the Authorization header before signing."""
    return Config(signature_version=UNSIGNED) if token else None


def bearer_client(region: str, token: str | None) -> Any:
    """A `bedrock-agentcore` client that carries `Authorization: Bearer <jwt>` when there is one.

    `before-sign` is emitted even for an UNSIGNED client, and is the last hook before the request goes
    out, so the header survives; with the default SigV4 signer it would not.
    """
    client = boto3.client("bedrock-agentcore", region_name=region, config=boto3_config(token))
    if token:
        client.meta.events.register(
            "before-sign.bedrock-agentcore.InvokeAgentRuntime",
            lambda request, **kwargs: request.headers.add_header("Authorization", f"Bearer {token}"),
        )
    return client


@contextlib.contextmanager
def _boto3_stream(args: argparse.Namespace, session: str, body: bytes) -> Iterator[Iterable[bytes]]:
    client = bearer_client(args.region, args.token)
    response = client.invoke_agent_runtime(agentRuntimeArn=args.arn, runtimeSessionId=session, payload=body)
    stream = response["response"]
    try:
        yield stream.iter_lines()
    finally:
        stream.close()


@contextlib.contextmanager
def _http_stream(url: str, headers: dict[str, str], body: bytes) -> Iterator[Iterable[str]]:
    try:
        with httpx.stream("POST", url, headers=headers, content=body, timeout=TIMEOUT) as response:
            if response.status_code >= 400:
                detail = response.read().decode(errors="replace")[:500]
                raise InvokeError(f"HTTP {response.status_code} from {url}: {detail}")
            yield response.iter_lines()
    except httpx.HTTPError as e:
        raise InvokeError(f"{type(e).__name__}: {e}") from e


@contextlib.contextmanager
def open_stream(args: argparse.Namespace, session: str) -> Iterator[Iterable[str | bytes]]:
    """Yield the chosen transport's line stream, keeping the connection open for the whole turn."""
    body = json.dumps({"prompt": args.prompt}).encode()
    if args.dev:
        headers = {SESSION_HEADER: session, ACTOR_HEADER: args.actor, "Content-Type": "application/json"}
        with _http_stream(args.url.rstrip("/") + "/invocations", headers, body) as lines:
            yield lines
        return
    if args.transport == "https":
        headers = {
            "Authorization": f"Bearer {args.token}",
            SESSION_HEADER: session,
            "Content-Type": "application/json",
        }
        with _http_stream(invocations_url(args.arn, args.region), headers, body) as lines:
            yield lines
        return
    with _boto3_stream(args, session, body) as lines:
        yield lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arn", default=None, help="agent runtime ARN (not needed with --dev)")
    parser.add_argument("--region", default=None)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--session", default=None, help="reuse a session id to continue a conversation")
    parser.add_argument("--token", default=None, help="Cognito JWT; required for --transport https")
    parser.add_argument("--transport", default="boto3", choices=["boto3", "https"])
    parser.add_argument("--dev", action="store_true", help="talk to the local dev server instead of AWS")
    parser.add_argument("--url", default=DEV_URL, help=f"dev server base URL (default {DEV_URL})")
    parser.add_argument("--actor", default="local-dev", help=f"{ACTOR_HEADER} for --dev; ignored in the cloud")
    parser.add_argument("--raw", action="store_true", help="print each frame as the JSON it arrived as")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dev and args.token:
        # Not an error: the dev server ignores Authorization entirely and takes the actor from a header,
        # so a token here silently does nothing rather than authenticating anyone.
        print("warning: --token is ignored with --dev; the dev server reads the actor from X-Actor-Id", file=sys.stderr)
    if not args.dev:
        if not args.arn:
            parser.error("--arn is required (or use --dev)")
        if not args.region:
            parser.error("--region is required (or use --dev)")
        if args.transport == "https" and not args.token:
            parser.error("--transport https needs --token (that is the point of it); scripts/get_token.py prints one")

    session = args.session or new_session_id()
    print(f"session_id={session}", file=sys.stderr)
    try:
        with open_stream(args, session) as lines:
            printer = print_frames(lines, raw=args.raw)
    except InvokeError as e:
        print(f"invoke failed: {e}", file=sys.stderr)
        return 1
    return 1 if printer.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
