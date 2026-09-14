#!/usr/bin/env python3
"""Parse [P1]/[P2] findings out of a Codex review comment's raw text.

Extracted as its own tested tool because the next piece of this archive
(the workflow that reads a completed Codex review and decides, on its own,
whether the recurrence pattern warrants opening a tracking issue) needs a
reliable, reusable way to turn free-form review prose back into a list of
individual findings -- the same shape docs/self-improvement-archive.jsonl
already stores per round.

Findings in this archive's own convention start a line with a number, a
period, and a **[P1]** or **[P2]** marker, e.g.:
    1. **[P1]** Some critical issue description.
A finding may span multiple lines until the next numbered marker or the end
of the text; this parser keeps only the first line to match how findings
are already recorded in the archive (short, single-line summaries).
"""

from __future__ import annotations

import json
import re
import sys

FINDING_LINE_RE = re.compile(r"^\s*\d+\.\s*(\*\*\[(P1|P2)\]\*\*.*)$")


def parse_findings(text: str) -> list[str]:
    findings = []
    for line in text.splitlines():
        match = FINDING_LINE_RE.match(line)
        if match:
            findings.append(match.group(1).strip())
    return findings


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        with open(argv[1]) as f:
            text = f.read()
    elif len(argv) == 1:
        text = sys.stdin.read()
    else:
        print("usage: parse-review-findings.py [file]  (reads stdin if omitted)", file=sys.stderr)
        return 2

    findings = parse_findings(text)
    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
