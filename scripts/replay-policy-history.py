#!/usr/bin/env python3
"""Replay the archive against every policy version, out of sample.

The test of a self-improving improver is not "did coverage go up on the
findings it was tuned on" but "did each new policy version predict trouble
better than its predecessor on rounds none of them had seen yet". For each
version in the policy history this replays the archive in time order and
measures the version only on rounds archived AFTER it was created:

  coverage_oos   share of those later findings the version classifies
  validity_oos   Spearman correlation of the version's per-topic review
                 signal over those rounds with the field anchor
  rounds_oos     how many later rounds that is

A rising line across versions is recursive self-improvement with held-out
evidence; a falling one is the case for a rollback. Both are worth seeing.

usage: replay-policy-history.py [ARCHIVE] [--policy PATH] [--history PATH]
                                [--trace-evidence PATH] [--out-json PATH]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
measure_mod = _load_sibling_module("measure_policy_validity", "measure-policy-validity.py")


def versions_in_order(policy: dict, history: list[dict]) -> list[dict]:
    """Every policy version ever in force, oldest first: v1 (the builtin or
    the recorded root), then each history snapshot; the current policy if
    the history does not already end with it."""
    root = next((e["policy"] for e in history if e.get("version") == 1 and e.get("policy")), None)
    out = [root or policy_mod.builtin_policy()]
    for entry in history:
        snap = entry.get("policy")
        if snap and snap.get("version") != 1:
            out.append(snap)
    if not any(
        policy_mod.policy_hash(v) == policy_mod.policy_hash(policy)
        and v.get("version") == policy.get("version")
        for v in out
    ):
        out.append(policy)
    return out


def rounds_after(entries: list[dict], created_at: str | None) -> list[dict]:
    """Archive entries whose round was recorded after `created_at`; every
    entry when the version predates the archive or is undated."""
    cutoff = measure_mod.parse_timestamp_ms(created_at) if created_at else None
    if cutoff is None:
        return list(entries)
    later = {
        r["key"]
        for r in measure_mod.rounds_in_order(entries)
        if r["timestamp_ms"] is not None and r["timestamp_ms"] > cutoff
    }
    return [e for e in entries if policy_mod.round_key(e) in later]


def replay(entries: list[dict], policy: dict, history: list[dict], evidence: dict | None) -> dict:
    rows = []
    for version in versions_in_order(policy, history):
        # The root version predates the archive by definition: every round
        # was archived under it or its descendants.
        is_root = version.get("origin") == "init" or version.get("parent") is None
        later = rounds_after(entries, None if is_root else version.get("created_at"))
        if later:
            current = measure_mod.measure(later, version, evidence)["current"]
            coverage, validity = current.get("coverage"), current.get("validity")
            findings = current.get("findings_total")
        else:
            coverage = validity = findings = None
        in_sample = measure_mod.measure(entries, version, evidence)["current"]
        rows.append(
            {
                "version": version.get("version"),
                "origin": version.get("origin"),
                "created_at": version.get("created_at"),
                "policy_hash": policy_mod.policy_hash(version),
                "rounds_oos": len({policy_mod.round_key(e) for e in later}),
                "findings_oos": findings,
                "coverage_oos": coverage,
                "validity_oos": validity,
                "coverage_all": in_sample.get("coverage"),
                "validity_all": in_sample.get("validity"),
            }
        )
    return {
        "archive_digest": measure_mod.archive_digest(entries),
        "evidence_digest": measure_mod.evidence_digest(evidence),
        "versions": rows,
    }


def summary_lines(result: dict) -> list[str]:
    lines = ["out-of-sample replay (each version judged only on rounds archived after it existed):"]
    for r in result["versions"]:
        cov = "n/a" if r["coverage_oos"] is None else f"{r['coverage_oos']:.2f}"
        val = "n/a" if r["validity_oos"] is None else f"{r['validity_oos']:.2f}"
        lines.append(
            f"  v{r['version']} ({r['origin']}, {r['policy_hash']}): {r['rounds_oos']} later round(s), "
            f"coverage {cov}, validity {val}"
        )
    return lines


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
    parser.add_argument("--history", default=str(policy_mod.HISTORY_PATH))
    parser.add_argument("--trace-evidence", default=None)
    parser.add_argument("--out-json", default=None)
    args = parser.parse_args(argv[1:])
    if args.out_json:
        policy_mod.assert_safe_output(
            args.out_json,
            inputs=[args.archive_path, args.policy, args.history, args.trace_evidence],
        )
    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    history = policy_mod.load_history(args.history)
    entries = [
        json.loads(line)
        for line in Path(args.archive_path).read_text().splitlines()
        if line.strip()
    ]
    evidence = json.loads(Path(args.trace_evidence).read_text()) if args.trace_evidence else None
    result = replay(entries, policy, history, evidence)
    for line in summary_lines(result):
        print(line)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, indent=2) + "\n")
    print("---")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
