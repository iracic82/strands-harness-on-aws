"""The gate behind `scripts/verify_docs.py`: what the documentation claims, the repo can back up.

Two rules are checked here rather than in the script, because they have to hold even where node is not
installed and nothing is executed. First, every ```bash block that is not tagged `title="manual"` is a
block this repo runs in CI with no AWS account at all, so naming a Makefile target or an operator script
that reaches AWS in an untagged block is a broken promise -- and the list of those targets and scripts is
read out of the Makefile and `scripts/`, not typed out here, so one added next month is covered by
default. Second, every image a document links to exists; today the answer is zero images, and it stays
correct the moment the screenshots land.

The rest exercises `verify_docs.py` itself offline: that a manual block is not executed, that a runnable
block cannot write into the checkout it was run from, and that a failing block fails the gate. The one
thing not tested here is a real Mermaid render -- that needs node and a headless Chromium, so the script's
argv and its no-npx path are tested instead and the rendering itself is what the CI `docs` job is for.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts import verify_docs

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The Makefile targets that need no AWS account, no network and no Docker: CI's `test` job is these
# three and nothing else. Every other target either drives the agentcore CLI or calls an AWS API.
OFFLINE_TARGETS = {"test", "lint", "hygiene"}
# The two scripts that only read the repository. The others all take a region, an ARN or a token.
OFFLINE_SCRIPTS = {"check_links.py", "verify_docs.py"}
# Values that belong to whoever is running the command, never to a reader of the docs.
ACCOUNT_SPECIFIC = ("arn:aws", "AWS_PROFILE", "DEMO_PASSWORD", "@aws/agentcore", "agentcore deploy")
# `aws <service> <operation>`: the CLI, as opposed to the prose word "aws" or the `aws_call` tool.
AWS_CLI = re.compile(r"^\s*aws\s+[a-z0-9-]+\s", re.M)

IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def cloud_tokens() -> set[str]:
    """Every way the docs have of spelling "this needs your account", derived from the repo itself."""
    lines = [line for line in MAKEFILE.read_text().splitlines() if line and not line[0].isspace()]
    phony = set(next(line for line in lines if line.startswith(".PHONY:")).removeprefix(".PHONY:").split())
    targets = {f"make {name}" for name in phony - OFFLINE_TARGETS}
    scripts = {
        f"scripts/{path.name}"
        for path in (ROOT / "scripts").iterdir()
        if path.is_file() and path.name not in OFFLINE_SCRIPTS
    }
    return targets | scripts | {"doctor.py", *ACCOUNT_SPECIFIC}


def in_scope() -> list[Path]:
    return [path for path in verify_docs.documents() if path not in verify_docs.SKIPPED]


def untagged_bash() -> list[verify_docs.Block]:
    return [
        block
        for path in in_scope()
        for block in verify_docs.blocks(path)
        if block.lang == "bash" and not verify_docs.is_manual(block)
    ]


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sample.md"
    path.write_text(body, encoding="utf-8")
    return path


# --- the rules the documentation has to follow -------------------------------------------------------


def test_the_makefile_and_scripts_still_spell_the_tokens_this_test_looks_for():
    """A token list derived from a file is only a gate while the file still parses the way we think.
    If `.PHONY` or scripts/ is restructured, the derivation silently returns a smaller set and the test
    below starts passing for the wrong reason."""
    tokens = cloud_tokens()
    assert {"make deploy", "make doctor", "make invoke", "scripts/smoke.sh", "scripts/auth_bootstrap.py"} <= tokens
    assert "make test" not in tokens and "scripts/verify_docs.py" not in tokens


def test_no_untagged_bash_block_needs_the_owners_account():
    """An untagged block runs in CI, where there is no profile, no token and no deployed runtime."""
    tokens = cloud_tokens()
    for block in untagged_bash():
        named = sorted(token for token in tokens if token in block.body)
        assert not named, f'{block.where} names {named}; tag it ```bash title="manual"'
        assert not AWS_CLI.search(block.body), f'{block.where} calls the AWS CLI; tag it ```bash title="manual"'


def test_every_image_reference_exists():
    """There are no images yet -- the screenshots land in a later pass -- and this is what stops the
    first `![...](docs/img/...)` from being committed before the file it points at."""
    for path in in_scope():
        for target in IMAGE.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://")):
                continue
            assert (path.parent / target).exists(), f"{verify_docs.rel(path)} references missing image {target}"


def test_the_vendored_cli_reference_is_the_only_document_out_of_scope():
    """Skipping a file is how a broken promise hides, so there is exactly one and it is upstream's."""
    assert set(verify_docs.SKIPPED) == {ROOT / "docs" / "agentcore-cli-AGENTS.md"}
    assert set(verify_docs.SKIPPED) < set(verify_docs.documents())


