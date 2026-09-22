#!/usr/bin/env python3
"""Get a Cognito JWT for the demo user created by `scripts/auth_bootstrap.py` (spec 4.4).

Usage:
    DEMO_PASSWORD='...' uv run python scripts/get_token.py --region us-west-2 --client-id <app client id>

Not `token.py`: running a script puts its own directory first on `sys.path`, and a `token.py` there
shadows the stdlib `token` module that `tokenize` (and so `linecache`, and so every traceback) imports.
That breaks *every* script in this directory, not just this one.

Prints the **access** token by default (`--id-token` for the ID token, `--json` for both plus expiry).
AgentCore's CUSTOM_JWT authorizer matches `allowedClients` against the token's `client_id` claim, which
only the access token carries -- an ID token carries `aud` instead. The first cloud smoke confirmed it:
the access token returned 200 and the ID token returned 401 `Claim 'client_id' value mismatch with
configuration` (PITFALLS P60). Both are still printed by `--json`, so the two can be compared without a
second sign-in.

The container reads the actor id from the token's `sub`, which is the same for both.
"""

from __future__ import annotations

import argparse
import json
import os

import boto3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--client-id", default=os.environ.get("CLIENT_ID"))
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD"))
    parser.add_argument("--json", action="store_true", help="print id_token, access_token and expires_in")
    parser.add_argument("--id-token", action="store_true", help="print the ID token instead of the access token")
    args = parser.parse_args(argv)
    if not args.region:
        parser.error("--region or AWS_REGION is required")
    if not args.client_id:
        parser.error("--client-id or CLIENT_ID is required (auth_bootstrap.py prints it)")
    if not args.password:
        parser.error("--password or DEMO_PASSWORD is required")

    idp = boto3.client("cognito-idp", region_name=args.region)
    result = idp.initiate_auth(
        ClientId=args.client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.username, "PASSWORD": args.password},
    )["AuthenticationResult"]
    if args.json:
        print(
            json.dumps(
                {
                    "id_token": result["IdToken"],
                    "access_token": result["AccessToken"],
                    "expires_in": result["ExpiresIn"],
                }
            )
        )
    else:
        print(result["IdToken"] if args.id_token else result["AccessToken"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
