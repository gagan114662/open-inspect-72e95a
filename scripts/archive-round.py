#!/usr/bin/env python3
"""Append one review round to the self-improvement archive, idempotently,
and report which finding topics newly cross the mechanism-fix threshold.

Extracted after Codex's own review of the first draft of
.github/workflows/archive-and-recommend.yml found the real gap in that
draft: scripts/analyze-latest-review.py compared "archive on disk" vs.
"archive on disk + this one round" purely in memory, without ever writing
the round back. Since the on-disk archive never grew, two separate PRs that
each contributed one finding on the same topic never combined into the
three occurrences a mechanism-level recommendation requires -- each PR was
compared against the same static baseline in isolation. Persisting the
round is what lets evidence actually accumulate across PRs, which is the
whole point of this being an archive.

Idempotency: each round is tagged with the git SHA of the PR commit the
review ran against (`source_sha`). If an entry with that source_sha already
exists, this script does nothing and reports the round as already
processed -- safe to re-run under retries, reruns, or overlapping workflow
runs without double-counting the same review.

Usage:
    python3 archive-round.py <archive.jsonl> <review-comment.txt> <source-sha> [--threshold N]

Prints a JSON object: {"already_processed": bool, "round": int|null,
"newly_crossed": [...]}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def _load_sibling_module(name: str, filename: str):
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyze_mod = _load_sibling_module("analyze_latest_review", "analyze-latest-review.py")
policy_mod = _load_sibling_module("improvement_policy", "improvement_policy.py")
parse_findings_mod = _load_sibling_module("parse_review_findings", "parse-review-findings.py")
detect_mod = _load_sibling_module("detect_recurring_pattern", "detect-recurring-pattern.py")


def already_processed(archive_entries: list[dict], source_sha: str) -> bool:
    return any(entry.get("source_sha") == source_sha for entry in archive_entries)


def build_round_entry(
    archive_entries: list[dict], findings: list[str], source_sha: str, target: str
) -> dict:
    return {
        "round": analyze_mod.next_round_number(archive_entries),
        "target": target,
        "proposed_by": "codex (automated review, archived by archive-and-recommend.yml)",
        "findings": findings,
        "source_sha": source_sha,
        "kept": None,
        "occurred_at": datetime.now(UTC).isoformat(),
        # Which improvement policy decided this round. revise-improvement-policy.py
        # judges a revision only on rounds stamped with its own hash, so the
        # waiting period counts rounds actually run under it, not rounds that
        # happened while its PR was still open (Codex review of PR #10, round 4).
        "policy_version": policy_mod.POLICY_VERSION,
        "policy_hash": policy_mod.POLICY_HASH,
    }


def append_entry(archive_path: str, entry: dict) -> None:
    with open(archive_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_path")
    parser.add_argument("review_comment_path")
    parser.add_argument("source_sha")
    parser.add_argument(
        "--target",
        default="PR diff (see source_sha)",
        help="Human-readable description of what was reviewed, e.g. 'PR #12 diff'.",
    )
    parser.add_argument("--threshold", type=int, default=None)
    args = parser.parse_args(argv[1:])

    threshold = args.threshold if args.threshold is not None else detect_mod.DEFAULT_THRESHOLD

    archive_entries = analyze_mod.load_archive(args.archive_path)

    if already_processed(archive_entries, args.source_sha):
        print(json.dumps({"already_processed": True, "round": None, "newly_crossed": []}))
        return 0

    with open(args.review_comment_path) as f:
        comment_text = f.read()
    findings = parse_findings_mod.parse_findings(comment_text)

    if not findings:
        print(json.dumps({"already_processed": False, "round": None, "newly_crossed": []}))
        return 0

    newly_crossed = analyze_mod.find_newly_crossed_topics(archive_entries, findings, threshold)
    entry = build_round_entry(archive_entries, findings, args.source_sha, args.target)
    append_entry(args.archive_path, entry)

    print(
        json.dumps(
            {
                "already_processed": False,
                "round": entry["round"],
                "newly_crossed": newly_crossed,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
