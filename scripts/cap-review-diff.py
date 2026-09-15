#!/usr/bin/env python3
"""Print a unified diff capped at whole-file boundaries.

The Codex review prompt has a hard input limit (1,048,576 characters). This
prints the diff read from PATH, keeping complete per-file hunks in order until
`--max-chars` would be exceeded, then a note listing every omitted file, so a
truncated prompt states what it does not show instead of ending mid-hunk.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_FILE_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$", re.MULTILINE)


def split_files(diff: str) -> list[tuple[str, str]]:
    """(path, text) per file section, in order. Text before the first header
    (normally nothing) is kept under an empty path."""
    starts = [m.start() for m in _FILE_HEADER.finditer(diff)]
    if not starts:
        return [("", diff)] if diff else []
    sections: list[tuple[str, str]] = []
    if starts[0] > 0:
        sections.append(("", diff[: starts[0]]))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(diff)
        text = diff[start:end]
        header = _FILE_HEADER.match(text)
        sections.append((header.group(2) if header else "", text))
    return sections


def cap(diff: str, max_chars: int) -> tuple[str, list[str]]:
    """The diff cut at a whole-file boundary under `max_chars`, plus the
    omitted paths. A single file larger than the cap is omitted too, never
    cut in half."""
    kept: list[str] = []
    omitted: list[str] = []
    used = 0
    for path, text in split_files(diff):
        if omitted or used + len(text) > max_chars:
            omitted.append(path or "(preamble)")
            continue
        kept.append(text)
        used += len(text)
    return "".join(kept), omitted


def _notice(total: int, max_chars: int, omitted: int) -> str:
    return (
        f"\n\nDIFF TRUNCATED: {total:,} characters exceed the {max_chars:,} cap; "
        f"{omitted} file(s) omitted — open them in the checkout:\n"
    )


def render(diff: str, max_chars: int) -> str:
    """The complete prompt fragment, body AND notice, within `max_chars`:
    the omitted-file list is itself budgeted, and what does not fit is
    counted instead of listed (Codex review of PR #72, round 4)."""
    body, omitted = cap(diff, max_chars)
    if not omitted:
        return body
    more = "  … and {:,} more (run `git diff --stat` for the full list)\n"
    reserve = len(_notice(len(diff), max_chars, len(omitted))) + len(more.format(len(omitted)))
    if len(body) + reserve > max_chars:
        # Leave room for the notice itself, then re-cap the body.
        body, omitted = cap(diff, max(0, max_chars - reserve))
    header = _notice(len(diff), max_chars, len(omitted))
    budget = max_chars - len(body) - len(header) - len(more.format(len(omitted)))
    listed: list[str] = []
    used = 0
    for path in omitted:
        line = f"  - {path}\n"
        if used + len(line) > budget:
            break
        listed.append(line)
        used += len(line)
    rest = len(omitted) - len(listed)
    out = body + header + "".join(listed) + (more.format(rest) if rest else "")
    # A cap smaller than the notice itself gets the notice, cut: still honest.
    return out[:max_chars]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--max-chars", type=int, default=900_000)
    args = parser.parse_args(argv[1:])
    diff = Path(args.path).read_text(errors="replace")
    sys.stdout.write(render(diff, args.max_chars))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
