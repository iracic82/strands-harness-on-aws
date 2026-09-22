#!/usr/bin/env python3
"""Check that every relative Markdown link, image, and heading anchor in this repo resolves.

Usage:
    uv run python scripts/check_links.py [path ...]     # defaults to *.md at the root plus docs/

External links (http, https, mailto) are not fetched: this is a structural check, not a crawler. A link
into another file's heading (`docs/x.md#a-section`) is checked both ways -- the file has to exist and the
heading has to be in it -- using GitHub's slug rule, so a renamed heading fails here rather than silently
scrolling nowhere.

Exit code 0 when everything resolves, 1 otherwise; every failure prints `file:line  link  reason`.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", "cdk.out", ".superpowers"}
EXTERNAL = ("http://", "https://", "mailto:", "tel:")

# [text](target) and ![alt](target), skipping the reference-style and autolink forms we do not use.
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Fenced code blocks: a path inside one is an example, not a link.
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
NOT_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)


def slug(heading: str) -> str:
    """GitHub's heading anchor rule: strip formatting, lowercase, drop punctuation, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # a link in a heading contributes its text
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return NOT_SLUG.sub("", text).strip().lower().replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every heading anchor in a Markdown file, with GitHub's `-1` suffix for repeats."""
    found: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        candidate, n = base, 1
        while candidate in found:
            candidate, n = f"{base}-{n}", n + 1
        found.add(candidate)
    return found


def links(path: Path) -> list[tuple[int, str]]:
    """Every inline link target in a Markdown file, with its 1-based line number, ignoring code fences."""
    out: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        out.extend((number, target) for target in LINK.findall(line))
    return out


def rel(path: Path) -> Path:
    """Display path, relative to the repo root when it is inside it. A caller may point us anywhere."""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def markdown_files(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
            continue
        for candidate in sorted(path.rglob("*.md")):
            if not SKIP_DIRS.intersection(rel(candidate).parts):
                found.append(candidate)
    return found


def check(path: Path, cache: dict[Path, set[str]]) -> list[str]:
    problems: list[str] = []
    for number, target in links(path):
        if target.startswith(EXTERNAL):
            continue
        where = f"{rel(path)}:{number}"
        file_part, _, anchor = target.partition("#")
        resolved = path if not file_part else (path.parent / file_part).resolve()
        if file_part and not resolved.exists():
            problems.append(f"{where}  {target}  missing file")
            continue
        if not anchor or resolved.suffix != ".md":
            continue
        if resolved not in cache:
            cache[resolved] = anchors(resolved)
        if anchor.lower() not in cache[resolved]:
            problems.append(f"{where}  {target}  no such heading")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories to check")
    args = parser.parse_args(argv)
    targets = args.paths or [*sorted(ROOT.glob("*.md")), ROOT / "docs"]

    cache: dict[Path, set[str]] = {}
    files = markdown_files([p if p.is_absolute() else (ROOT / p) for p in targets])
    problems = [problem for path in files for problem in check(path, cache)]
    checked = sum(len(links(path)) for path in files)
    print(f"checked {checked} links in {len(files)} files")
    for problem in problems:
        print(problem)
    print("OK" if not problems else f"{len(problems)} broken")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
