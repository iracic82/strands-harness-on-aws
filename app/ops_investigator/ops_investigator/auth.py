"""Actor identity. Cloud: JWT `sub` from the allowlisted Authorization header (spec 4.4, P24).

The AgentCore CUSTOM_JWT authorizer validated the token before the container saw it; we only decode.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping

ACTOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ActorError(ValueError):
    pass


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ActorError("Authorization bearer token is not a JWT (expected three dot-separated segments).")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, UnicodeDecodeError) as e:
        raise ActorError("Authorization bearer token payload is not valid base64url JSON.") from e


def _validate(actor: str) -> str:
    if not ACTOR_RE.match(actor or ""):
        raise ActorError("actor id must match ^[A-Za-z0-9_-]{1,64}$ (memory namespaces are built from it).")
    return actor


def actor_from_headers(headers: Mapping[str, str], *, profile: str, payload_actor: str | None = None) -> str:
    if profile == "cloud":
        raw = _header(headers, "Authorization")
        if not raw or not raw.lower().startswith("bearer "):
            raise ActorError(
                "Missing Authorization: Bearer <jwt>. The runtime is configured for CUSTOM_JWT; get a token with "
                "`make token` (scripts/token.py) and pass it on every invoke."
            )
        return _validate(str(_jwt_claims(raw.split(" ", 1)[1].strip()).get("sub", "")))
    hdr = _header(headers, "X-Actor-Id")
    return _validate(hdr or payload_actor or "local-dev")
