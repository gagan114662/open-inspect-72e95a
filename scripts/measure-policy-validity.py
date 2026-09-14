#!/usr/bin/env python3
"""Measure whether the improvement policy's own signal predicts what the
field shows -- the L5 trigger from docs/plans/recursive-meta-improvement.md.

The loop in this repo decides "target fix vs mechanism fix" from Codex
review findings bucketed by docs/improvement-policy.json's taxonomy. That
bucketed count is the loop's development score: it is what the mechanism
sees. It can be wrong in two ways the mechanism itself cannot notice:

  1. Coverage: findings the taxonomy does not classify are simply dropped,
     so a class of problem the loop keeps hitting never accumulates toward
     the threshold. Measured as classified / total findings.
  2. Predictive validity: a topic the taxonomy credits heavily may never
     show up in actual working sessions, while one it barely credits does.
     Measured as the Spearman rank correlation, across topics, between the
     review-derived recurrence (rounds with a finding) and an independent
     anchor: Traces evidence from working sessions in this repository.

The anchor deliberately excludes the verifier's own transcripts (Codex
review sessions) by default: those contain the findings themselves, so
counting them would make the anchor echo the development score instead of
checking it (paper failure mode 3, "reliable verification").

Both measures are replayed per archive round, using only the rounds and
traces that existed at that round's timestamp, so the dashboard can show
when a revision would have fired, not just where things stand now.

Usage:
    python3 measure-policy-validity.py <archive.jsonl>
        [--policy PATH] [--trace-evidence EVIDENCE.json]
        [--repo-dir DIR [--save-evidence EVIDENCE.json] [--anchor-agents a,b|all]]
        [--traces-bin PATH]

Without --trace-evidence or --repo-dir the anchor is absent: coverage is
still measured, validity is reported as null, and the JSON says so plainly.
Prints human-readable lines, then a `---` separator, then a JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
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

DEFAULT_ANCHOR_AGENTS = ["claude-code", "antigravity", "cursor", "droid", "openclaw", "pi"]
VERIFIER_AGENT = "codex"
MIN_TOPICS_FOR_VALIDITY = 3
SEARCH_RESULT_LIMIT = 500


class TracesCliError(RuntimeError):
    pass


# --- archive replay ---------------------------------------------------------


def load_archive(path: str) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def archive_digest(entries: list[dict]) -> str:
    """Content digest of the archive a measurement was taken against, so a
    decision can refuse a measurement from a different archive (Codex
    review of PR #10, round 2, finding 2)."""
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def parse_timestamp_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def rounds_in_order(entries: list[dict]) -> list[dict]:
    """Merge archive entries by round number (a round may be recorded as a
    'pending' placeholder and later as its result) and carry the latest
    parseable timestamp forward so every epoch has a time."""
    by_round: dict[int, dict] = {}
    for entry in entries:
        round_num = entry.get("round")
        if not isinstance(round_num, int):
            continue
        merged = by_round.setdefault(
            round_num, {"round": round_num, "findings": [], "timestamp_ms": None}
        )
        merged["findings"].extend(f for f in entry.get("findings", []) if isinstance(f, str))
        ts = parse_timestamp_ms(entry.get("occurred_at"))
        if ts is not None and (merged["timestamp_ms"] is None or ts > merged["timestamp_ms"]):
            merged["timestamp_ms"] = ts
    ordered = [by_round[r] for r in sorted(by_round)]
    last_ts: int | None = None
    for rnd in ordered:
        if rnd["timestamp_ms"] is None:
            rnd["timestamp_ms"] = last_ts
        last_ts = rnd["timestamp_ms"]
    # Replay order is time order, not round-number order: a round recorded
    # later than a higher-numbered one must not be replayed against an
    # earlier field snapshot (Codex review of PR #10, round 2, finding 4).
    return sorted(
        ordered,
        key=lambda r: (r["timestamp_ms"] if r["timestamp_ms"] is not None else -1, r["round"]),
    )


# --- anchor evidence ---------------------------------------------------------


def run_traces_json(traces_bin: str, args: list[str]) -> dict:
    try:
        result = subprocess.run(
            [traces_bin, *args, "--json"], capture_output=True, text=True, timeout=60
        )
    except OSError as exc:
        raise TracesCliError(f"Could not run `{traces_bin}`: {exc}") from exc
    if result.returncode != 0:
        raise TracesCliError(f"`{traces_bin} {' '.join(args)}` failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TracesCliError(f"Non-JSON output from `{traces_bin} {' '.join(args)}`") from exc
    if not payload.get("ok"):
        raise TracesCliError(f"`{traces_bin} {' '.join(args)}` reported failure: {payload}")
    return payload["data"]


def topic_pattern(keywords: list[str]) -> str:
    return "|".join(re.escape(k) for k in keywords)


def collect_trace_evidence(
    traces_bin: str,
    repo_dir: str,
    keywords: dict[str, list[str]],
    anchor_agents: list[str] | None,
) -> dict:
    """One Traces search per topic, scoped to the repository directory and
    (unless 'all') to non-verifier agents. Stores only ids, agents and
    timestamps -- enough to replay the anchor per epoch, no transcript text."""
    topics: dict[str, list[dict]] = {}
    truncated: list[str] = []
    for topic, words in keywords.items():
        matches: dict[str, dict] = {}
        agent_filters: list[list[str]] = (
            [["--agent", agent] for agent in anchor_agents] if anchor_agents else [[]]
        )
        for agent_args in agent_filters:
            data = run_traces_json(
                traces_bin,
                [
                    "search",
                    topic_pattern(words),
                    "--dir",
                    repo_dir,
                    *agent_args,
                    "--result-level",
                    "trace",
                    "--limit",
                    str(SEARCH_RESULT_LIMIT),
                ],
            )
            found = data.get("traces", [])
            if len(found) >= SEARCH_RESULT_LIMIT and topic not in truncated:
                # A capped result is a lower bound, not a count; recording it
                # as absolute would flatten frequent topics into identical
                # numbers (Codex review of PR #10, round 5).
                truncated.append(topic)
            for trace in found:
                matches[trace["id"]] = {
                    "id": trace["id"],
                    "agentId": trace.get("agentId"),
                    "timestamp": trace.get("timestamp"),
                }
        topics[topic] = sorted(matches.values(), key=lambda t: (t["timestamp"] or 0, t["id"]))
    return {
        "source": "traces",
        "collected_at": policy_mod.utc_now_iso(),
        "repo_dir": repo_dir,
        "agents": anchor_agents or ["all"],
        "topics": topics,
        "truncated": truncated,
    }


def anchor_counts_at(
    evidence: dict | None, topics: list[str], until_ms: int | None
) -> dict[str, int | None] | None:
    """Per-topic trace counts at a point in time. A topic the evidence
    snapshot never searched (added by a later policy revision) is None,
    unknown, not zero: reusing an old snapshot must not make a new topic
    look unsupported (Codex review of PR #10, finding 5)."""
    if evidence is None:
        return None
    searched = evidence.get("topics", {})
    truncated = set(evidence.get("truncated", []))
    counts: dict[str, int | None] = {}
    for topic in topics:
        if topic not in searched or topic in truncated:
            counts[topic] = None
            continue
        traces = searched[topic]
        if until_ms is None:
            counts[topic] = len(traces)
        else:
            counts[topic] = sum(1 for t in traces if (t.get("timestamp") or 0) <= until_ms)
    return counts


# --- statistics --------------------------------------------------------------


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < MIN_TOPICS_FOR_VALIDITY:
        return None
    if len(set(xs)) == 1 or len(set(ys)) == 1:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx * vy), 4)


