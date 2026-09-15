#!/usr/bin/env python3
"""Decide target-fix vs. mechanism-fix from the improvement archive itself.

Closes the specific gap named while building
docs/self-improvement-archive.jsonl: across rounds 1-5, *I* (the agent)
judged when a finding was serious/recurring enough to warrant revising the
improvement mechanism itself (round 5) rather than just patching the current
target again (rounds 2-4). That judgment call was mine, not something
derived from the archive's own data — which is exactly the gap the paper's
L5 definition ("persistently revises a mechanism that governs subsequent
improvement") requires closing: the recursion has to include *deciding when
to recurse on the mechanism*, not just executing that decision once someone
notices a pattern.

This script makes that decision algorithmically instead: it reads the
archive, buckets every finding into a topic by keyword co-occurrence (no ML,
no external calls -- deliberately simple and auditable), and recommends
"mechanism" once a topic has recurred at or above a threshold across
distinct rounds, "target" otherwise. It is still a human (or an agent
executing on the human's behalf) who reads the recommendation and acts on
it -- this does not make merge/deploy autonomous, and does not claim to.
What it closes is narrower and real: the *decision itself* is now
reproducible from evidence, not from an agent's unrecorded judgment.

Usage:
    python3 detect-recurring-pattern.py <archive.jsonl> [--threshold N]

Prints one recommendation line per topic that has reached the threshold,
plus a machine-readable JSON summary to stdout after a `---` separator.
Exit code 0 always (this is advisory, not a pass/fail gate).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path


def _load_sibling_module(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy_mod = _load_sibling_module("improvement_policy", "improvement_policy.py")

# The taxonomy and threshold are no longer constants of this file: they are
# docs/improvement-policy.json, a versioned document the meta-improver
# (scripts/revise-improvement-policy.py) can revise from evidence and roll
# back. These module-level names are kept so every existing caller and test
# keeps working; they reflect the policy version checked in alongside this
# script (or the built-in v1 fallback when the file is absent).
POLICY = policy_mod.load_policy_or_builtin()
DEFAULT_THRESHOLD: int = POLICY["threshold"]
TOPIC_KEYWORDS: dict[str, list[str]] = policy_mod.topic_keywords(POLICY)
TOPIC_WEIGHTS: dict[str, float] = policy_mod.topic_weights(POLICY)


def classify_finding(text: str, keywords: dict[str, list[str]] | None = None) -> str | None:
    return policy_mod.classify_finding(text, TOPIC_KEYWORDS if keywords is None else keywords)


def load_archive(path: str) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def analyze(
    entries: list[dict],
    threshold: int,
    keywords: dict[str, list[str]] | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    keywords = TOPIC_KEYWORDS if keywords is None else keywords
    weights = TOPIC_WEIGHTS if weights is None else weights
    # Rounds are counted by identity (commit reviewed), the same rule the
    # measurer uses, so the recurrence that opens an issue is the recurrence
    # the policy is judged on (Codex review of PR #10, round 37).
    topic_rounds: dict[str, dict[str, int]] = defaultdict(dict)
    topic_examples: dict[str, list[str]] = defaultdict(list)

    for entry in entries:
        round_num = entry.get("round")
        for finding in entry.get("findings", []):
            topic = classify_finding(finding, keywords)
            if topic is None:
                continue
            topic_rounds[topic][policy_mod.round_key(entry)] = round_num
            if len(topic_examples[topic]) < 3:
                topic_examples[topic].append(f"round {round_num}: {finding[:120]}")

    recommendations = []
    for topic, rounds in sorted(topic_rounds.items(), key=lambda kv: -len(kv[1])):
        recurrence = len(rounds)
        # A topic's weight is the policy's learned credit for it: evidence the
        # field never corroborates gets discounted (see revise-improvement-policy.py).
        weighted = recurrence * weights.get(topic, 1.0)
        action = "mechanism" if weighted >= threshold else "target"
        recommendations.append(
            {
                "topic": topic,
                "recurrence_count": recurrence,
                "weighted_recurrence": round(weighted, 3),
                "rounds": sorted(rounds.values(), key=lambda r: (r is None, r)),
                "recommended_action": action,
                "examples": topic_examples[topic],
            }
        )

    return {"threshold": threshold, "recommendations": recommendations}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_path")
    parser.add_argument("--threshold", type=int, default=None)
    parser.add_argument(
        "--policy",
        default=None,
        help="Path to an improvement-policy JSON; defaults to docs/improvement-policy.json",
    )
    args = parser.parse_args(argv[1:])

    policy = policy_mod.load_policy(args.policy) if args.policy else POLICY
    threshold = args.threshold if args.threshold is not None else policy["threshold"]
    entries = load_archive(args.archive_path)
    result = analyze(
        entries,
        threshold,
        policy_mod.topic_keywords(policy),
        policy_mod.topic_weights(policy),
    )
    result["policy_version"] = policy["version"]
    result["policy_hash"] = policy_mod.policy_hash(policy)

    for rec in result["recommendations"]:
        marker = (
            "MECHANISM-LEVEL FIX RECOMMENDED"
            if rec["recommended_action"] == "mechanism"
            else "target-level fix sufficient so far"
        )
        print(
            f"[{rec['topic']}] recurred in {rec['recurrence_count']} round(s) "
            f"{rec['rounds']} -> {marker}"
        )
        for example in rec["examples"]:
            print(f"    - {example}")

    print("---")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