def test_the_docs_gate_runs_in_make_lint_and_in_ci():
    """Step 3 of the task: a checker nobody runs is documentation about documentation."""
    recipes = [line.strip() for line in MAKEFILE.read_text().splitlines() if line.startswith("\t")]
    assert "uv run python scripts/verify_docs.py" in recipes
    workflow = WORKFLOW.read_text()
    assert "uv run python scripts/verify_docs.py" in workflow
    # The quickstart block is manual because this job already proves it; if it goes, so does the reason.
    assert "fresh-clone:" in workflow


# --- verify_docs.py itself ---------------------------------------------------------------------------


def test_a_manual_block_is_described_but_never_executed(tmp_path, capsys):
    marker = tmp_path / "ran"
    path = write(tmp_path, f'# x\n\n```bash title="manual"\ntouch {marker}\n```\n')
    assert verify_docs.main(["--only-bash", str(path)]) == 0
    assert not marker.exists()
    assert "[manual]" in capsys.readouterr().out


def test_a_runnable_block_runs_in_a_copy_and_cannot_touch_the_checkout(tmp_path):
    """The reason the snapshot exists: a documented command may write files, and the checkout you ran
    `make lint` from is not a scratch directory."""
    path = write(tmp_path, "# x\n\n```bash\necho hi > verify-docs-should-not-exist.txt\n```\n")
    assert verify_docs.main(["--only-bash", str(path)]) == 0
    assert not (ROOT / "verify-docs-should-not-exist.txt").exists()


def test_the_copy_is_the_working_tree_and_carries_the_repo(tmp_path):
    """Committed or merely staged, the files a reader would clone are the files a block runs against."""
    path = write(tmp_path, "# x\n\n```bash\ntest -f agentcore/agentcore.json && test -f README.md\n```\n")
    assert verify_docs.main(["--only-bash", str(path)]) == 0


def test_a_failing_block_fails_the_gate(tmp_path, capsys):
    path = write(tmp_path, "# x\n\n```bash\necho before\nfalse\necho after\n```\n")
    assert verify_docs.main(["--only-bash", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "before" in out  # the excerpt is what tells you which line gave up


def test_a_block_that_leans_on_aws_credentials_does_not_see_them(monkeypatch):
    """`-euo pipefail` plus an environment with no AWS_* is what makes "runs offline" mean something."""
    monkeypatch.setenv("AWS_PROFILE", "whatever")
    assert "AWS_PROFILE" not in verify_docs.OFFLINE_ENV


def test_diagrams_are_skipped_rather_than_passed_when_npx_is_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(verify_docs.shutil, "which", lambda name: None)
    path = write(tmp_path, "# x\n\n```mermaid\nflowchart LR\n  a[A] --> b[B]\n```\n")
    assert verify_docs.main(["--only-mermaid", str(path)]) == 0
    assert "no npx" in capsys.readouterr().out


def test_the_renderer_is_pinned_and_runs_chromium_without_its_sandbox(tmp_path):
    """Unpinned, somebody else's release breaks this repo's gate; sandboxed, Chromium cannot start on
    a GitHub runner and every diagram in the repo reads as broken at once."""
    command = verify_docs.mermaid_command(tmp_path / "in.mmd", tmp_path / "p.json", tmp_path / "out.svg")
    assert command[:3] == ["npx", "-y", verify_docs.MERMAID_CLI]
    assert re.fullmatch(r"@mermaid-js/mermaid-cli@\d+\.\d+\.\d+", verify_docs.MERMAID_CLI)
    assert "--no-sandbox" in verify_docs.PUPPETEER_CONFIG


def test_a_fence_inside_a_block_does_not_end_it(tmp_path):
    """The docs quote fenced markdown in places; a greedy or unanchored fence regex would split a block
    in half and run its second half as a command."""
    path = write(tmp_path, '# x\n\n```bash title="manual"\necho "    ```bash"\necho done\n```\n')
    found = verify_docs.blocks(path)
    assert [block.lang for block in found] == ["bash"]
    assert found[0].body.count("echo") == 2
