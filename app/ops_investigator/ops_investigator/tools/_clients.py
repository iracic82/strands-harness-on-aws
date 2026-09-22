"""boto3 clients for the tools: bounded timeouts, standard retries, CloudTrail-correlatable user agent (spec 6.4)."""

from __future__ import annotations

import boto3
from botocore.client import BaseClient
from botocore.config import Config


def client_config(*, session_id: str = "", actor_id: str = "") -> Config:
    return Config(
        connect_timeout=5,
        read_timeout=30,
        retries={"mode": "standard", "max_attempts": 3},
        user_agent_extra=f"strands-harness-on-aws session={session_id} actor={actor_id}",
    )


def make_client(service: str, region: str | None, *, session_id: str = "", actor_id: str = "") -> BaseClient:
    return boto3.client(service, region_name=region, config=client_config(session_id=session_id, actor_id=actor_id))
