"""The gate that keeps this repo cloneable: nothing account-specific is committed, and the files that
would carry an account id are git-ignored rather than merely absent today.

`make hygiene` runs this file and then gitleaks. These two checks catch different things: gitleaks
knows what a credential looks like, and this file knows what *this* repo's local state looks like
(account ids, runtime ARNs, Cognito pool ids, and the placeholders `auth_bootstrap` rewrites).

Only tracked files are scanned, because only tracked files get published. `docs/superpowers/` is the
one exception: it is the design history, it names the owner's account, and it is stripped from the
published archive by `.gitattributes export-ignore`.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# A path prefix, matched against `git ls-files` output.
ALLOW = ("docs/superpowers/",)
# Lockfiles pin hashes and PyPI URLs that trip the 12-digit rule with nothing to fix; images are binary.
SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".gif", ".ico", ".pdf", ".woff", ".woff2")
# The placeholder account id that documentation and the example target are allowed to use.
EXAMPLE_ACCOUNT = "123456789012"

# Bounded by non-digits so a 13+ digit run (a nanosecond timestamp, a hash) is not a 12-digit account.
ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN_WITH_ACCOUNT = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:")
USER_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+/")
# A Cognito user pool id: the region, an underscore, nine characters. No example is spelled out here,
# because this file is tracked too, and the first thing this test caught was a sample in its own comment.
COGNITO_ID = re.compile(r"\b[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{9}\b")

PLACEHOLDER = "REPLACED_BY_auth_bootstrap"
PUBLIC_URL_PLACEHOLDER = "REPLACED_AFTER_FIRST_DEPLOY"

# A source archive (`git archive`, or GitHub's "Download ZIP") has no git metadata, so there is
# nothing to scan and nothing anyone could commit from it. In every checkout, including CI, it runs.
pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _tracked() -> list[str]:
    return [
        path
        for path in _git("ls-files").splitlines()
        if path and not path.startswith(ALLOW) and not path.endswith(SKIP_SUFFIXES)
    ]


def _is_placeholder(match: str) -> bool:
    return EXAMPLE_ACCOUNT in match or "<your" in match


def test_no_account_specific_values_committed():
    bad = []
    for rel in _tracked():
        path = ROOT / rel
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue  # binary, or a symlink to somewhere that does not exist in this checkout
        for rx in (ARN_WITH_ACCOUNT, USER_PATH, COGNITO_ID):
            bad += [f"{rel}: {m.group(0)}" for m in rx.finditer(text) if not _is_placeholder(m.group(0))]
        bad += [f"{rel}: account id {m.group(0)}" for m in ACCOUNT_ID.finditer(text) if not _is_placeholder(m.group(0))]
    assert bad == [], "\n".join(bad)


def test_generated_files_are_ignored():
    """Ignored, not merely absent: the next `git add -A` after a deploy is what this prevents."""
    for rel in (
        "agentcore/aws-targets.json",
        "agentcore/.cli/deployed-state.json",
        "agentcore/.env.local",
        ".env",
        ".agent/x",
        "app/ops_investigator/.agent/x",
    ):
        assert subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT).returncode == 0, f"{rel} must be ignored"


def test_no_generated_file_is_tracked():
    """The mirror image of the check above: an ignore rule added after the file was committed is a no-op."""
    leaked = re.compile(r"(^|/)(aws-targets\.json|\.env)$|(^|/)\.cli/|(^|/)\.agent/")
    assert [rel for rel in _git("ls-files").splitlines() if leaked.search(rel)] == []


def test_agentcore_json_has_only_placeholders():
    """After a deploy, `agentcore.json` holds a real discovery URL, client id, and A2A URL. Put them
    back with `scripts/setup.py --reset-placeholders` (or `git checkout agentcore/agentcore.json`).

    The placeholders stay schema-valid -- `discoveryUrl` is a real https OIDC URL that merely
    *contains* the token -- so this asserts the token is present rather than that a URL is absent.
    """
    path = ROOT / "agentcore" / "agentcore.json"
    text = path.read_text()
    assert not COGNITO_ID.search(text), "a real Cognito pool id is committed in agentcore.json"
    config = json.loads(text)
    for runtime in config["runtimes"]:
        name = runtime["name"]
        jwt = runtime["authorizerConfiguration"]["customJwtAuthorizer"]
        assert PLACEHOLDER in jwt["discoveryUrl"], name
        assert jwt["allowedClients"] == [PLACEHOLDER], name
        env = {var["name"]: var["value"] for var in runtime["envVars"]}
        if "COGNITO_DISCOVERY_URL" in env:
            assert env["COGNITO_DISCOVERY_URL"] == PLACEHOLDER, name
        if "A2A_PUBLIC_URL" in env:
            assert env["A2A_PUBLIC_URL"] == PUBLIC_URL_PLACEHOLDER, name
