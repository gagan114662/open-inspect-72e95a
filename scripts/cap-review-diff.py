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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--max-chars", type=int, default=900_000)
    args = parser.parse_args(argv[1:])
    diff = Path(args.path).read_text(errors="replace")
    body, omitted = cap(diff, args.max_chars)
    sys.stdout.write(body)
    if omitted:
        sys.stdout.write(
            f"\n\nDIFF TRUNCATED: {len(diff):,} characters exceed the {args.max_chars:,} cap; "
            f"{len(omitted)} file(s) omitted — open them in the checkout:\n"
        )
        for path in omitted:
            sys.stdout.write(f"  - {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
