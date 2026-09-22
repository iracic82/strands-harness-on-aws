"""The gate that keeps this repo cloneable: nothing account-specific is in it, and the files that would
carry an account id are git-ignored rather than merely absent today.

`make hygiene` runs this file and then gitleaks, twice (history and working tree). The two catch
different things: gitleaks knows what a credential looks like, and this file knows what *this* repo's
local state looks like -- account ids, runtime ARNs, Cognito pool ids, the owner's own profile name, and
the placeholders `auth_bootstrap` rewrites.

Every file git would publish is scanned, tracked or not: `git ls-files --cached --others
--exclude-standard`. There is no allowlisted directory. `docs/superpowers/` is design history and is
published with the rest of the repo, so it was scrubbed to the same placeholders everything else uses --
a clone gets it, and `export-ignore` would only have hidden it from `git archive`.

Two scans, because scrubbing the tip is not scrubbing the repository. `git push` publishes every reachable
commit, and gitleaks does not help here -- it knows what a credential looks like and an account id or a
profile name is not one, so it reports "no leaks found" over the same commits that carry them. The
working-tree scan below is the one that runs on every commit; the history scan is the one that says
whether the repository is publishable at all. The fix for a red history scan is a one-off rewrite
(`git filter-repo --replace-text`) before the remote is ever public, not an edit at the tip.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Lockfiles pin hashes and PyPI URLs that trip the 12-digit rule with nothing to fix; images are binary.
SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".gif", ".ico", ".pdf", ".woff", ".woff2")
# The placeholder account id that documentation and the example target are allowed to use.
EXAMPLE_ACCOUNT = "123456789012"

# Bounded by non-digits so a 13+ digit run (a nanosecond timestamp, a hash) is not a 12-digit account.
ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN_WITH_ACCOUNT = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:")
USER_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+/")
# A Cognito user pool id: the region, an underscore, nine characters. No example is spelled out here,
# because this file is scanned like every other, and the first thing this test caught was a sample in
# its own comment.
COGNITO_ID = re.compile(r"\b[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{9}\b")
PATTERNS = (ACCOUNT_ID, ARN_WITH_ACCOUNT, USER_PATH, COGNITO_ID)

# Values no regex can recognise -- a profile name and an email address look like ordinary words. They
# are assembled from halves on purpose: this file is scanned too, and spelling them out here would be
# the leak. Adding one costs nothing; leaving one out means nothing catches it.
FORBIDDEN_LITERALS = tuple(
    "".join(parts) for parts in (("okta", "-sso"), ("/Users/", "iracic"), ("iracic82", "@gmail.com"))
)

PLACEHOLDER = "REPLACED_BY_auth_bootstrap"
PUBLIC_URL_PLACEHOLDER = "REPLACED_AFTER_FIRST_DEPLOY"

# A source archive (`git archive`, or GitHub's "Download ZIP") has no git metadata, so there is
# nothing to list and nothing anyone could commit from it. In every checkout, including CI, it runs.
pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _publishable() -> list[str]:
    """Every file `git add -A` would stage: tracked, plus untracked that is not ignored.

    Tracked-only would be a gate with a blind spot exactly where new work lands -- a file is invisible
    to it until the moment it is too late to matter.
    """
    listed = _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    return sorted({path for path in listed if path and not path.endswith(SKIP_SUFFIXES)})


def _is_placeholder(match: str) -> bool:
    return EXAMPLE_ACCOUNT in match or "<your" in match


def _literal_hits(text: str) -> list[str]:
    """Case-insensitively: a profile name shouted in a heading is the same profile name."""
    lowered = text.lower()
    return [literal for literal in FORBIDDEN_LITERALS if literal.lower() in lowered]


def test_no_account_specific_values_are_in_the_working_tree():
    bad = []
    for rel in _publishable():
        path = ROOT / rel
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue  # binary, or a symlink to somewhere that does not exist in this checkout
        for rx in PATTERNS:
            bad += [f"{rel}: {m.group(0)}" for m in rx.finditer(text) if not _is_placeholder(m.group(0))]
        bad += [f"{rel}: forbidden literal {literal}" for literal in _literal_hits(text)]
    assert bad == [], "\n".join(bad)


def test_no_forbidden_literal_is_reachable_in_any_commit():
    """The working-tree scan above cannot see a value that was committed and later removed, and `git
    push` publishes all of it. Scrubbing the tip left 115 commit:file pairs reachable here -- an account
    id, an SSO profile name, a home directory and an email address across 47 of 65 commits -- while both
    gates reported clean: this file reads `git ls-files`, and gitleaks has no rule for any of them.

    Only the literals are scanned. The regex patterns would need a per-path allowlist to be usable over
    history (a lockfile hash is twelve digits), and the values a regex cannot recognise are exactly the
    ones nothing else catches. `-l` so the output names commits and paths and never prints the value
    itself, which would otherwise land in a CI log.

    A red result here is not fixable at the tip. Rewrite history before the remote is public:
    `git filter-repo --replace-text` with one `old==>new` line per literal, from a fresh mirror clone.
    """
    commits = _git("rev-list", "--all").split()
    assert commits, "no commits to scan; this test is meaningless in a repository with no history"
    found = subprocess.run(
        ["git", "grep", "-I", "-i", "-l", "-F", *[arg for lit in FORBIDDEN_LITERALS for arg in ("-e", lit)], *commits],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if found.returncode == 1:  # git grep's "no matches", which is the only outcome that passes
        return
    assert found.returncode == 0, f"git grep failed ({found.returncode}): {found.stderr.strip()[:200]}"
    hits = found.stdout.splitlines()
    shown = "\n".join(hits[:5])
    pytest.fail(f"{len(hits)} commit:file pairs carry a forbidden literal. First few:\n{shown}")


def test_every_pitfall_citation_resolves_to_a_pitfall():
    """`(P52)` in a comment is a promise that PITFALLS.md has a P52 and that it is about this.

    The numbers cannot be checked for *subject*, but they can be checked for existence, and that is the
    failure that actually happens: eight entries were added at once and a dozen comments were written
    against the first number in the batch. A citation that points at a heading which does not exist is
    always wrong; one that resolves is at least reviewable.
    """
    headings = set(re.findall(r"^#+ P(\d+)\.", (ROOT / "PITFALLS.md").read_text(), re.M))
    assert headings, "PITFALLS.md has no '### P<n>.' headings; delete this test with them"
    citation = re.compile(r"\bP(\d{1,3})\b")
    bad = []
    for rel in _publishable():
        if rel == "PITFALLS.md" or not (rel.endswith((".py", ".sh", ".md")) or Path(rel).name == "Makefile"):
            continue
        try:
            text = (ROOT / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        bad += [
            f"{rel}: {m.group(0)} has no entry in PITFALLS.md"
            for m in citation.finditer(text)
            if m.group(1) not in headings
        ]
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


def test_every_gitleaks_allowlisted_path_is_build_output_git_ignores():
    """The allowlist exists because `--no-git` walks the working tree without reading .gitignore, and a
    CDK synth leaves asset hashes that the generic-api-key rule reports. It may only ever name paths
    nobody receives: if a path in it stops being git-ignored, a clone gets a directory gitleaks skips."""
    text = (ROOT / ".gitleaks.toml").read_text()
    assert re.search(r"^\s*regexes\s*=", text, re.M) is None, "allowlists here are paths-only by design"

    blocks = re.findall(r"paths\s*=\s*\[(.*?)\]", text, re.S)
    patterns = [m for block in blocks for m in re.findall(r"'''(.*?)'''", block)]
    assert patterns, ".gitleaks.toml has no path allowlist; delete this test with it"
    for pattern in patterns:
        sample = re.sub(r"[\\^$]", "", pattern).rstrip("/") + "/probe"
        assert subprocess.run(["git", "check-ignore", "-q", sample], cwd=ROOT).returncode == 0, (
            f"{pattern} allowlists {sample}, which git does not ignore"
        )


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
        # Not account-specific, but deployment-specific in exactly the same way: a region left behind
        # from someone else's deploy is silently wrong everywhere else, so it resets with the rest (P62).
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            assert env[var] == PLACEHOLDER, f"{name}.{var}"


# --- the patterns themselves ------------------------------------------------------------------------
# A scanner that matches nothing passes every scan. These samples are assembled rather than written
# out, for the same reason the literals above are: this file is scanned too.

OTHER_ACCOUNT = EXAMPLE_ACCOUNT[:-1] + "3"  # 12 digits, and not the allowed placeholder
SAMPLE_POOL = "us-west-2" + "_" + "Ab1Cd2Ef3"
SAMPLE_HOME = "/Users/" + "somebody/code"


@pytest.mark.parametrize(
    ("pattern", "sample"),
    [
        (ACCOUNT_ID, OTHER_ACCOUNT),
        (ACCOUNT_ID, f"the account is {OTHER_ACCOUNT}."),
        (ARN_WITH_ACCOUNT, f"arn:aws:iam::{OTHER_ACCOUNT}:role/Admin"),
        (ARN_WITH_ACCOUNT, f"arn:aws:bedrock-agentcore:us-west-2:{OTHER_ACCOUNT}:runtime/Ops-abc"),
        (USER_PATH, SAMPLE_HOME),
        (COGNITO_ID, SAMPLE_POOL),
        (COGNITO_ID, f"https://cognito-idp.us-west-2.amazonaws.com/{SAMPLE_POOL}/.well-known/openid-configuration"),
    ],
)
def test_the_patterns_catch_what_they_are_for(pattern, sample):
    assert pattern.search(sample)


@pytest.mark.parametrize(
    ("pattern", "sample"),
    [
        (ACCOUNT_ID, "1234567890123456"),  # a 16-digit run is a card number or a hash, not an account
        (ACCOUNT_ID, "12345678901"),  # eleven digits
        (ACCOUNT_ID, "1.2345678901234"),  # thirteen digits with a dot in front
        (USER_PATH, "/usr/local/bin/"),
        (USER_PATH, "/Users/<name>/"),  # the placeholder form the docs use
        (COGNITO_ID, "us-west-2"),  # a bare region is not a pool id
        (ARN_WITH_ACCOUNT, "arn:aws:iam::aws:policy/ReadOnlyAccess"),  # an AWS-managed policy has no account
    ],
)
def test_the_patterns_leave_lookalikes_alone(pattern, sample):
    assert not pattern.search(sample)


def test_forbidden_literals_are_found_whatever_their_case():
    for literal in FORBIDDEN_LITERALS:
        assert literal in _literal_hits(f"see {literal.upper()} for details")
        assert literal in _literal_hits(literal)
    assert _literal_hits("nothing to see here") == []


def test_the_placeholder_account_is_allowed_and_a_real_one_is_not():
    assert _is_placeholder(EXAMPLE_ACCOUNT)
    assert _is_placeholder("<your-profile>")
    assert not _is_placeholder(OTHER_ACCOUNT)
