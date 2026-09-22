#!/usr/bin/env python3
"""Delete one actor's long-term records and raw events from AgentCore Memory (spec 4.3).

Usage:
    uv run python scripts/memory_wipe.py --region us-west-2 --memory-id "$MEMORY_ID" --actor "$ACTOR" [--dry-run]

The admin escape hatch behind "forget what you know about me": memory is keyed by actor id, and the
namespaces come straight from the strategies in `agentcore/agentcore.json`. Extraction is asynchronous,
so a record can still appear minutes after a wipe if an event was in flight; run it again if so.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from typing import Any

import boto3

# Mirrors the namespaceTemplates of the USER_PREFERENCE and SEMANTIC strategies in agentcore.json.
NAMESPACE_TEMPLATES = ("/preferences/{actor}/", "/facts/{actor}/")


def namespaces(actor: str) -> list[str]:
    return [template.format(actor=actor) for template in NAMESPACE_TEMPLATES]


def _paginate(client: Any, operation: str, key: str, **kwargs: Any) -> Iterator[dict]:
    """Every list operation used here has a paginator in the installed boto3; this keeps the three
    call sites identical anyway, and degrades to a manual nextToken loop if one ever disappears."""
    if client.can_paginate(operation):
        for page in client.get_paginator(operation).paginate(**kwargs):
            yield from page.get(key, [])
        return
    token = None
    while True:
        page = getattr(client, operation)(**kwargs, **({"nextToken": token} if token else {}))
        yield from page.get(key, [])
        token = page.get("nextToken")
        if not token:
            return


def wipe_records(client: Any, memory_id: str, actor: str, *, dry_run: bool = False) -> int:
    deleted = 0
    for namespace in namespaces(actor):
        for record in _paginate(
            client, "list_memory_records", "memoryRecordSummaries", memoryId=memory_id, namespace=namespace
        ):
            record_id = record["memoryRecordId"]
            print(f"record {namespace}{record_id}")
            if not dry_run:
                client.delete_memory_record(memoryId=memory_id, memoryRecordId=record_id)
            deleted += 1
    return deleted


def wipe_events(client: Any, memory_id: str, actor: str, *, dry_run: bool = False) -> int:
    deleted = 0
    for session in _paginate(client, "list_sessions", "sessionSummaries", memoryId=memory_id, actorId=actor):
        session_id = session["sessionId"]
        for event in _paginate(
            client, "list_events", "events", memoryId=memory_id, actorId=actor, sessionId=session_id
        ):
            event_id = event["eventId"]
            print(f"event {session_id}/{event_id}")
            if not dry_run:
                client.delete_event(memoryId=memory_id, actorId=actor, sessionId=session_id, eventId=event_id)
            deleted += 1
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True)
    parser.add_argument("--memory-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--dry-run", action="store_true", help="list what would go, delete nothing")
    args = parser.parse_args(argv)
    client = boto3.client("bedrock-agentcore", region_name=args.region)
    deleted = wipe_records(client, args.memory_id, args.actor, dry_run=args.dry_run)
    deleted += wipe_events(client, args.memory_id, args.actor, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {deleted} records/events for actor {args.actor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