# --- measurement -------------------------------------------------------------


def measure_epoch(
    rounds: list[dict],
    keywords: dict[str, list[str]],
    weights: dict[str, float],
    evidence: dict | None,
    until_ms: int | None,
) -> dict:
    topics = list(keywords)
    dev_rounds: dict[str, set[int]] = {t: set() for t in topics}
    total = 0
    classified = 0
    unclassified: list[dict] = []
    for rnd in rounds:
        for finding in rnd["findings"]:
            total += 1
            topic = policy_mod.classify_finding(finding, keywords)
            if topic is None:
                unclassified.append({"round": rnd["round"], "finding": finding})
                continue
            classified += 1
            dev_rounds[topic].add(rnd["round"])
    dev = {t: len(dev_rounds[t]) for t in topics}
    # The detector decides on weighted recurrence, so validity must be
    # measured on the same signal, or discounting a topic could never
    # change what is measured (Codex review of PR #10, finding 4).
    dev_weighted = {t: round(dev[t] * weights.get(t, 1.0), 4) for t in topics}
    anchor = anchor_counts_at(evidence, topics, until_ms)
    validity = None
    known = [t for t in topics if anchor is not None and anchor[t] is not None]
    if anchor is not None:
        validity = spearman(
            [float(dev_weighted[t]) for t in known], [float(anchor[t]) for t in known]
        )
    coverage = round(classified / total, 4) if total else None
    return {
        "round": rounds[-1]["round"] if rounds else None,
        "timestamp_ms": until_ms,
        "findings_total": total,
        "findings_classified": classified,
        "coverage": coverage,
        "dev": dev,
        "dev_weighted": dev_weighted,
        "anchor": anchor,
        "anchor_unknown_topics": sorted(
            t for t in topics if anchor is not None and anchor[t] is None
        ),
        "validity": validity,
        "unclassified_findings": unclassified,
        "dev_only_topics": sorted(
            t for t in known if dev[t] >= 2 and anchor is not None and anchor[t] == 0
        ),
        "anchor_only_topics": sorted(
            t for t in known if dev[t] == 0 and anchor is not None and (anchor[t] or 0) > 0
        ),
    }


