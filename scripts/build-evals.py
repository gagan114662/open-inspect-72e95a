#!/usr/bin/env python3
"""Turn archived review rounds into an eval set.

Every archived round is a real change plus an independent verdict on it:
the commit Codex reviewed and the findings it reported. That is an eval
case: given this diff, does a reviewer (or a pre-push check, or a future
version of the agent with new skills) surface the same classes of problem?

Each case under evals/cases/ holds the diff of the reviewed commit (capped,
credentials scrubbed), the findings as expected outcomes, and the topic the
current policy files each finding under. Cases whose commit is no longer
reachable are skipped and counted.

usage: build-evals.py [ARCHIVE] [--policy PATH] [--out-dir DIR] [--max-diff-chars N]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "evals" / "cases"
DEFAULT_MAX_DIFF_CHARS = 60_000


def _load_sibling_module(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy_mod = _load_sibling_module("improvement_policy", "improvement_policy.py")


def commit_diff(sha: str, repo: Path = REPO_ROOT) -> str | None:
    """The reviewed commit's diff, or None when the commit is unreachable."""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True
    ).returncode:
        return None
    result = subprocess.run(
        ["git", "diff", f"{sha}~1", sha], cwd=repo, capture_output=True, text=True
    )
    if result.returncode:
        result = subprocess.run(
            ["git", "show", "--format=", sha], cwd=repo, capture_output=True, text=True
        )
    return result.stdout if result.returncode == 0 else None


def build_case(entry: dict, diff: str, keywords: dict[str, list[str]], max_diff_chars: int) -> dict:
    findings = [f for f in entry.get("findings", []) or [] if isinstance(f, str)]
    expected = [
        {
            "finding": policy_mod.scrub_secrets(f).strip(),
            "topic": policy_mod.classify_finding(f, keywords),
        }
        for f in findings
    ]
    scrubbed = policy_mod.scrub_secrets(diff)
    truncated = len(scrubbed) > max_diff_chars
    return {
        "id": f"round-{int(entry['round']):03d}",
        "round": entry["round"],
        "source_sha": entry.get("source_sha"),
        "target": entry.get("target"),
        "occurred_at": entry.get("occurred_at"),
        "expected": expected,
        "expected_topics": sorted({e["topic"] for e in expected if e["topic"]}),
        "diff": scrubbed[:max_diff_chars],
        "diff_truncated": truncated,
        "diff_chars": len(scrubbed),
    }


def build(
    entries: list[dict], keywords: dict[str, list[str]], diff_for, max_diff_chars: int
) -> tuple[list[dict], int]:
    cases: list[dict] = []
    skipped = 0
    for entry in entries:
        sha = entry.get("source_sha")
        if not sha or not isinstance(entry.get("round"), int):
            skipped += 1
            continue
        diff = diff_for(sha)
        if diff is None:
            skipped += 1
            continue
        cases.append(build_case(entry, diff, keywords, max_diff_chars))
    return cases, skipped


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "archive_path",
        nargs="?",
        default=str(REPO_ROOT / "docs" / "self-improvement-archive.jsonl"),
    )
    parser.add_argument("--policy", default=None)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--max-diff-chars", type=int, default=DEFAULT_MAX_DIFF_CHARS)
    args = parser.parse_args(argv[1:])
    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    entries = [
        json.loads(line)
        for line in Path(args.archive_path).read_text().splitlines()
        if line.strip()
    ]
    cases, skipped = build(
        entries, policy_mod.topic_keywords(policy), commit_diff, args.max_diff_chars
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        target = out_dir / f"{case['id']}.json"
        policy_mod.assert_safe_output(target, inputs=[args.archive_path, args.policy])
        target.write_text(json.dumps(case, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cases": len(cases),
                "skipped": skipped,
                "policy_version": policy["version"],
                "out_dir": policy_mod.relative_to_repo(out_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
