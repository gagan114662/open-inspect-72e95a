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


def is_undated(entry: dict) -> bool:
    return measure_mod.parse_timestamp_ms(entry.get("occurred_at")) is None


def rounds_after(entries: list[dict], created_at: str | None) -> list[dict]:
    """Archive entries recorded after `created_at`, judged by EACH ENTRY's
    own timestamp: a later result line for a round must not drag that
    round's earlier findings into the held-out set (Codex review of PR
    #70). An undated entry is never held-out evidence: without its own
    observation time it may have informed the revision, so inheriting its
    round's earliest time would count seen findings as unseen (Codex
    review of PR #70, round 3). Every entry when the version predates the
    archive."""
    cutoff = measure_mod.parse_timestamp_ms(created_at) if created_at else None
    if cutoff is None:
        return list(entries)
    kept = []
    for e in entries:
        ts = measure_mod.parse_timestamp_ms(e.get("occurred_at"))
        if ts is not None and ts > cutoff:
            kept.append(e)
    return kept


def evidence_after(evidence: dict | None, cutoff_ms: int | None) -> dict | None:
    """The evidence snapshot restricted to field traces observed after
    `cutoff_ms`: held-out validity must not count traces that informed the
    revision (Codex review of PR #70, round 4). A topic with a trace that
    cannot be placed in time is dropped, so its held-out count is unknown
    rather than wrong; the scanned-session list is restricted the same way.
    No cutoff (the root version) keeps every trace."""
    if evidence is None or cutoff_ms is None:
        return evidence
    topics: dict[str, list[dict]] = {}
    for topic, traces in (evidence.get("topics") or {}).items():
        if any(not isinstance(t.get("timestamp"), int | float) for t in traces):
            continue
        topics[topic] = [t for t in traces if t["timestamp"] > cutoff_ms]
    kept_ids = {t["id"] for traces in topics.values() for t in traces}
    sessions = [s for s in evidence.get("sessions") or [] if s in kept_ids]
    return {**evidence, "topics": topics, "sessions": sessions}


def revision_cutoff_ms(version: dict) -> int | None:
    """When a non-root version started existing, or None when its created_at
    is missing or unparseable: such a revision has no defensible held-out
    window (Codex review of PR #70, round 4)."""
    return measure_mod.parse_timestamp_ms(version.get("created_at"))


def is_root_version(version: dict) -> bool:
    # The root version predates the archive by definition: every round was
    # archived under it or its descendants.
    return version.get("origin") == "init" or version.get("parent") is None


def replay(entries: list[dict], policy: dict, history: list[dict], evidence: dict | None) -> dict:
    """Per version: metrics on the rounds after it existed (its own
    held-out set) AND on one common held-out set, the rounds after the
    newest version existed, so versions are compared on the same rounds
    (Codex review of PR #70): a rising per-version line alone says nothing
    when each version is judged on different rounds."""
    versions = versions_in_order(policy, history)
    for v in versions:
        policy_mod.validate_policy(v)  # a tampered snapshot never renders
    # The common window starts when the NEWEST non-root version came to be.
    # If that version has no valid created_at there is no common window at
    # all: falling back to an older version's time would let findings the
    # newest version was trained on count as held out.
    newest = next((v for v in reversed(versions) if not is_root_version(v)), None)
    newest_created = None
    common_cutoff = None
    if newest is not None:
        common_cutoff = revision_cutoff_ms(newest)
        newest_created = newest.get("created_at") if common_cutoff is not None else None
    common = rounds_after(entries, newest_created) if common_cutoff is not None else []
    common_evidence = evidence_after(evidence, common_cutoff)
    rows = []
    for version in versions:
        is_root = is_root_version(version)
        cutoff = None if is_root else revision_cutoff_ms(version)
        held_out = "ok"
        if is_root:
            later = list(entries)
        elif cutoff is None:
            later = []
            held_out = "unavailable: no valid created_at"
        else:
            later = rounds_after(entries, version.get("created_at"))
        if later:
            own_evidence = evidence_after(evidence, cutoff)
            current = measure_mod.measure(later, version, own_evidence)["current"]
            coverage, validity = current.get("coverage"), current.get("validity")
            findings = current.get("findings_total")
        else:
            coverage = validity = findings = None
        in_sample = measure_mod.measure(entries, version, evidence)["current"]
        if common:
            on_common = measure_mod.measure(common, version, common_evidence)["current"]
            coverage_common, validity_common = on_common.get("coverage"), on_common.get("validity")
        else:
            coverage_common = validity_common = None
        rows.append(
            {
                "coverage_common": coverage_common,
                "validity_common": validity_common,
                "held_out": held_out,
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
        "common_window_after": newest_created,
        "common_rounds": len({policy_mod.round_key(e) for e in common}),
        "undated_excluded": sum(1 for e in entries if is_undated(e)),
        "versions": rows,
    }


def summary_lines(result: dict) -> list[str]:
    lines = ["out-of-sample replay (each version judged only on rounds archived after it existed):"]
    if result.get("undated_excluded"):
        lines.append(
            f"  {result['undated_excluded']} undated finding(s) excluded from every held-out set"
        )
    for r in result["versions"]:
        cov = "n/a" if r["coverage_oos"] is None else f"{r['coverage_oos']:.2f}"
        val = "n/a" if r["validity_oos"] is None else f"{r['validity_oos']:.2f}"
        cc = "n/a" if r.get("coverage_common") is None else f"{r['coverage_common']:.2f}"
        lines.append(
            f"  v{r['version']} ({r['origin']}, {r['policy_hash']}): {r['rounds_oos']} later round(s), "
            f"coverage {cov}, validity {val}; on the common {result['common_rounds']} round(s): coverage {cc}"
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