def evidence_trace_ids(evidence: dict | None) -> set[str]:
    if evidence is None:
        return set()
    return {t["id"] for traces in evidence.get("topics", {}).values() for t in traces}


def measure(entries: list[dict], policy: dict, evidence: dict | None) -> dict:
    keywords = policy_mod.topic_keywords(policy)
    weights = policy_mod.topic_weights(policy)
    rounds = rounds_in_order(entries)
    anchor_meta: dict = {"source": "none", "agents": [], "traces_considered": 0}
    if evidence is not None:
        seen = evidence_trace_ids(evidence)
        anchor_meta = {
            "source": evidence.get("source", "traces"),
            "agents": evidence.get("agents", []),
            "collected_at": evidence.get("collected_at"),
            "traces_considered": len(seen),
        }
        if not seen:
            # An anchor with no traces at all is absence of evidence, not
            # evidence of absence: treat it as no anchor so nothing gets
            # discounted for failing to appear in a field nobody observed.
            anchor_meta["source"] = f"{anchor_meta['source']} (empty)"
            evidence = None
    epochs = []
    for i in range(len(rounds)):
        epoch = measure_epoch(
            rounds[: i + 1], keywords, weights, evidence, rounds[i]["timestamp_ms"]
        )
        epoch.pop("unclassified_findings")
        epochs.append(epoch)
    current = measure_epoch(rounds, keywords, weights, evidence, None)
    # Evidence is a snapshot: findings archived after it was collected come
    # from sessions it never searched, so they must not mark a topic as
    # "credited by reviews, never seen in the field" (Codex review of
    # PR #10, round 6). Weight-relevant fields are recomputed over the
    # rounds the snapshot could have seen; the count of newer rounds is
    # reported so a caller can insist on fresh evidence.
    rounds_after_evidence = 0
    if evidence is not None:
        collected_ms = parse_timestamp_ms(evidence.get("collected_at"))
        if collected_ms is not None:
            seen_rounds = [
                r
                for r in rounds
                if r["timestamp_ms"] is not None and r["timestamp_ms"] <= collected_ms
            ]
            rounds_after_evidence = len(rounds) - len(seen_rounds)
            aligned = measure_epoch(seen_rounds, keywords, weights, evidence, None)
            current["dev_only_topics"] = aligned["dev_only_topics"]
            current["anchor_only_topics"] = aligned["anchor_only_topics"]
    current["rounds_after_evidence"] = rounds_after_evidence
    return {
        "policy_version": policy["version"],
        "policy_hash": policy_mod.policy_hash(policy),
        "archive_digest": archive_digest(entries),
        "anchor": anchor_meta,
        "epochs": epochs,
        "current": current,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("archive_path")
    parser.add_argument("--policy", default=None)
    parser.add_argument("--trace-evidence", default=None)
    parser.add_argument("--repo-dir", default=None)
    parser.add_argument("--save-evidence", default=None)
    parser.add_argument("--anchor-agents", default=",".join(DEFAULT_ANCHOR_AGENTS))
    parser.add_argument("--traces-bin", default="traces")
    args = parser.parse_args(argv[1:])

    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    entries = load_archive(args.archive_path)

    evidence: dict | None = None
    if args.trace_evidence:
        with open(args.trace_evidence) as f:
            evidence = json.load(f)
    elif args.repo_dir:
        agents = (
            None
            if args.anchor_agents.strip() == "all"
            else [a.strip() for a in args.anchor_agents.split(",") if a.strip()]
        )
        try:
            evidence = collect_trace_evidence(
                args.traces_bin, args.repo_dir, policy_mod.topic_keywords(policy), agents
            )
        except TracesCliError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
        if args.save_evidence:
            Path(args.save_evidence).write_text(json.dumps(evidence, indent=2) + "\n")

    result = measure(entries, policy, evidence)
    current = result["current"]
    print(
        f"policy v{result['policy_version']} ({result['policy_hash']}): "
        f"coverage {current['coverage']} over {current['findings_total']} finding(s); "
        f"validity {current['validity']} "
        f"(anchor: {result['anchor']['source']}, {result['anchor']['traces_considered']} trace(s))"
    )
    for item in current["unclassified_findings"]:
        print(f"  unclassified (round {item['round']}): {item['finding'][:100]}")
    if current["dev_only_topics"]:
        print(f"  credited by reviews, never seen in the field: {current['dev_only_topics']}")
    if current["anchor_only_topics"]:
        print(f"  seen in the field, never credited by reviews: {current['anchor_only_topics']}")
    if current["anchor_unknown_topics"]:
        print(
            f"  not yet searched in the field (re-collect evidence): {current['anchor_unknown_topics']}"
        )
    print("---")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
