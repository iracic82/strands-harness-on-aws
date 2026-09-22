#!/usr/bin/env python3
"""One-time local setup: detect the account and region, then write the two files a fresh clone needs.

Usage:
    AWS_PROFILE=<your-profile> uv run python scripts/setup.py --region us-west-2   # or: make setup
    uv run python scripts/setup.py --reset-placeholders    # before committing agentcore.json

It writes `agentcore/aws-targets.json` (the committed example, with the account id STS reports) and
`.env` (from `.env.example`). Both are git-ignored, because both carry the account id -- which is the
whole reason this script exists instead of a `cp` line in the README.

Nothing is clobbered: a generated file that already exists and already matches is left alone, and one
that differs is reported and kept until you pass `--force`. Run it as often as you like.

The region is not validated here. `make doctor` checks it against the list of regions AgentCore
actually runs in, and this script prints `next: make doctor` for exactly that reason.

`--reset-placeholders` delegates to `auth_bootstrap.reset_placeholders`, which owns the placeholder
spellings. It lives here too because `setup.py` is the command people remember.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import boto3

try:  # `python scripts/setup.py` puts scripts/ on sys.path; importing it as `scripts.setup` does not.
    from scripts import auth_bootstrap
except ImportError:
    import auth_bootstrap

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "agentcore" / "aws-targets.json"
TARGETS_EXAMPLE = ROOT / "agentcore" / "aws-targets.example.json"
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
DEFAULT_REGION = "us-west-2"
# The example's own description explains how to copy it, which reads wrong in the generated file.
TARGET_DESCRIPTION = "Written by scripts/setup.py (make setup). Never committed: it carries the AWS account id."


def detect_region(explicit: str | None) -> str:
    """`--region` wins, then `AWS_REGION`, then the profile's own default, then the repo's default."""
    return explicit or os.environ.get("AWS_REGION") or boto3.session.Session().region_name or DEFAULT_REGION


def render_targets(account: str, region: str) -> str:
    """The committed example, retargeted at the caller's account and region."""
    targets = json.loads(TARGETS_EXAMPLE.read_text())
    for target in targets:
        target["account"] = account
        target["region"] = region
        target["description"] = TARGET_DESCRIPTION
    return json.dumps(targets, indent=2) + "\n"


def render_env(account: str, region: str) -> str:
    return ENV_EXAMPLE.read_text().replace("<region>", region).replace("<account-id>", account)


def write_if_absent(path: Path, content: str, *, force: bool) -> str:
    """Write `content`, unless the file exists. Returns a word for what happened, for the caller to print.

    A file that already matches is reported as unchanged rather than rewritten, so re-running setup
    never shows up as a file modification. A file that differs is somebody's edit: keep it.
    """
    if path.exists() and not force:
        if path.read_text() == content:
            return "unchanged"
        return "kept (it differs from what setup would write; --force overwrites it)"
    path.write_text(content)
    return "written"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=None, help="deployment region; defaults to AWS_REGION or the profile's")
    parser.add_argument("--force", action="store_true", help="overwrite a generated file that already differs")
    parser.add_argument(
        "--reset-placeholders",
        action="store_true",
        help="put the REPLACED_BY_* placeholders back in agentcore.json and touch no AWS resource",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.reset_placeholders:
        changed = auth_bootstrap.reset_placeholders(auth_bootstrap.CONFIG)
        print(f"agentcore.json: placeholders restored in {', '.join(changed) or 'nothing'}")
        return 0

    region = detect_region(args.region)
    identity = boto3.client("sts", region_name=region).get_caller_identity()
    account = identity["Account"]
    print(f"agentcore/aws-targets.json: {write_if_absent(TARGETS, render_targets(account, region), force=args.force)}")
    print(f".env: {write_if_absent(ENV, render_env(account, region), force=args.force)}")
    caller = identity["Arn"].rsplit("/", 1)[-1]
    print(f"\naccount={account} region={region} caller={caller}\nnext: make doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
