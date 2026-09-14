#!/usr/bin/env python3
"""The L5 step: revise the improvement mechanism's own policy from evidence,
version it, and roll it back when the revision did not help.

scripts/detect-recurring-pattern.py decides target-vs-mechanism fixes using
docs/improvement-policy.json. scripts/measure-policy-validity.py measures
whether that policy's signal predicts the field (coverage of real findings,
and agreement with independent Traces evidence). This script closes the
recursion: when those measures fall below the fixed acceptance thresholds,
it proposes a new policy version, and when a previously adopted revision
turns out worse than its parent over the rounds that followed, it proposes
rolling back to the parent.

What it may change (AI-owned, see improvement_policy.AI_OWNED_COMPONENTS):
  - the taxonomy: add topics mined from findings the policy failed to
    classify (coverage repair);
  - per-topic weights: discount topics that reviews keep crediting but the
    field never corroborates, restore them when the field does (validity
    repair).

What it may NOT change (fixed infrastructure): the archive, the verifier,
the anchor, the thresholds below, and the promotion path -- every proposal
lands as a pull request a human merges. `assert_ai_may_write` enforces the
file boundary; the thresholds are constants here, not fields of the policy,
so a revision cannot loosen the rule that judges revisions.

Every revision is bounded and auditable: at most MAX_NEW_TOPICS topics per
revision, each backed by at least MIN_FINDINGS_PER_TOPIC previously
unclassified findings, keywords chosen by document frequency (no model, no
external call), appended after existing topics so nothing already
classified changes bucket.

Usage:
    python3 revise-improvement-policy.py <archive.jsonl> --measurement MEASUREMENT.json
        [--policy PATH] [--history PATH] [--out-policy PATH] [--dry-run] [--now ISO]

Prints human-readable lines, then `---`, then a JSON object describing what
was (or would be) done. Exit code 0 always unless inputs are unusable.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
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
measure_mod = _load_sibling_module("measure_policy_validity", "measure-policy-validity.py")

# Meta-acceptance rule. Fixed infrastructure: deliberately not part of the
# policy document, so the thing being revised cannot loosen its own judge.
MIN_COVERAGE = 0.8
MIN_VALIDITY = 0.3
MIN_ROUNDS_TO_JUDGE = 2
MAX_NEW_TOPICS = 2
MIN_FINDINGS_PER_TOPIC = 2
MAX_KEYWORDS_PER_TOPIC = 5
MIN_TOKEN_LENGTH = 4
WEIGHT_DISCOUNT = 0.5
MIN_WEIGHT = 0.25

STOPWORDS = frozenset(
    [
        "about",
        "above",
        "after",
        "again",
        "against",
        "also",
        "always",
        "another",
        "anything",
        "archive-less",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "cannot",
        "check",
        "checks",
        "existing",
        "compares",
        "compare",
        "comparing",
        "prior",
        "older",
        "newer",
        "could",
        "does",
        "doing",
        "each",
        "either",
        "else",
        "even",
        "ever",
        "every",
        "exactly",
        "from",
        "further",
        "have",
        "having",
        "here",
        "into",
        "itself",
        "just",
        "later",
        "less",
        "like",
        "more",
        "most",
        "much",
        "must",
        "never",
        "none",
        "only",
        "other",
        "ought",
        "over",
        "own",
        "rather",
        "same",
        "should",
        "since",
        "some",
        "still",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "until",
        "upon",
        "very",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "whose",
        "will",
        "with",
        "within",
        "without",
        "would",
        "your",
        "finding",
        "findings",
        "review",
        "reviews",
        "reviewed",
        "codex",
        "github",
        "workflow",
        "workflows",
        "step",
        "steps",
        "file",
        "files",
        "code",
        "change",
        "changes",
        "also",
        "does",
        "each",
        "first",
        "second",
        "third",
        "same",
        "same",
        "this",
        "that",
        "these",
        "those",
        "into",
        "onto",
        "both",
        "each",
        "which",
        "their",
    ]
)

_TOKEN_RE = re.compile(r"[a-z][a-z_-]{2,}")


def tokenize(text: str) -> set[str]:
    return {
        tok.strip("-_")
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= MIN_TOKEN_LENGTH
        and tok not in STOPWORDS
        and not tok.startswith("http")
        and not tok.endswith("ly")  # adverbs name manner, never a class of problem
    }


def existing_keyword_tokens(keywords: dict[str, list[str]]) -> set[str]:
    return {k.lower() for words in keywords.values() for k in words}


def mine_topics(
    unclassified: list[dict],
    keywords: dict[str, list[str]],
) -> list[dict]:
    """Greedy, auditable topic mining over findings the policy could not
    classify: the most frequent significant token names a topic; its
    keywords are that token plus the tokens that co-occur with it most;
    findings the new topic covers are removed and the process repeats."""
    taken = existing_keyword_tokens(keywords)
    remaining = [
        {
            "round": item["round"],
            "finding": item["finding"],
            "tokens": tokenize(item["finding"]) - taken,
        }
        for item in unclassified
    ]
    mined: list[dict] = []
    while len(mined) < MAX_NEW_TOPICS and remaining:
        df = Counter(tok for item in remaining for tok in item["tokens"])
        candidates = [t for t, n in df.items() if n >= MIN_FINDINGS_PER_TOPIC]
        if not candidates:
            break
        head = sorted(candidates, key=lambda t: (-df[t], t))[0]
        covering = [item for item in remaining if head in item["tokens"]]
        co = Counter(tok for item in covering for tok in item["tokens"] if tok != head)
        companions = [
            t
            for t, n in sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))
            if n >= MIN_FINDINGS_PER_TOPIC
        ][: MAX_KEYWORDS_PER_TOPIC - 1]
        topic_keywords = [head, *companions]
        name = "-".join(topic_keywords[:2]) if companions else head
        if name in keywords or any(m["name"] == name for m in mined):
            name = f"{name}-{len(mined) + 1}"
        mined.append(
            {
                "name": name,
                "keywords": topic_keywords,
                "evidence": [
                    {"round": item["round"], "finding": item["finding"][:160]} for item in covering
                ],
            }
        )
        taken.update(topic_keywords)
        remaining = [item for item in remaining if head not in item["tokens"]]
        for item in remaining:
            item["tokens"] -= set(topic_keywords)
    return mined


def rounds_since(entries: list[dict], created_at: str) -> int:
    adopted_ms = measure_mod.parse_timestamp_ms(created_at)
    if adopted_ms is None:
        return 0
    return sum(
        1
        for rnd in measure_mod.rounds_in_order(entries)
        if rnd["timestamp_ms"] is not None and rnd["timestamp_ms"] > adopted_ms
    )


def snapshot_for_version(version: int, history: list[dict]) -> dict | None:
    for entry in reversed(history):
        if entry.get("version") == version and isinstance(entry.get("policy"), dict):
            return entry["policy"]
    if version == 1:
        return policy_mod.builtin_policy()
    return None


def adoption_entry(version: int, history: list[dict]) -> dict | None:
    for entry in reversed(history):
        if entry.get("version") == version:
            return entry
    return None


def decide(
    entries: list[dict],
    policy: dict,
    history: list[dict],
    measurement: dict,
    now: str,
) -> dict:
    """Pure decision: returns {"action": "none"|"revise"|"rollback", ...}
    without touching disk, so it can be tested and dry-run."""
    current = measurement["current"]
    coverage = current.get("coverage")
    validity = current.get("validity")
    keywords = policy_mod.topic_keywords(policy)
    weights = policy_mod.topic_weights(policy)

    # 1. Safe inheritance: a revision that made things worse gets rolled back
    #    before any new revision is layered on top of it. Both policies are
    #    re-measured on the SAME findings: comparing today's coverage with
    #    the parent's historical number would punish a revision merely for
    #    being alive when unfamiliar findings arrived (Codex review of
    #    PR #10, finding 1).
    if policy.get("origin") == "revision" and policy.get("parent") is not None:
        adopted = adoption_entry(policy["version"], history)
        if (
            adopted is not None
            and rounds_since(entries, policy["created_at"]) >= MIN_ROUNDS_TO_JUDGE
        ):
            parent = snapshot_for_version(policy["parent"], history)
            if parent is not None and coverage is not None:
                parent_now = measure_mod.measure(entries, parent, None)["current"]["coverage"]
                if parent_now is not None and coverage < parent_now:
                    return {
                        "action": "rollback",
                        "reason": (
                            f"on the same {current['findings_total']} findings, v{policy['version']} covers "
                            f"{coverage} but its parent v{policy['parent']} covers {parent_now}"
                        ),
                        "policy": policy_mod.new_version(
                            policy,
                            topics=parent["topics"],
                            threshold=parent["threshold"],
                            origin="rollback",
                            rationale=f"Rollback to v{policy['parent']}: revision v{policy['version']} lowered coverage.",
                            created_at=now,
                        ),
                        "coverage_before": coverage,
                        "coverage_after": parent_now,
                        "validity_before": validity,
                        "changes": [
                            f"restored taxonomy, weights and threshold of v{policy['parent']}"
                        ],
                    }

    triggers = []
    if coverage is not None and coverage < MIN_COVERAGE:
        triggers.append(f"coverage {coverage} < {MIN_COVERAGE}")
    if validity is not None and validity < MIN_VALIDITY:
        triggers.append(f"validity {validity} < {MIN_VALIDITY}")
    if not triggers:
        return {
            "action": "none",
            "reason": "policy signal still predicts the field within thresholds",
        }

    changes: list[str] = []
    new_topics = dict(policy["topics"])

    # 2. Coverage repair: mine the blind spots.
    mined = (
        mine_topics(current.get("unclassified_findings", []), keywords)
        if coverage is not None and coverage < MIN_COVERAGE
        else []
    )
    for topic in mined:
        new_topics[topic["name"]] = {
            "keywords": topic["keywords"],
            "weight": 1.0,
            "mined_from": topic["evidence"],
        }
        changes.append(
            f"added topic {topic['name']} (keywords {topic['keywords']}) covering {len(topic['evidence'])} unclassified finding(s)"
        )

    # 3. Validity repair: stop crediting what the field never shows.
    anchor = current.get("anchor")
    if anchor is not None:
        for topic in current.get("dev_only_topics", []):
            old = weights[topic]
            new = max(MIN_WEIGHT, round(old * WEIGHT_DISCOUNT, 3))
            if new < old:
                new_topics[topic] = {**new_topics[topic], "weight": new}
                changes.append(
                    f"discounted {topic} weight {old} -> {new}: credited in {current['dev'][topic]} round(s), 0 field traces"
                )
        for topic, count in anchor.items():
            if count is not None and count > 0 and weights.get(topic, 1.0) < 1.0:
                restored = min(1.0, round(weights[topic] / WEIGHT_DISCOUNT, 3))
                new_topics[topic] = {**new_topics[topic], "weight": restored}
                changes.append(
                    f"restored {topic} weight {weights[topic]} -> {restored}: {count} field trace(s)"
                )

    if not changes:
        return {
            "action": "none",
            "reason": "triggered ("
            + "; ".join(triggers)
            + ") but no bounded, evidence-backed change was available",
            "triggers": triggers,
        }

    revised = policy_mod.new_version(
        policy,
        topics=new_topics,
        threshold=policy["threshold"],
        origin="revision",
        rationale="Revised because " + "; ".join(triggers) + ". " + " ".join(changes),
        created_at=now,
    )
    after = measure_mod.measure(entries, revised, None)["current"]
    return {
        "action": "revise",
        "reason": "; ".join(triggers),
        "triggers": triggers,
        "policy": revised,
        "changes": changes,
        "coverage_before": coverage,
        "coverage_after": after["coverage"],
        "validity_before": validity,
        "validity_after": None,  # needs the next anchor collection under the new taxonomy
    }


def history_entry(decision: dict, policy: dict, measurement: dict, now: str) -> dict:
    new_policy = decision["policy"]
    return {
        "version": new_policy["version"],
        "parent": new_policy["parent"],
        "origin": new_policy["origin"],
        "created_at": now,
        "reason": decision["reason"],
        "changes": decision["changes"],
        "coverage_before": decision.get("coverage_before"),
        "coverage_after": decision.get("coverage_after"),
        "validity_before": decision.get("validity_before"),
        "validity_after": decision.get("validity_after"),
        "measured_policy_hash": measurement.get("policy_hash"),
        "anchor": measurement.get("anchor"),
        "replaced_policy_hash": policy_mod.policy_hash(policy),
        "policy": new_policy,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("archive_path")
    parser.add_argument("--measurement", required=True)
    parser.add_argument("--policy", default=str(policy_mod.POLICY_PATH))
    parser.add_argument("--history", default=str(policy_mod.HISTORY_PATH))
    parser.add_argument("--out-policy", default=None, help="Defaults to overwriting --policy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--now", default=None)
    args = parser.parse_args(argv[1:])

    entries = measure_mod.load_archive(args.archive_path)
    policy = policy_mod.load_policy(args.policy)
    history = policy_mod.load_history(args.history)
    with open(args.measurement) as f:
        measurement = json.load(f)
    if measurement.get("policy_hash") != policy_mod.policy_hash(policy):
        print(
            f"::error::measurement was taken under policy hash {measurement.get('policy_hash')}, "
            f"but {args.policy} hashes to {policy_mod.policy_hash(policy)}; re-measure first",
            file=sys.stderr,
        )
        return 1
    now = args.now or policy_mod.utc_now_iso()

    decision = decide(entries, policy, history, measurement, now)
    if decision["action"] == "none":
        print(f"no revision: {decision['reason']}")
    else:
        verb = "ROLLBACK" if decision["action"] == "rollback" else "REVISION"
        print(
            f"{verb} -> policy v{decision['policy']['version']} (parent v{decision['policy']['parent']}): {decision['reason']}"
        )
        for change in decision["changes"]:
            print(f"  - {change}")
        if decision.get("coverage_after") is not None:
            print(f"  coverage {decision['coverage_before']} -> {decision['coverage_after']}")
        if not args.dry_run:
            out_policy = args.out_policy or args.policy
            policy_mod.save_policy(decision["policy"], out_policy)
            policy_mod.append_history(
                history_entry(decision, policy, measurement, now), args.history
            )
            print(
                f"  wrote {policy_mod.relative_to_repo(out_policy)} and {policy_mod.relative_to_repo(args.history)}"
            )

    print("---")
    print(json.dumps(dict(decision), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
