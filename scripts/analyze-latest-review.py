#!/usr/bin/env python3
"""Decide, from evidence, whether a just-completed review round newly
crosses the mechanism-fix threshold for any finding topic.

This is the piece that closes the gap named while building this archive:
scripts/detect-recurring-pattern.py could already derive a target-vs-
mechanism recommendation from the archive's accumulated data, but something
still had to run it and decide whether the result was worth acting on --
that was a human/agent judgment call, made by eyeballing the tool's output.

This script makes that specific decision mechanical: it compares the
recommendation with vs. without the latest round's findings included, and
reports only topics whose recommendation *flips* from "target" to
"mechanism" (or newly appears at/above threshold) because of this round
specifically -- not topics that already crossed the threshold in earlier
rounds, which would otherwise fire on every single subsequent round
forever. A workflow can run this automatically after every review and act
(e.g. open a tracking issue) purely on its output, with no one needing to
have read the archive and noticed the pattern themselves.

Usage:
    python3 analyze-latest-review.py <archive.jsonl> <review-comment.txt> [--threshold N]

Exits 0 always (advisory). Prints newline-delimited human-readable lines,
then a `---` separator, then a JSON object: {"newly_crossed": [...]}.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_sibling_module(name: str, filename: str):
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


detect = _load_sibling_module("detect_recurring_pattern", "detect-recurring-pattern.py")
parse_findings_mod = _load_sibling_module("parse_review_findings", "parse-review-findings.py")


def load_archive(path: str) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def next_round_number(entries: list[dict]) -> int:
    rounds = [e.get("round", 0) for e in entries]
    return (max(rounds) + 1) if rounds else 1


def recommendations_by_topic(entries: list[dict], threshold: int) -> dict[str, str]:
    result = detect.analyze(entries, threshold)
    return {rec["topic"]: rec["recommended_action"] for rec in result["recommendations"]}


def find_newly_crossed_topics(
    archive_entries: list[dict], new_findings: list[str], threshold: int
) -> list[dict]:
    """Compare recommendations with vs. without the new round's findings.

    Returns entries for topics that recommend "mechanism" only once the new
    round is included -- i.e. this round is the one that tipped it over,
    not a topic that already exceeded the threshold in prior rounds.
    """
    before = recommendations_by_topic(archive_entries, threshold)

    new_round_entry = {"round": next_round_number(archive_entries), "findings": new_findings}
    after_entries = [*archive_entries, new_round_entry]
    after = recommendations_by_topic(after_entries, threshold)

    newly_crossed = []
    for topic, action in after.items():
        if action != "mechanism":
            continue
        was_mechanism_before = before.get(topic) == "mechanism"
        if not was_mechanism_before:
            newly_crossed.append({"topic": topic, "recommended_action": action})

    return newly_crossed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_path")
    parser.add_argument("review_comment_path")
    parser.add_argument("--threshold", type=int, default=detect.DEFAULT_THRESHOLD)
    args = parser.parse_args(argv[1:])

    archive_entries = load_archive(args.archive_path)

    with open(args.review_comment_path) as f:
        comment_text = f.read()
    new_findings = parse_findings_mod.parse_findings(comment_text)

    if not new_findings:
        print("No findings in the latest review — nothing to analyze.")
        print("---")
        print(json.dumps({"newly_crossed": []}, indent=2))
        return 0

    newly_crossed = find_newly_crossed_topics(archive_entries, new_findings, args.threshold)

    if newly_crossed:
        for item in newly_crossed:
            print(
                f"[{item['topic']}] newly recommends a MECHANISM-LEVEL fix "
                f"as of this round's findings."
            )
    else:
        print("No topic newly crosses the mechanism-fix threshold this round.")

    print("---")
    print(json.dumps({"newly_crossed": newly_crossed}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
