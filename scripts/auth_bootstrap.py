#!/usr/bin/env python3
"""Create (idempotently) the Cognito user pool + app client that AgentCore's CUSTOM_JWT inbound auth
validates against, then point `agentcore/agentcore.json` at it (spec 2, 4.4).

Usage:
    DEMO_PASSWORD='...' uv run python scripts/auth_bootstrap.py --region us-west-2 [--project harnessaws]
    uv run python scripts/auth_bootstrap.py --reset-placeholders   # before committing

The config is edited structurally (load, mutate, `json.dump(indent=2)`) rather than by string replacement:
the committed placeholders have to stay schema-valid for CDK -- `discoveryUrl` is a real https URL ending
in the OIDC well-known path that merely *contains* the token -- so there is no bare token to regex for.

`--reset-placeholders` puts the placeholders back: a real user pool id or app client id must never be
committed (the repo-hygiene gate rejects it). It restores the values, not the AgentCore CLI's original
whitespace; `git checkout agentcore/agentcore.json` drops both in one step.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agentcore" / "agentcore.json"

PLACEHOLDER = "REPLACED_BY_auth_bootstrap"
PLACEHOLDER_REGION = "us-west-2"  # only ever part of the placeholder URL; the real one comes from --region
DISCOVERY_ENV = "COGNITO_DISCOVERY_URL"  # the A2A container reads this to advertise the card's security scheme


def discovery_url(region: str, pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"


def placeholder_discovery_url() -> str:
    return discovery_url(PLACEHOLDER_REGION, PLACEHOLDER)


def find_pool(idp: Any, name: str) -> str | None:
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == name:
                return str(pool["Id"])
    return None


def find_client(idp: Any, pool_id: str, name: str) -> str | None:
    for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id, MaxResults=60):
        for client in page["UserPoolClients"]:
            if client["ClientName"] == name:
                return str(client["ClientId"])
    return None


def ensure_pool(idp: Any, name: str) -> str:
    found = find_pool(idp, name)
    if found:
        return found
    created = idp.create_user_pool(
        PoolName=name,
        Policies={"PasswordPolicy": {"MinimumLength": 8}},
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
    )
    return str(created["UserPool"]["Id"])


def ensure_client(idp: Any, pool_id: str, name: str) -> str:
    found = find_client(idp, pool_id, name)
    if found:
        return found
    created = idp.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=name,
        GenerateSecret=False,  # a public CLI client: the secret would have to ship with the demo
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )
    return str(created["UserPoolClient"]["ClientId"])


def ensure_user(idp: Any, pool_id: str, username: str, password: str) -> None:
    try:
        idp.admin_get_user(UserPoolId=pool_id, Username=username)
    except idp.exceptions.UserNotFoundException:
        idp.admin_create_user(UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS")
    # Permanent, so the user is CONFIRMED and USER_PASSWORD_AUTH returns tokens instead of a challenge.
    idp.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)


def apply_auth(config: dict, *, discovery: str, client_id: str, env_discovery: str | None = None) -> list[str]:
    """Point every CUSTOM_JWT runtime at one user pool. Returns the names of the runtimes it changed.

    `env_discovery` defaults to `discovery`; it exists because the two placeholders differ. The env var
    is read by our own container long after deploy, so its placeholder is the bare token, while
    `discoveryUrl` is validated by the CDK schema and so has to stay URL-shaped even before bootstrap.
    """
    env_value = discovery if env_discovery is None else env_discovery
    changed: list[str] = []
    for runtime in config.get("runtimes", []):
        touched = False
        jwt = (runtime.get("authorizerConfiguration") or {}).get("customJwtAuthorizer")
        if isinstance(jwt, dict):
            jwt["discoveryUrl"] = discovery
            jwt["allowedClients"] = [client_id]
            touched = True
        for var in runtime.get("envVars", []):
            if var.get("name") == DISCOVERY_ENV:
                var["value"] = env_value
                touched = True
        if touched:
            changed.append(str(runtime.get("name", "")))
    return changed


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2) + "\n")


def reset_placeholders(path: Path) -> list[str]:
    config = load_config(path)
    changed = apply_auth(
        config, discovery=placeholder_discovery_url(), client_id=PLACEHOLDER, env_discovery=PLACEHOLDER
    )
    write_config(path, config)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--project", default="harnessaws")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD"))
    parser.add_argument("--config", default=str(CONFIG), help="path to agentcore.json")
    parser.add_argument(
        "--reset-placeholders",
        action="store_true",
        help="put the REPLACED_BY_auth_bootstrap placeholders back and touch no AWS resource",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if args.reset_placeholders:
        changed = reset_placeholders(config_path)
        print(json.dumps({"reset": changed, "config": str(config_path)}, indent=2))
        return 0

    if not args.region:
        parser.error("--region or AWS_REGION is required")
    if not args.password:
        parser.error("--password or DEMO_PASSWORD is required (min 8 chars, upper/lower/digit/symbol)")

    idp = boto3.client("cognito-idp", region_name=args.region)
    pool_id = ensure_pool(idp, f"{args.project}-users")
    client_id = ensure_client(idp, pool_id, f"{args.project}-cli")
    ensure_user(idp, pool_id, args.username, args.password)
    discovery = discovery_url(args.region, pool_id)

    config = load_config(config_path)
    changed = apply_auth(config, discovery=discovery, client_id=client_id)
    write_config(config_path, config)
    print(
        json.dumps(
            {
                "user_pool_id": pool_id,
                "client_id": client_id,
                "discovery_url": discovery,
                "username": args.username,
                "runtimes_updated": changed,
                "reminder": "run with --reset-placeholders before committing agentcore/agentcore.json",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
