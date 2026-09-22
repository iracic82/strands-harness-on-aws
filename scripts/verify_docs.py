#!/usr/bin/env python3
"""Check that everything this repo's documentation says actually works.

Usage:
    uv run python scripts/verify_docs.py [--only-bash | --only-mermaid] [path ...]

Scope is `README.md`, `PITFALLS.md` and `docs/*.md`, minus `docs/agentcore-cli-AGENTS.md`: that one is
the vendored `@aws/agentcore` CLI reference, its bash blocks are the CLI's own examples against the
CLI's own placeholder project, and editing them would make our copy diverge from upstream.

Fences are read the way CommonMark and GitHub read them -- ``` or ~~~, three or more of them, up to
three spaces of indentation, closing fence of the same character and at least the same length -- because
a block this script cannot see is a block it reports nothing about, and silence is the one failure this
gate must not have. `tests/test_docs_commands.py` counts raw fence lines against the blocks found here,
so a style that slips past (a fence quoted into a blockquote, say) fails a test instead of shipping.

Every ```bash block is a promise, and there are exactly two kinds. Either it runs here -- offline, with
no AWS credentials, no Docker, no interactive input and no long-running server -- or it says so, with
```bash title="manual". A manual block is executed by hand during a real deployment and its transcript
is what proves it; an untagged block is executed verbatim by this script on every `make lint` and in CI.
`PITFALLS.md` is manual by file rather than by block: the fenced blocks there quote a symptom -- an
environment assignment and the arrow to what botocore does with it, an ImportError -- and tagging them
`manual` would claim they are commands somebody types.

Runnable blocks never run in the checkout. They run in a throwaway copy of it (see `snapshot`), because
a documented command is allowed to write files, and `git checkout <path>` in your working tree would
throw away an edit you had not committed yet.

The README quickstart -- `git clone`, `uv sync`, `export AWS_PROFILE=<your-profile>` -- stays manual on
purpose: it names a profile only you can fill in, and CI's `fresh-clone` job already proves that a clone
of this commit installs from the lockfile and passes the suite. Duplicating it here would run the same
minutes twice and check less.

```mermaid blocks are parsed by rendering them with `@mermaid-js/mermaid-cli`, which is the renderer
GitHub uses, so a diagram that passes here is a diagram that draws on the repository page. That needs
node, so without `npx` the diagrams are reported as skipped rather than silently passing -- CI has node,
and that is where the rule is enforced.

Exit code 0 when nothing failed, 1 otherwise. One line per block, `[ok]`, `[FAIL]`, `[manual]` or
`[skip]`, and a failure prints the part of the output that explains it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]

# Not ours to fix: a copy of upstream's AGENTS.md, kept verbatim so it can be diffed against a new one.
SKIPPED = {ROOT / "docs" / "agentcore-cli-AGENTS.md": "vendored @aws/agentcore CLI reference"}
# Manual by file. There are no bash blocks in PITFALLS.md today; this is what happens to the next one.
MANUAL_FILES = {ROOT / "PITFALLS.md": "symptoms and fragments, not commands"}

# A fence the way CommonMark, and so GitHub, reads one: up to three spaces of indentation, then three
# or more backticks or tildes. Four spaces would be an indented code block instead, and the three-space
# allowance is what makes a fence under a list item a fence. The gate has to see exactly what a reader
# sees rendered -- a block the parser cannot see ships with no [ok], no [FAIL] and no [manual] line at
# all, which is the one failure mode this whole script exists to prevent.
FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
MANUAL = 'title="manual"'

# Pinned like the Makefile pins the agentcore CLI: an unpinned renderer turns a green gate red on
# somebody else's release day, and a diagram that parsed yesterday has not changed.
MERMAID_CLI = "@mermaid-js/mermaid-cli@11.17.0"
# Chromium's sandbox needs unprivileged user namespaces, which GitHub's Ubuntu runners restrict. Without
# this the browser dies before it parses anything and every diagram in the repo reads as broken.
PUPPETEER_CONFIG = '{"args": ["--no-sandbox", "--disable-setuid-sandbox"]}'

BASH_TIMEOUT = 600
MERMAID_TIMEOUT = 300

# A runnable block has to work in CI, where there are no credentials at all. Dropping AWS_* here means a
# block that quietly leans on the owner's profile fails on the owner's laptop too, not two pushes later.
OFFLINE_ENV = {name: value for name, value in os.environ.items() if not name.startswith("AWS_")}


class Block(NamedTuple):
    """One fenced block: where it is, what it claims to be, and what is in it."""

    path: Path
    line: int
    lang: str
    attrs: str
    body: str

    @property
    def where(self) -> str:
        return f"{rel(self.path)}:{self.line}"

    @property
    def summary(self) -> str:
        """The first non-blank line, which is what the block is recognisable by in a report."""
        first = next((line for line in self.body.splitlines() if line.strip()), "")
        return first if len(first) <= 70 else f"{first[:69]}…"


def rel(path: Path) -> Path:
    """Display path, relative to the repo root when it is inside it. A caller may point us anywhere."""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def documents(paths: list[Path] | None = None) -> list[Path]:
    """The Markdown in scope: the two root documents plus docs/, in reading order."""
    if paths:
        return [path if path.is_absolute() else (ROOT / path) for path in paths]
    return [ROOT / "README.md", ROOT / "PITFALLS.md", *sorted((ROOT / "docs").glob("*.md"))]


def dedent(line: str, indent: int) -> str:
    """Drop as much of the opening fence's indentation as this line actually has: CommonMark's rule, and
    the reason a fence under a list item yields the command rather than the command behind two spaces."""
    return line[min(indent, len(line) - len(line.lstrip(" "))) :]


def closes(line: str, opened: str) -> bool:
    """A fence closes a block only with its own character and at least as many of them, so a ``` inside
    a ````-fenced block is content and not the end of it."""
    match = FENCE_CLOSE.match(line)
    return bool(match) and match["fence"][0] == opened[0] and len(match["fence"]) >= len(opened)


def blocks(path: Path) -> list[Block]:
    """Every fenced block in a document, in order, scanned line by line rather than with one regex.

    The regex version could only see a fence at column 0, spelled with backticks. An indented one under
    a list item, a `~~~bash` one, or a closing fence longer than its opening -- all of which GitHub
    renders as ordinary code blocks -- were invisible, and an invisible bash block is an ungated one.
    Blockquoted fences (`> ```bash`) are still not supported; `tests/test_docs_commands.py` counts raw
    fence lines against the blocks found here, so one of those fails loudly instead of slipping past.
    """
    found: list[Block] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        opening = FENCE_OPEN.match(lines[index])
        # A backtick fence's info string may not contain a backtick; that rule is what keeps inline code
        # in a paragraph (`a` `b` `c`) from being read as the start of a block.
        if not opening or (opening["fence"][0] == "`" and "`" in opening["info"]):
            index += 1
            continue
        start, indent, fence = index, len(opening["indent"]), opening["fence"]
        lang, _, attrs = opening["info"].strip().partition(" ")
        index += 1
        body: list[str] = []
        while index < len(lines) and not closes(lines[index], fence):
            body.append(dedent(lines[index], indent))
            index += 1
        index += 1  # the closing fence, or one past the end of an unclosed block
        found.append(Block(path, start + 1, lang, attrs.strip(), "".join(f"{line}\n" for line in body)))
    return found


def is_manual(block: Block) -> bool:
    return MANUAL in block.attrs or block.path in MANUAL_FILES


def snapshot(tmp: Path) -> Path:
    """A throwaway copy of everything `git add -A` would stage, as a repository with one commit.

    The working tree and not `HEAD`, so `make lint` checks the documentation you are about to commit
    rather than the documentation you committed last time. A repository and not a plain directory,
    because a documented command may well be a git command -- `git checkout agentcore/agentcore.json`
    is one of them -- and it has to resolve against something.
    """
    work = tmp / "checkout"
    work.mkdir()
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for name in filter(None, listed.split("\0")):
        source = ROOT / name
        if not source.is_file():  # staged-but-deleted, or a submodule
            continue
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, work / name)
    git = ["git", "-c", "user.email=docs@example.invalid", "-c", "user.name=verify_docs"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "snapshot"]):
        subprocess.run([*git, *args], cwd=work, capture_output=True, text=True, check=True)
    return work


def prepare(work: Path, runnable: list[Block]) -> subprocess.CompletedProcess | None:
    """Install the copy's dependencies, but only when a block is going to need them.

    `--frozen` for the same reason CI uses it: a block that only passes because the resolver picked a
    newer library than the lockfile pins is a block that does not pass for a reader.
    """
    if not any(re.search(r"\b(uv|make)\b", block.body) for block in runnable):
        return None
    return subprocess.run(["uv", "sync", "--frozen"], cwd=work, capture_output=True, text=True, timeout=BASH_TIMEOUT)


def run_bash(block: Block, work: Path) -> subprocess.CompletedProcess:
    """`-euo pipefail`: a block that prints an error and carries on is a block that lies about working."""
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block.body],
        cwd=work,
        env=OFFLINE_ENV,
        capture_output=True,
        text=True,
        timeout=BASH_TIMEOUT,
    )


def mermaid_command(source: Path, config: Path, target: Path) -> list[str]:
    return ["npx", "-y", MERMAID_CLI, "-p", str(config), "-i", str(source), "-o", str(target)]


def render_mermaid(block: Block, tmp: Path, index: int) -> subprocess.CompletedProcess:
    """Render one diagram to SVG in a temp dir. mmdc reports a parse error by failing, which is the point."""
    config = tmp / "puppeteer.json"
    if not config.exists():
        config.write_text(PUPPETEER_CONFIG, encoding="utf-8")
    source = tmp / f"diagram-{index}.mmd"
    source.write_text(block.body, encoding="utf-8")
    return subprocess.run(
        mermaid_command(source, config, tmp / f"diagram-{index}.svg"),
        capture_output=True,
        text=True,
        timeout=MERMAID_TIMEOUT,
    )


def report(status: str, where: object, note: str) -> None:
    print(f"[{status}]".ljust(9) + f"{where}  {note}")


def excerpt(result: subprocess.CompletedProcess) -> str:
    """The end of stdout and the *start* of stderr: what a reader needs is the last thing that worked
    and the first thing that did not. mmdc prints `Parse error on line N` and then ten frames of
    puppeteer stack, and `bash -e` stops at the first failure, so trimming stderr from the end would
    throw away the only line worth reading in both cases."""
    return "\n".join(part.strip() for part in (result.stdout[-800:], result.stderr[:800]) if part.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="Markdown files to check; defaults to the repo's own")
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--only-bash", action="store_true", help="skip the diagrams (needs no node)")
    only.add_argument("--only-mermaid", action="store_true", help="skip the commands")
    args = parser.parse_args(argv)

    wanted = {"bash"} if args.only_bash else {"mermaid"} if args.only_mermaid else {"bash", "mermaid"}
    found: list[Block] = []
    for path in documents(args.paths):
        if path in SKIPPED:
            report("skip", rel(path), SKIPPED[path])
            continue
        found.extend(block for block in blocks(path) if block.lang in wanted)

    runnable = [block for block in found if block.lang == "bash" and not is_manual(block)]
    diagrams = [block for block in found if block.lang == "mermaid"]
    have_npx = bool(shutil.which("npx"))
    counts = {"ok": 0, "manual": 0, "skip": 0, "FAIL": 0}

    with tempfile.TemporaryDirectory(prefix="verify-docs-") as name:
        tmp = Path(name)
        work = snapshot(tmp) if runnable else None
        if work is not None:
            synced = prepare(work, runnable)
            if synced is not None and synced.returncode:
                report("FAIL", rel(ROOT / "uv.lock"), "uv sync --frozen in the copy of the checkout")
                print(excerpt(synced))
                counts["FAIL"] += 1
                runnable = []

        for block in found:
            note = f"mermaid: {block.summary}" if block.lang == "mermaid" else block.summary
            if block.lang == "mermaid" and not have_npx:
                counts["skip"] += 1
                report("skip", block.where, "mermaid not validated (no npx)")
                continue
            if block.lang == "bash" and is_manual(block):
                counts["manual"] += 1
                report("manual", block.where, note)
                continue
            if block.lang == "bash" and block not in runnable:  # the copy has no environment to run in
                counts["skip"] += 1
                report("skip", block.where, note)
                continue
            result = (
                render_mermaid(block, tmp, diagrams.index(block)) if block.lang == "mermaid" else run_bash(block, work)
            )
            status = "ok" if result.returncode == 0 else "FAIL"
            counts[status] += 1
            report(status, block.where, note)
            if status == "FAIL":
                print(excerpt(result))

    print(f"{counts['ok']} ok, {counts['manual']} manual, {counts['skip']} skipped")
    print("OK" if not counts["FAIL"] else f"{counts['FAIL']} failed")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
