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
import re
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


VALID_ORIGINS = frozenset({"init", "revision", "rollback"})

# Shapes of secrets that show up in failed commands and their output. Text
# that reaches a report, a log, or the policy's `mined_from` evidence passes
# through scrub_secrets first, because a proposal PR is public the moment it
# is pushed (Codex review of PR #10, round 38).
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s]+@"),  # scheme://user:PASS@
    re.compile(r"(?i)\b((?:bearer|basic|token|digest)\s+)[a-z0-9._~+/=-]{8,}"),
    # The whole header value, scheme included (round 41).
    re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?(?:[a-z]+\s+)?)[^\s\"']+"),
    re.compile(
        r"(?i)\b((?:[a-z0-9_]*)(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|auth)[a-z0-9_]*\s*[=:]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s\"']{4,})"
    ),
    # Command-line flags: --password VALUE, --token=VALUE, -p VALUE, -pVALUE,
    # and whole quoted values with spaces (rounds 40-42).
    re.compile(
        r"(?i)((?:--?[a-z0-9-]*(?:token|secret|password|passwd|api-?key|access-?key|private-?key|auth|key)\b|(?<!\S)-[pa])(?:\s+|=)?)"
        r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s\"']{4,})"
    ),
    # curl-style user:password arguments, attached or not, quoted or not:
    # -u user:PASS, -uuser:PASS, --user 'user:PASS WITH SPACES' (rounds 43-44).
    re.compile(
        r"(?i)((?<!\S)(?:-u|--user|--proxy-user|--login)(?:\s+|=)?)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    ),
    re.compile(r"(?i)((?<!\S)(?:-u|--user|--proxy-user|--login)(?:\s+|=)?[^\s:\"']+:)[^\s\"']+"),
    # Quoted JSON / Python-dict / YAML fields, either quote style, matched to
    # the ACTUAL enclosing delimiter so a value containing the other quote or
    # an escaped quote is redacted whole (rounds 39, 45 and 46).
    re.compile(
        r"(?i)(?P<keep>(?P<q1>[\"'])[a-z0-9_]*(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|auth)[a-z0-9_]*(?P=q1)\s*[:=]\s*(?P<q2>[\"']))"
        r"(?P<val>(?:\\.|(?!(?P=q2)).)+)(?P<close>(?P=q2))"
    ),
    re.compile(r"\b(sk|ghp|gho|ghu|ghs|ghr|vcp|xox[abp]|npm_|pypi-|glpat-|AKIA)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    # Shape-agnostic last layer: any long token mixing letters and digits
    # (API keys, encoded blobs, hashes). Topic keywords are words, so
    # losing such tokens costs the evidence nothing (round 44).
    re.compile(
        r"(?<![\w/.-])(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{20,}(?![\w/.-])"
    ),
)


def scrub_secrets(text: str) -> str:
    """Replace credential-shaped substrings with [REDACTED], keeping the
    surrounding words so the failure stays classifiable."""
    for pattern in _SECRET_SHAPES:
        if not pattern.groups:
            text = pattern.sub("[REDACTED]", text)
        else:
            text = pattern.sub(_redact_match, text)
    return text


def _redact_match(m: re.Match[str]) -> str:
    names = m.re.groupindex
    keep = m.group("keep") if "keep" in names else m.group(1)
    close = m.group("close") if "close" in names else ("@" if m.group(0).endswith("@") else "")
    return f"{keep}[REDACTED]{close}"


def round_key(entry: dict) -> str:
    """Identity of an archived round, shared by every tool that counts
    rounds (the detector that opens issues and the measurer the policy is
    judged on must count the same signal — Codex review of PR #10, round
    37). Automated rounds are identified by the commit they reviewed: two
    archive proposals opened before either merged both computed the same
    next round number, and merging by number alone collapsed two reviews
    into one round (full-branch review, workflows finding 2). Legacy rounds
    without a source_sha keep their number (placeholder + result pairs)."""
    sha = entry.get("source_sha")
    if isinstance(sha, str) and sha:
        return f"sha:{sha}"
    return f"round:{entry.get('round')}"


def assert_policy_matches_history(policy: dict, history: list[dict]) -> None:
    """The policy's lineage metadata (version, parent, origin,
    restored_version) is not part of its content hash, yet the wait gate and
    rollback logic depend on it. So a policy in force must be exactly the
    snapshot its history recorded for that version, metadata included; a
    root policy must not claim to be a revision or rollback. Otherwise an
    edit to `origin` alone would switch the evaluation gates off (Codex
    full-branch review, finding 1)."""
    if not history:
        if policy.get("origin") != "init" or policy.get("parent") is not None:
            raise ValueError(
                "policy claims a revision/rollback lineage but the history records no versions"
            )
        return
    latest = history[-1]
    snapshot = latest.get("policy") or {}
    if latest.get("version") != policy.get("version"):
        raise ValueError(
            f"policy is v{policy.get('version')} but the history's latest entry is "
            f"v{latest.get('version')}; the policy and its history must be written together"
        )
    for key in ("version", "parent", "origin", "restored_version"):
        if snapshot.get(key) != policy.get(key):
            raise ValueError(
                f"policy.{key}={policy.get(key)!r} differs from the recorded v{policy.get('version')} "
                f"snapshot ({snapshot.get(key)!r}); lineage metadata may not be edited in place"
            )
    if policy_hash(snapshot) != policy_hash(policy):
        raise ValueError(
            f"policy content hashes to {policy_hash(policy)} but the recorded v{policy.get('version')} "
            f"snapshot hashes to {policy_hash(snapshot)}"
        )


def validate_policy(policy: dict) -> None:
    if not isinstance(policy.get("version"), int) or policy["version"] < 1:
        raise ValueError("policy.version must be a positive integer")
    if not isinstance(policy.get("threshold"), int) or policy["threshold"] < 1:
        raise ValueError("policy.threshold must be a positive integer")
    if policy.get("origin") not in VALID_ORIGINS:
        raise ValueError(f"policy.origin must be one of {sorted(VALID_ORIGINS)}")
    topics = policy.get("topics")
    if not isinstance(topics, dict) or not topics:
        raise ValueError("policy.topics must be a non-empty object")
    for name, spec in topics.items():
        if not isinstance(name, str) or not name or "@" in name or name != name.strip():
            # `name@tag` keys are reserved for older definitions of a name in
            # the evidence; a topic named that way would be skipped by the
            # candidate anchor and escape the validity comparison (Codex
            # full-branch review, finding 2).
            raise ValueError(
                f"topic name {name!r} is invalid (non-empty, no '@', no surrounding whitespace)"
            )
        keywords = spec.get("keywords")
        if (
            not isinstance(keywords, list)
            or not keywords
            or not all(isinstance(k, str) and k for k in keywords)
        ):
            # An empty list would classify nothing while matching every trace
            # (Codex review of PR #10, round 16).
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
    restored_version: int | None = None,
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
    if origin == "rollback":
        # Which version's configuration this restores, so ancestry checks can
        # continue through it (Codex review of PR #10, round 17).
        policy["restored_version"] = restored_version
    validate_policy(policy)
    return policy


def relative_to_repo(path: Path | str) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def component_paths(role: str | None, allowed: dict[str, str] | None = None) -> set[str]:
    """Paths the meta-improver may write for one role ('policy' or
    'history'), or for any role when role is None."""
    components = allowed or AI_OWNED_COMPONENTS
    if role is None:
        return set(components.values())
    return {p for name, p in components.items() if name == role or name.endswith(f"-{role}")}


def assert_ai_may_write(
    path: Path | str, *, allowed: dict[str, str] | None = None, role: str | None = None
) -> None:
    """Attribution guard: the meta-improver only ever writes the files it owns,
    and each file only in its own role: the policy destination must be the
    policy component and the history destination the history component, so
    swapped arguments cannot append a policy to the history or overwrite the
    policy with a history line (Codex review of PR #10, rounds 20 and 35).
    Raises PermissionError otherwise, so a bug that tries to 'fix' the archive
    or the verifier fails loudly instead of silently widening autonomy."""
    allowed_paths = component_paths(role, allowed)
    rel = relative_to_repo(path)
    if rel not in allowed_paths:
        what = f"the {role} component" if role else "a file it owns"
        raise PermissionError(
            f"{rel} is fixed infrastructure or not {what}; "
            f"the meta-improver may only write {sorted(allowed_paths)} here"
        )


PROTECTED_OUTPUT_PREFIXES: tuple[str, ...] = (
    ".github/",
    "scripts/",
    "tools/",
    "packages/",
    "terraform/",
)
PROTECTED_OUTPUT_FILES: tuple[str, ...] = (
    # The agent's root-level entry point and contract are code, not outputs
    # (Codex review of PR #68, round 8).
    "run.py",
    "agent.json",
    "instructions.md",
    "docs/self-improvement-archive.jsonl",
    "docs/improvement-policy.json",
    "docs/improvement-policy-history.jsonl",
)
# The committed field anchors: only a deliberate evidence refresh may write
# them, never a report or decision output (Codex review of PR #10, round 15).
CANONICAL_EVIDENCE_FILES: tuple[str, ...] = (
    "docs/rsi/trace-evidence.json",
    "docs/rsi/trace-evidence-verifier.json",
)


def assert_safe_output(
    path: Path | str, *, inputs: list[str | Path] = (), kind: str = "report"
) -> None:
    """Side outputs may go anywhere EXCEPT the loop's own records, its code,
    the files the invocation is reading, and (for anything but an evidence
    refresh) the canonical evidence snapshots (Codex review of PR #10,
    rounds 11 and 15)."""
    rel = relative_to_repo(path)
    if rel in PROTECTED_OUTPUT_FILES or any(rel.startswith(p) for p in PROTECTED_OUTPUT_PREFIXES):
        raise PermissionError(f"{rel} is a protected file; choose another output path")
    if kind != "evidence" and rel in CANONICAL_EVIDENCE_FILES:
        raise PermissionError(
            f"{rel} is a canonical evidence snapshot; only --save-evidence may write it"
        )
    # Identity is by path AND by inode: a hard link to the archive named
    # report.json resolves to a different path but is the same file (Codex
    # full-branch review, finding 6).
    for protected in (
        *PROTECTED_OUTPUT_FILES,
        *(() if kind == "evidence" else CANONICAL_EVIDENCE_FILES),
    ):
        if same_file(REPO_ROOT / protected, path):
            raise PermissionError(f"{rel} is the same file as protected {protected}")
    for source in inputs:
        if source and (Path(source).resolve() == Path(path).resolve() or same_file(source, path)):
            raise PermissionError(f"{rel} is an input of this run; choose another output path")


def same_file(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).samefile(b)
    except OSError:
        return False


def save_policy(
    policy: dict, path: Path | str = POLICY_PATH, *, allowed: dict[str, str] | None = None
) -> None:
    assert_ai_may_write(path, allowed=allowed, role="policy")
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
    assert_ai_may_write(path, allowed=allowed, role="history")
    # No sort_keys: a snapshot's topic order is its classification
    # precedence, and restoring an alphabetized snapshot would silently
    # reclassify findings (Codex review of PR #10, finding 2).
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# The checked-in policy, resolved once so every tool stamps and decides with
# the same version and hash in one process.
_CURRENT = load_policy_or_builtin()
POLICY_VERSION: int = _CURRENT["version"]
POLICY_HASH: str = policy_hash(_CURRENT)
