#!/usr/bin/env python3
"""Fetch the A2A agent card, and optionally send one message to it (a2a-sdk 1.x, spec 4.5).

Usage:
    uv run python scripts/a2a_client.py --url http://localhost:9000                  # card only
    uv run python scripts/a2a_client.py --url "$A2A_URL" --token "$TOKEN" --prompt "what is broken?"

This runtime speaks A2A **1.0**: the method is `SendMessage` (0.3 spelled it `message/send`) and every
request carries `A2A-Version: 1.0`, which the SDK client sends for us. In 1.0 the protocol version lives
on each entry of `supportedInterfaces`, not at the top of the card; a card with a top-level
`protocolVersion` is a 0.3 card and this client will say so rather than pretend (P22).

Without `--prompt` nothing is sent and nothing is billed -- that is what `make a2a-card` runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.parse
import uuid

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import AgentCard, Role, SendMessageRequest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

PROTOCOL_VERSION = "1.0"
TIMEOUT = 900.0  # the server's own wall clock is 600s and it reports a timeout as a task state
CARD_FIELDS = ("name", "version", "description", "supportedInterfaces", "securitySchemes", "securityRequirements")


def card_url_for(url: str) -> str:
    """Where the card lives for a given A2A base URL.

    The well-known path goes on the end of the *path*, not the end of the string: a deployed AgentCore
    runtime is addressed as `.../runtimes/<arn>/invocations?qualifier=DEFAULT`, and its card is at
    `.../invocations/.well-known/agent-card.json?qualifier=DEFAULT` (the request URI of the service's
    own GetAgentCard operation). A plain server has no query string and is unaffected.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=parts.path.rstrip("/") + AGENT_CARD_WELL_KNOWN_PATH))


def card_protocol_version(card: dict) -> str | None:
    """1.0 puts the version on each interface; 0.3 put it at the top of the card."""
    interfaces = card.get("supportedInterfaces")
    if isinstance(interfaces, list) and interfaces and isinstance(interfaces[0], dict):
        version = interfaces[0].get("protocolVersion")
        if version:
            return str(version)
    version = card.get("protocolVersion")
    return str(version) if version else None


def _part_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))


def summarize_event(event: dict) -> str:
    """One line per streamed response, plus a line per artifact on a Task. The wire shape is the protobuf
    `StreamResponse` as JSON, so the payload is under exactly one of task / message / statusUpdate /
    artifactUpdate."""
    if "task" in event:
        task = event["task"]
        state = (task.get("status") or {}).get("state", "")
        lines = [f"[task] id={task.get('id', '')} context={task.get('contextId', '')} state={state}"]
        # A non-streaming SendMessage answers with one Task and nothing else, so the answer arrives as
        # `task.artifacts` rather than as its own `artifactUpdate` event. Reporting only the state here
        # printed the task id and threw the answer away (P52).
        lines += [
            f"[artifact] {artifact.get('name', '')}: {_part_text(artifact.get('parts'))}"
            for artifact in task.get("artifacts") or []
        ]
        return "\n".join(lines)
    if "statusUpdate" in event:
        status = event["statusUpdate"].get("status") or {}
        text = _part_text((status.get("message") or {}).get("parts"))
        return f"[status] {status.get('state', '')}" + (f": {text}" if text else "")
    if "artifactUpdate" in event:
        artifact = event["artifactUpdate"].get("artifact") or {}
        return f"[artifact] {artifact.get('name', '')}: {_part_text(artifact.get('parts'))}"
    if "message" in event:
        return f"[message] {_part_text(event['message'].get('parts'))}"
    return "[event] " + json.dumps(event, sort_keys=True)


async def fetch_card(http: httpx.AsyncClient, card_url: str) -> dict:
    response = await http.get(card_url)
    response.raise_for_status()
    card = response.json()
    if not isinstance(card, dict):
        raise ValueError(f"{card_url} did not return a JSON object")
    return card


async def send(http: httpx.AsyncClient, card: dict, prompt: str, context_id: str) -> int:
    """One SendMessage turn. Non-streaming: the server publishes the answer as a single artifact.

    The client is built from the card we already fetched rather than from a URL, so the SDK does not
    resolve the card a second time (and does not have to guess where it lives).
    """
    try:
        agent = ParseDict(card, AgentCard(), ignore_unknown_fields=True)
    except ParseError as e:
        print(f"card is not 1.0-shaped, so this client cannot talk to it; see PITFALLS P22 ({e})")
        return 1
    client = await create_client(agent, ClientConfig(streaming=False, httpx_client=http))
    try:
        message = new_text_message(prompt, role=Role.ROLE_USER)
        # The server uses the context id as the AgentCore session id and rejects anything under 33 chars.
        message.context_id = context_id
        async for event in client.send_message(SendMessageRequest(message=message)):
            print(summarize_event(MessageToDict(event)), flush=True)
    finally:
        await client.close()
    return 0


async def run(*, card_url: str, token: str | None, prompt: str | None, context_id: str) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as http:
        try:
            card = await fetch_card(http, card_url)
        except (httpx.HTTPError, ValueError) as e:
            print(f"could not read the agent card at {card_url}: {type(e).__name__}: {e}")
            return 1
        print(json.dumps({k: card[k] for k in CARD_FIELDS if k in card}, indent=2))
        version = card_protocol_version(card)
        if version != PROTOCOL_VERSION:
            print(f"WARNING: card advertises protocolVersion={version!r}; this client speaks 1.0 (P22)")
        if not prompt:
            return 0
        print(f"context_id={context_id}")
        return await send(http, card, prompt, context_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="A2A base URL (the card's supportedInterfaces[0].url)")
    parser.add_argument("--card-url", default=None, help="override where the card is fetched from")
    parser.add_argument("--token", default=None, help="Cognito JWT for a CUSTOM_JWT runtime")
    parser.add_argument("--prompt", default=None, help="send one message; omit to only read the card")
    parser.add_argument("--context", default=None, help="A2A context id (the session id); 33 chars minimum")
    args = parser.parse_args(argv)
    if not args.url.strip():
        parser.error("--url is empty; `make a2a-card` needs A2A_URL set to the deployed A2A endpoint")
    return asyncio.run(
        run(
            card_url=args.card_url or card_url_for(args.url),
            token=args.token,
            prompt=args.prompt,
            context_id=args.context or uuid.uuid4().hex + uuid.uuid4().hex[:8],
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
