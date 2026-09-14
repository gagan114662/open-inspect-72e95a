"""The improvement mechanism's own policy, as versioned data instead of code.

Before this module, the rule that decides whether a recurring finding
deserves a target-level patch or a mechanism-level fix lived as constants in
scripts/detect-recurring-pattern.py: a keyword taxonomy and a recurrence
threshold, written by hand once and never revisited. That is an L4 loop in
the paper's terms (docs/plans/recursive-meta-improvement.md): the system
adapts its deployed state, but the mechanism governing what counts as an
improvement stays fixed human infrastructure.

L5 requires that mechanism to be something the system can revise from
evidence, with the same safeguards it applies to every other change. So the
policy becomes a JSON document with a version, a parent, and an origin, and
every revision is appended to a history file with the evidence that
justified it. The pieces that must NOT be revisable by the meta-improver
(the archive, the external anchor, the independent verifier, the acceptance
thresholds, and the promotion path) are enumerated in FIXED_INFRASTRUCTURE,
and `assert_ai_may_write` refuses any write outside AI_OWNED_COMPONENTS.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "docs" / "improvement-policy.json"
HISTORY_PATH = REPO_ROOT / "docs" / "improvement-policy-history.jsonl"

# The v1 taxonomy and threshold, kept in code only as a fallback so every
# existing tool still runs in a checkout that predates the policy file.
BUILTIN_THRESHOLD = 3
BUILTIN_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "credential-redaction": ["redact", "credential", "secret", "token", "leak", "expos"],
    "shell-semantics": ["errexit", "bash -e", "exit code", "-e", "pipefail", "shell"],
    "env-var-precedence": ["precedence", "env var", "environment variable", "unconditionally"],
    "fork-pr-permissions": ["fork", "github_token", "persist-credentials"],
    "auth-lifecycle": ["refresh token", "rotat", "expir", "auth.json", "stale"],
}

# Autonomy attribution (paper failure mode 2): the meta-improver may rewrite
# exactly these files, and nothing else. Paths are repo-relative.
AI_OWNED_COMPONENTS: dict[str, str] = {
    "improvement-policy": "docs/improvement-policy.json",
    "improvement-policy-history": "docs/improvement-policy-history.jsonl",
}

# Everything the loop depends on that stays human-owned infrastructure. The
# dashboard renders this list verbatim so the boundary is visible, not implied.
FIXED_INFRASTRUCTURE: dict[str, str] = {
    "archive": "docs/self-improvement-archive.jsonl — append-only, SHA-idempotent (archive-round.py)",
    "verifier": ".github/workflows/codex-review.yml — independent second-model review of every PR",
    "anchor": "Traces evidence from working sessions — never consulted when a round is decided",
    "meta-acceptance-rule": "MIN_COVERAGE / MIN_VALIDITY / MIN_ROUNDS_TO_JUDGE in revise-improvement-policy.py",
    "promotion": "pull requests only; a human merges every policy revision and every rollback",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def builtin_policy() -> dict:
    return {
        "version": 1,
        "parent": None,
        "origin": "init",
        "created_at": "2026-09-14T18:00:00Z",
        "threshold": BUILTIN_THRESHOLD,
        "topics": {
            topic: {"keywords": list(keywords), "weight": 1.0}
            for topic, keywords in BUILTIN_TOPIC_KEYWORDS.items()
        },
        "rationale": "Built-in fallback identical to policy version 1.",
    }


def load_policy(path: Path | str = POLICY_PATH) -> dict:
    with open(path) as f:
        policy = json.load(f)
    validate_policy(policy)
    return policy


def load_policy_or_builtin(path: Path | str = POLICY_PATH) -> dict:
    if Path(path).exists():
        return load_policy(path)
    return builtin_policy()


def validate_policy(policy: dict) -> None:
    if not isinstance(policy.get("version"), int) or policy["version"] < 1:
        raise ValueError("policy.version must be a positive integer")
    if not isinstance(policy.get("threshold"), int) or policy["threshold"] < 1:
        raise ValueError("policy.threshold must be a positive integer")
    topics = policy.get("topics")
    if not isinstance(topics, dict) or not topics:
        raise ValueError("policy.topics must be a non-empty object")
    for name, spec in topics.items():
        keywords = spec.get("keywords")
        if not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords):
            raise ValueError(f"topic {name!r} needs a non-empty list of keyword strings")
        weight = spec.get("weight", 1.0)
        if not isinstance(weight, int | float) or weight <= 0:
            raise ValueError(f"topic {name!r} weight must be a positive number")


def policy_hash(policy: dict) -> str:
    """Content hash of the decision-relevant fields. Two policies with the
    same taxonomy, weights, and threshold decide identically, whatever their
    version metadata says — this is what the dashboard pins per epoch to
    show the evaluator was frozen while a round was decided.

    Topic ORDER is part of the hash: classification takes the first topic
    whose keyword matches, so reordering overlapping topics changes
    decisions and must not pass the stale-measurement guard (Codex review
    of PR #10, finding 3)."""
    canonical = json.dumps(
        {
            "threshold": policy["threshold"],
            "topics": [
                [name, spec["keywords"], float(spec.get("weight", 1.0))]
                for name, spec in policy["topics"].items()
            ],
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def topic_keywords(policy: dict) -> dict[str, list[str]]:
    return {name: list(spec["keywords"]) for name, spec in policy["topics"].items()}


def topic_weights(policy: dict) -> dict[str, float]:
    return {name: float(spec.get("weight", 1.0)) for name, spec in policy["topics"].items()}


def classify_finding(text: str, keywords: dict[str, list[str]]) -> str | None:
    """First topic (in policy order) with any keyword present. Same rule the
    detector has always applied; it lives here so every tool classifies
    identically under the same policy version."""
    lowered = text.lower()
    for topic, words in keywords.items():
        if any(word in lowered for word in words):
            return topic
    return None


def new_version(
    parent: dict,
    *,
    topics: dict,
    threshold: int,
    origin: str,
    rationale: str,
    created_at: str | None = None,
) -> dict:
    if origin not in {"revision", "rollback"}:
        raise ValueError("origin must be 'revision' or 'rollback'")
    policy = {
        "version": parent["version"] + 1,
        "parent": parent["version"],
        "origin": origin,
        "created_at": created_at or utc_now_iso(),
        "threshold": threshold,
        "topics": topics,
        "rationale": rationale,
    }
    validate_policy(policy)
    return policy


def relative_to_repo(path: Path | str) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def assert_ai_may_write(path: Path | str, *, allowed: dict[str, str] | None = None) -> None:
    """Attribution guard: the meta-improver only ever writes the files it owns.
    Raises PermissionError otherwise, so a bug that tries to 'fix' the archive
    or the verifier fails loudly instead of silently widening autonomy."""
    allowed_paths = set((allowed or AI_OWNED_COMPONENTS).values())
    rel = relative_to_repo(path)
    if rel not in allowed_paths:
        raise PermissionError(
            f"{rel} is fixed infrastructure; the meta-improver may only write {sorted(allowed_paths)}"
        )


def save_policy(
    policy: dict, path: Path | str = POLICY_PATH, *, allowed: dict[str, str] | None = None
) -> None:
    assert_ai_may_write(path, allowed=allowed)
    validate_policy(policy)
    Path(path).write_text(json.dumps(policy, indent=2) + "\n")


def load_history(path: Path | str = HISTORY_PATH) -> list[dict]:
    if not Path(path).exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def append_history(
    entry: dict, path: Path | str = HISTORY_PATH, *, allowed: dict[str, str] | None = None
) -> None:
    assert_ai_may_write(path, allowed=allowed)
    # No sort_keys: a snapshot's topic order is its classification
    # precedence, and restoring an alphabetized snapshot would silently
    # reclassify findings (Codex review of PR #10, finding 2).
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
