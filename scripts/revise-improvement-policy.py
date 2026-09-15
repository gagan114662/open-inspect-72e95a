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
# Field blind spots: failures mined from working sessions that no topic
# claims. Enough of them is a trigger of its own, so the taxonomy can learn
# from what actually broke and not only from what reviews wrote up.
MIN_FIELD_BLIND_SPOTS = 5
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
        "traceback",
        "recent",
        "call",
        "last",
        "line",
        "module",
        "string",
        "stdin",
        "exit",
        "passed",
        "tool",
        "input",
        "json",
        "true",
        "false",
        "none",
        "required",
        "found",
        "such",
        "directory",
        "usage",
        "error",
        "errors",
        "warning",
        "please",
        "reason",
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
# Paths, URLs and file references name WHERE a finding is, never what class
# of problem it is: "/home/runner/work/<repo>/scripts/x.py:41" must not
# hand mining the tokens home, runner, scripts or the repository's own name
# (the loop's first autonomous proposal, PR #54, did exactly that).
_LOCATION_EXTENSIONS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".yml",
    ".yaml",
    ".json",
    ".jsonl",
    ".md",
    ".sh",
    ".toml",
)


_LOCATION_ROOTS = frozenset(
    {
        "scripts",
        "docs",
        "packages",
        "terraform",
        ".github",
        "tools",
        "skills",
        "channels",
        "schedules",
        "agents",
        "home",
        "tmp",
        "var",
        "usr",
        "etc",
        "opt",
        "users",
        "runner",
        "work",
        "node_modules",
        "src",
        "lib",
        "test",
        "tests",
        "dist",
        "build",
    }
)
_LINE_REF_RE = re.compile(r":\d+(?:-\d+)?$")


def is_location(word: str) -> bool:
    """A URL, an absolute or explicitly relative path, a file reference
    (x.py, x.py:41, x.py:41-43, with surrounding punctuation), or a path
    whose first segment is a known directory. Slash-separated prose such as
    deadlock/livelock/starvation is not a location. Plain string tests per
    whitespace-separated word, linear on long inputs (Codex review of PR
    #56, rounds 2 and 3)."""
    w = word.strip("()[]<>`'\",;.:!?*_").lower()
    w = _LINE_REF_RE.sub("", w)
    if not w:
        return False
    if "http://" in w or "https://" in w:
        return True  # a URL glued to other characters is still a URL
    if w.startswith(("/", "./", "../", "~/")):
        return True
    segments = w.split("/")
    if any(seg.endswith(_LOCATION_EXTENSIONS) for seg in segments):
        return True
    return len(segments) > 1 and segments[0] in _LOCATION_ROOTS


# A token present in more than this share of ALL archived findings is the
# repository's background vocabulary ("policy", "evidence", "scripts"), not
# a class of problem, and may not name or define a mined topic.
MAX_BACKGROUND_SHARE = 0.2
# ...and only once a token has been seen in at least this many findings: a
# word in 3 of 10 findings is a recurring problem, not background.
MIN_BACKGROUND_OCCURRENCES = 10


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def tokenize(text: str) -> set[str]:
    # A markdown link keeps its text and drops its target: the target is a
    # location by definition (Codex review of PR #56, round 4).
    text = _MARKDOWN_LINK_RE.sub(r"\1 ", text)
    text = " ".join(w for w in text.lower().split() if not is_location(w))
    return {
        tok.strip("-_")
        for tok in _TOKEN_RE.findall(text)
        if len(tok) >= MIN_TOKEN_LENGTH
        and tok not in STOPWORDS
        and tok != "redacted"  # the scrubber's marker is never a topic
        and not tok.startswith("http")
        and not tok.endswith("ly")  # adverbs name manner, never a class of problem
    }


def existing_keyword_tokens(keywords: dict[str, list[str]]) -> set[str]:
    return {k.lower() for words in keywords.values() for k in words}


# Words a field failure may contribute as policy keywords even when no
# archived (public) review finding contains them: the vocabulary of failure
# itself. Anything else a trace says — a passphrase, a hostname, a project
# name — is never published as a keyword (Codex review of PR #10, round 49).
SAFE_FIELD_VOCABULARY: frozenset[str] = frozenset(
    [
        "action",
        "address",
        "approval",
        "argument",
        "arguments",
        "assertion",
        "assertionerror",
        "attributeerror",
        "auth",
        "authentication",
        "bind",
        "blocked",
        "branch",
        "build",
        "busy",
        "cache",
        "certificate",
        "checkout",
        "classifier",
        "clone",
        "cloudflare",
        "commit",
        "compile",
        "compiler",
        "conflict",
        "connect",
        "connection",
        "constraint",
        "container",
        "corrupt",
        "crash",
        "crashed",
        "credential",
        "credentials",
        "database",
        "deadlock",
        "decode",
        "denied",
        "dependencies",
        "dependency",
        "detached",
        "disk",
        "dns",
        "docker",
        "encoding",
        "error",
        "errors",
        "escape",
        "exception",
        "expired",
        "failed",
        "failing",
        "failure",
        "fetch",
        "flag",
        "forbidden",
        "found",
        "github",
        "handshake",
        "image",
        "import",
        "importerror",
        "indexerror",
        "install",
        "interactive",
        "inuse",
        "invalid",
        "json",
        "keyerror",
        "killed",
        "limit",
        "limited",
        "lint",
        "lock",
        "locked",
        "malformed",
        "memory",
        "merge",
        "migration",
        "mismatch",
        "missing",
        "modal",
        "module",
        "mount",
        "network",
        "notfound",
        "oom",
        "option",
        "origin",
        "overflow",
        "package",
        "panic",
        "parse",
        "parser",
        "password",
        "permission",
        "permissions",
        "policy",
        "port",
        "prompt",
        "proxy",
        "pull",
        "push",
        "pytest",
        "query",
        "quota",
        "quote",
        "quoting",
        "rate",
        "readonly",
        "rebase",
        "recursion",
        "refused",
        "registry",
        "rejected",
        "remote",
        "reset",
        "retries",
        "retry",
        "rollback",
        "ruff",
        "runner",
        "sandbox",
        "schema",
        "secret",
        "segfault",
        "socket",
        "space",
        "sqlite",
        "ssl",
        "stack",
        "stale",
        "stdin",
        "syntax",
        "syntaxerror",
        "terraform",
        "throttle",
        "throttled",
        "timed",
        "timeout",
        "tls",
        "token",
        "toml",
        "traceback",
        "transaction",
        "tty",
        "typecheck",
        "typeerror",
        "unauthenticated",
        "unauthorized",
        "unavailable",
        "unicode",
        "unique",
        "unreachable",
        "validate",
        "validation",
        "valueerror",
        "vercel",
        "version",
        "volume",
        "worker",
        "workflow",
        "yaml",
    ]
)


def background_tokens(
    entries: list[dict], keywords: dict[str, list[str]] | None = None
) -> set[str]:
    """Tokens that pervade the findings the taxonomy ALREADY classifies: what
    this repository talks about across every class of problem ("scripts",
    "policy", "evidence"). A word that recurs only in unclassified findings
    ("deadlock", ten times) is a defect waiting for a topic, not background,
    so it is never excluded however often it appears (Codex review of PR
    #56, round 2)."""
    findings = [
        f
        for e in entries
        for f in e.get("findings", [])
        if isinstance(f, str)
        and (not keywords or policy_mod.classify_finding(f, keywords) is not None)
    ]
    if not findings:
        return set()
    df: Counter[str] = Counter(tok for f in findings for tok in tokenize(f))
    return {
        tok
        for tok, n in df.items()
        if n >= MIN_BACKGROUND_OCCURRENCES and n / len(findings) > MAX_BACKGROUND_SHARE
    }


def field_vocabulary(entries: list[dict]) -> set[str]:
    """Words that may be published from field failures: the safe vocabulary
    plus every token already public in an archived review finding."""
    public = {
        tok
        for e in entries
        for f in e.get("findings", [])
        if isinstance(f, str)
        for tok in tokenize(f)
    }
    return set(SAFE_FIELD_VOCABULARY) | public


def mine_topics(
    unclassified: list[dict],
    keywords: dict[str, list[str]],
    vocabulary: set[str] | None = None,
    background: set[str] | None = None,
) -> list[dict]:
    """Greedy, auditable topic mining over findings the policy could not
    classify: the most frequent significant token names a topic; its
    keywords are that token plus the tokens that co-occur with it most;
    findings the new topic covers are removed and the process repeats.
    Field items (round `field:…`) contribute only tokens in `vocabulary`;
    with no vocabulary they contribute nothing (round 49)."""
    taken = existing_keyword_tokens(keywords) | set(background or ())

    def allowed(item: dict) -> set[str]:
        toks = tokenize(item["finding"]) - taken
        if str(item["round"]).startswith("field:"):
            return toks & (vocabulary or set())
        return toks

    remaining = [
        {"round": item["round"], "finding": item["finding"], "tokens": allowed(item)}
        for item in unclassified
    ]
    mined: list[dict] = []
    while len(mined) < MAX_NEW_TOPICS and remaining:
        df = Counter(tok for item in remaining for tok in item["tokens"])
        candidates = [t for t, n in df.items() if n >= MIN_FINDINGS_PER_TOPIC]
        if not candidates:
            break
        head = sorted(candidates, key=lambda t: (-df[t], t))[0]
        covering_head = [item for item in remaining if head in item["tokens"]]
        co = Counter(tok for item in covering_head for tok in item["tokens"] if tok != head)
        companions = [
            t
            for t, n in sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))
            if n >= MIN_FINDINGS_PER_TOPIC
        ][: MAX_KEYWORDS_PER_TOPIC - 1]
        topic_keywords = [head, *companions]
        # A topic claims exactly the findings it would classify: any keyword,
        # by the same substring rule the detector uses. Those findings are
        # then unavailable to later topics, so no finding supports two
        # topics (Codex review of PR #10, round 2, finding 3).
        covering = [
            item
            for item in remaining
            if policy_mod.classify_finding(item["finding"], {"_": topic_keywords}) is not None
        ]
        if len(covering) < MIN_FINDINGS_PER_TOPIC:
            break
        base = "-".join(topic_keywords[:2]) if companions else head
        taken_names = set(keywords) | {m["name"] for m in mined}
        name = base
        suffix = 1
        while name in taken_names:
            # Never reuse a name: an overwritten topic would silently drop
            # its classifications (Codex review of PR #10, round 14).
            suffix += 1
            name = f"{base}-{suffix}"
        mined.append(
            {
                "name": name,
                "keywords": topic_keywords,
                # Field failures never enter the policy as text: only the
                # keywords that matched, so no regex list decides what a
                # proposal PR publishes (Codex review of PR #10, round 48).
                # Archive findings are Codex review text that is already
                # public on the PR, kept for the dashboard.
                "evidence": [
                    (
                        {
                            "round": item["round"],
                            "matched": [
                                kw for kw in topic_keywords if kw in item["finding"].lower()
                            ],
                        }
                        if str(item["round"]).startswith("field:")
                        else {
                            "round": item["round"],
                            "finding": policy_mod.scrub_secrets(item["finding"])[:160],
                        }
                    )
                    for item in covering
                ],
            }
        )
        taken.update(topic_keywords)
        covered_ids = {id(item) for item in covering}
        remaining = [item for item in remaining if id(item) not in covered_ids]
        for item in remaining:
            item["tokens"] -= set(topic_keywords)
    return mined


def validity_under(policy: dict, entries: list[dict], anchor: dict | None) -> float | None:
    """Validity a policy would score on the same findings and the same
    anchor counts the measurement carried. Topics without a known anchor
    count are excluded, exactly as the measurement excludes them."""
    if anchor is None:
        return None
    current = measure_mod.measure(entries, policy, None)["current"]
    known = [t for t in policy["topics"] if anchor.get(t) is not None]
    return measure_mod.spearman(
        [float(current["dev_weighted"][t]) for t in known], [float(anchor[t]) for t in known]
    )


def entries_covered_by_evidence(entries: list[dict], measurement: dict) -> list[dict]:
    """Archive entries from rounds no later than the evidence snapshot's
    collection time. Without a collection time, all entries."""
    collected_ms = measure_mod.parse_timestamp_ms(
        (measurement.get("anchor") or {}).get("collected_at")
    )
    if collected_ms is None:
        return entries
    # By round identity, not number: two reviews sharing a number can
    # straddle the snapshot time (Codex review of PR #10, round 37).
    covered = {
        r["key"]
        for r in measure_mod.rounds_in_order(entries)
        if r["timestamp_ms"] is not None and r["timestamp_ms"] <= collected_ms
    }
    return [e for e in entries if policy_mod.round_key(e) in covered]


def validity_regressed(before: float | None, after: float | None) -> bool:
    """A candidate may not lower a defined validity, and may not turn a
    defined validity into an undefined one (a constant weighted signal has
    no rank agreement to measure, which would silence every later trigger —
    Codex review of PR #10, round 3, finding 1)."""
    if before is None:
        return False
    return after is None or after < before


def weight_repair(
    policy: dict,
    current: dict,
    entries: list[dict],
    *,
    discount: bool,
) -> tuple[dict, list[str]]:
    """Propose weight changes against the measured anchor: restore credit to
    topics the field now corroborates (always considered, so a discounted
    topic is not stranded below the threshold once evidence arrives — Codex
    review of PR #10, round 3, finding 2), and discount topics the field
    never shows (only when the validity trigger fired). The whole proposal
    is kept only if it does not regress validity on the same anchor."""
    anchor = candidate_anchor(current, policy)
    weights = policy_mod.topic_weights(policy)
    if anchor is None:
        return policy["topics"], []
    topics = dict(policy["topics"])
    changes: list[str] = []
    if discount and current.get("rounds_after_evidence", 0) > 0:
        changes.append(
            f"skipped discounts: {current['rounds_after_evidence']} round(s) are newer than the evidence snapshot; refresh it first"
        )
        discount = False
    if discount:
        for topic in current.get("dev_only_topics", []):
            old_w = weights[topic]
            new_w = max(MIN_WEIGHT, round(old_w * WEIGHT_DISCOUNT, 3))
            if new_w < old_w:
                topics[topic] = {**topics[topic], "weight": new_w}
                changes.append(
                    f"discounted {topic} weight {old_w} -> {new_w}: credited in {current['dev'][topic]} round(s), 0 field traces"
                )
    for topic, count in anchor.items():
        if count is not None and count > 0 and weights.get(topic, 1.0) < 1.0:
            restored = min(1.0, round(weights[topic] / WEIGHT_DISCOUNT, 3))
            topics[topic] = {**topics[topic], "weight": restored}
            changes.append(
                f"restored {topic} weight {weights[topic]} -> {restored}: {count} field trace(s)"
            )
    if not changes:
        return policy["topics"], []
    candidate = {**policy, "topics": topics}
    before_v = validity_under(policy, entries, anchor)
    after_v = validity_under(candidate, entries, candidate_anchor(current, candidate))
    if validity_regressed(before_v, after_v):
        return policy["topics"], [
            f"kept weights unchanged: proposed reweighting would move validity {before_v} -> {after_v}"
        ]
    return topics, changes


def candidate_anchor(current: dict, policy: dict) -> dict | None:
    """Anchor counts for judging a specific policy: every topic the evidence
    searched, with each of this policy's topics resolved to the evidence
    searched under exactly its own keyword definition (plain name or a
    `name@tag` variant kept for an older definition). A topic whose
    definition was never searched is unknown, never borrowed from another
    definition of the same name (Codex review of PR #10, rounds 9, 12, 18,
    32 and 33)."""
    anchor = current.get("anchor")
    if anchor is None:
        return None
    evidence = current.get("anchor_evidence")
    definitions = current.get("anchor_definitions") or {}
    keywords = policy_mod.topic_keywords(policy)
    if evidence is None:
        # A measurement without evidence-wide counts (older format): only
        # the measured topics are known, and only where definitions match.
        return {
            topic: (
                count
                if topic not in keywords
                or list(definitions.get(topic, [])) == list(keywords[topic])
                else None
            )
            for topic, count in anchor.items()
        }
    result: dict[str, int | None] = {}
    for topic, count in evidence.items():
        if "@" in topic or topic in keywords:
            continue
        result[topic] = count  # a topic this policy does not define keeps its evidence
    searched_names = {key.split("@")[0] for key in evidence}
    for topic, words in keywords.items():
        if topic not in searched_names:
            continue  # never searched under any definition: not part of the anchor
        key = measure_mod.resolve_evidence_key(definitions, topic, list(words))
        result[topic] = evidence.get(key) if key is not None else None
    return result


def rolled_back_hashes(history: list[dict]) -> set[str]:
    """Configurations a rollback has already rejected. They are never
    rollback targets themselves: after v2 is rolled back for validity, the
    restored v1 would otherwise be judged worse than v2 on coverage two
    rounds later and 'rolled back' to v2, and so on forever."""
    return {
        e["replaced_policy_hash"]
        for e in history
        if e.get("origin") == "rollback" and isinstance(e.get("replaced_policy_hash"), str)
    }


def rejected_configuration(candidate: dict, history: list[dict], measurement: dict) -> dict | None:
    """A configuration rolled back on the same archive and evidence is not
    retried: the rollback's history entry records the rejected hash and
    what it was judged on."""
    wanted = policy_mod.policy_hash(candidate)
    anchor = measurement.get("anchor") or {}
    for entry in history:
        if entry.get("origin") != "rollback" or entry.get("replaced_policy_hash") != wanted:
            continue
        if entry.get("archive_digest") != measurement.get("archive_digest"):
            continue
        # Same evidence means the same observations, not the same clock: an
        # hourly refresh that re-collects an unchanged field must not lift
        # the block (Codex verification pass, finding 1). Entries written
        # before digests existed fall back to the collection time.
        if "evidence_digest" in entry:
            if entry.get("evidence_digest") == anchor.get("evidence_digest"):
                return entry
        elif entry.get("evidence_collected_at") == anchor.get("collected_at"):
            return entry
    return None


def unjudged_ancestors(policy: dict, history: list[dict]) -> list[dict]:
    """Snapshots this policy descends from, nearest first, following a
    rollback through to the ancestry of the configuration it restored, up to
    and including the first non-revision ancestor. Later evidence must be
    able to expose a harmful ancestor that a newer revision or a rollback
    was layered on before validity could be measured (Codex review of
    PR #10, rounds 14 and 17)."""
    chain: list[dict] = []
    seen: set[int] = set()
    version = judged_from(policy, history)
    while isinstance(version, int) and version not in seen:
        seen.add(version)
        snapshot = snapshot_for_version(version, history)
        if snapshot is None:
            break
        chain.append(snapshot)
        if snapshot.get("origin") == "rollback":
            version = judged_from(snapshot, history)
            continue
        if snapshot.get("origin") != "revision":
            break
        version = snapshot.get("parent")
    return chain


def judged_from(policy: dict, history: list[dict]) -> int | None:
    """Version whose ancestry a policy continues: the parent for a revision;
    for a rollback, the parent of the restored version."""
    if policy.get("origin") == "rollback":
        restored = policy.get("restored_version")
        snapshot = snapshot_for_version(restored, history) if isinstance(restored, int) else None
        if snapshot is None:
            return None
        return snapshot.get("parent")
    return policy.get("parent")


def entries_under(entries: list[dict], policy: dict) -> list[dict]:
    """Archive entries from rounds stamped with this policy's version and
    hash: the only rounds on which the policy can be judged (Codex review
    of PR #10, round 25: older rounds decided by an ancestor must not enter
    the comparison, or they can drown out the revision's own signal)."""
    wanted = policy_mod.policy_hash(policy)
    version = policy["version"]
    return [
        e for e in entries if e.get("policy_hash") == wanted and e.get("policy_version") == version
    ]


def rounds_under(entries: list[dict], policy: dict) -> int:
    """Rounds decided under this exact policy: archive-round.py stamps each
    round with the policy hash in force when it was archived. Rounds from
    before a revision was merged never count toward judging it, however
    long its pull request sat open (Codex review of PR #10, round 4)."""
    wanted = policy_mod.policy_hash(policy)
    version = policy["version"]
    # Version AND hash: a later revision that recreates an earlier
    # configuration must not inherit that configuration's old rounds
    # (Codex review of PR #10, round 7).
    return len(
        {
            measure_mod.round_key(e)
            for e in entries
            if e.get("policy_hash") == wanted
            and e.get("policy_version") == version
            and isinstance(e.get("round"), int)
        }
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


def field_blind_spots(field_failures: dict | None, keywords: dict[str, list[str]]) -> list[dict]:
    """Unclassified failures from mine-trace-failures.py, shaped like archive
    findings so the same mining applies. Deduplicated by excerpt."""
    if not field_failures:
        return []
    seen: set[str] = set()
    items: list[dict] = []
    for failure in field_failures.get("blind_spots", []):
        # The output, not the command: command text is full of paths and
        # repository names that would name topics after folders. Classify
        # the excerpt alone: a kind label such as "tool-error" would match
        # keywords like "-e" (Codex review of PR #10, round 32).
        # Mining sees the excerpt alone, too: a synthetic label shared by every
        # failure would otherwise be the most frequent token and name a topic
        # that classifies no real failure text (round 34). The kind travels
        # as metadata.
        excerpt = policy_mod.scrub_secrets(str(failure.get("excerpt", "")).strip())
        if not excerpt or excerpt in seen:
            continue
        if policy_mod.classify_finding(excerpt, keywords) is not None:
            continue
        seen.add(excerpt)
        items.append(
            {
                "round": f"field:{str(failure.get('trace_id', ''))[:8]}",
                "finding": excerpt,
                "kind": str(failure.get("kind", "")),
            }
        )
    return items


def decide(
    entries: list[dict],
    policy: dict,
    history: list[dict],
    measurement: dict,
    now: str,
    field_failures: dict | None = None,
) -> dict:
    """Pure decision: returns {"action": "none"|"revise"|"rollback", ...}
    without touching disk, so it can be tested and dry-run."""
    current = measurement["current"]
    coverage = current.get("coverage")
    validity = current.get("validity")
    keywords = policy_mod.topic_keywords(policy)

    # 1. Safe inheritance: a revision that made things worse gets rolled back
    #    before any new revision is layered on top of it. Both policies are
    #    re-measured on the SAME findings: comparing today's coverage with
    #    the parent's historical number would punish a revision merely for
    #    being alive when unfamiliar findings arrived (Codex review of
    #    PR #10, finding 1).
    base_version = judged_from(policy, history)
    if policy.get("origin") in {"revision", "rollback"} and base_version is not None:
        adopted = adoption_entry(policy["version"], history)
        if adopted is not None and rounds_under(entries, policy) >= MIN_ROUNDS_TO_JUDGE:
            parent = snapshot_for_version(base_version, history)
            if parent is not None and coverage is not None:
                # Validity is judged only on rounds the evidence snapshot could
                # have seen; rounds archived after collection would make an
                # unchanged field look like a regression (Codex review of
                # PR #10, round 7).
                covered = entries_covered_by_evidence(entries_under(entries, policy), measurement)
                # Coverage on the revision's own rounds, kept apart from the
                # full-archive `coverage` that the trigger and candidate
                # acceptance use (Codex review of PR #10, round 26).
                own_rounds = entries_under(entries, policy)
                parent_now = measure_mod.measure(own_rounds, parent, None)["current"]["coverage"]
                own_coverage = measure_mod.measure(own_rounds, policy, None)["current"]["coverage"]
                child_validity = validity_under(policy, covered, candidate_anchor(current, policy))
                rejected = rolled_back_hashes(history)
                worse_coverage = (
                    parent_now is not None
                    and own_coverage is not None
                    and own_coverage < parent_now
                    and policy_mod.policy_hash(parent) not in rejected
                )
                # Compare against every ancestor in the unjudged chain, not
                # only the parent: the best-scoring ancestor is the rollback
                # target when the current policy is worse than any of them.
                best: dict | None = None
                best_validity: float | None = None
                # Coverage is compared with every ancestor too: a topic a
                # grandparent recognised and the parent dropped is invisible
                # in a parent-only comparison (Codex full-branch review,
                # finding 5).
                best_cov: dict | None = None
                best_cov_value: float | None = None
                for ancestor in unjudged_ancestors(policy, history):
                    if policy_mod.policy_hash(ancestor) in rejected:
                        continue
                    v = validity_under(ancestor, covered, candidate_anchor(current, ancestor))
                    if v is not None and (best_validity is None or v > best_validity):
                        best, best_validity = ancestor, v
                    c = measure_mod.measure(own_rounds, ancestor, None)["current"]["coverage"]
                    if c is not None and (best_cov_value is None or c > best_cov_value):
                        best_cov, best_cov_value = ancestor, c
                if (
                    best_cov_value is not None
                    and own_coverage is not None
                    and best_cov_value > own_coverage
                ):
                    worse_coverage = True
                    parent, parent_now = best_cov, best_cov_value
                worse_validity = best_validity is not None and (
                    child_validity is None or child_validity < best_validity
                )
                if worse_coverage or worse_validity:
                    target = parent if worse_coverage else best
                    assert target is not None
                    # Record what the restored policy actually scores, not the
                    # current one's number (Codex review of PR #10, round 15).
                    target_coverage = measure_mod.measure(entries, target, None)["current"][
                        "coverage"
                    ]
                    what = (
                        f"coverage {own_coverage} vs {parent_now}"
                        if worse_coverage
                        else f"validity {child_validity} vs {best_validity}"
                    )
                    return {
                        "action": "rollback",
                        "reason": (
                            f"on the same {current['findings_total']} findings and anchor, "
                            f"v{policy['version']} scores {what} against v{target['version']}"
                        ),
                        "policy": policy_mod.new_version(
                            policy,
                            topics=target["topics"],
                            threshold=target["threshold"],
                            origin="rollback",
                            rationale=f"Rollback to v{target['version']}: v{policy['version']} scored worse ({what}).",
                            created_at=now,
                            restored_version=target["version"],
                        ),
                        "coverage_before": own_coverage,
                        "coverage_after": target_coverage,
                        "validity_before": child_validity,
                        "validity_after": best_validity if not worse_coverage else None,
                        "changes": [
                            f"restored taxonomy, weights and threshold of v{target['version']}"
                        ],
                    }

    # A revision that has not yet been judged must not be built on: a
    # successor would only ever be compared with it, so a regression it
    # introduced against ITS parent could never be rolled back (Codex review
    # of PR #10, round 6). Wait until MIN_ROUNDS_TO_JUDGE rounds have run
    # under it; the rollback check above already covered the judged case.
    # The wait does not depend on having an ancestor to compare with: a
    # rollback to the root version has none, and skipping the wait there let
    # a rejected configuration be re-proposed after one round (round 36).
    if policy.get("origin") in {"revision", "rollback"}:
        under = rounds_under(entries, policy)
        if under < MIN_ROUNDS_TO_JUDGE:
            return {
                "action": "none",
                "reason": (
                    f"v{policy['version']} has run under {under} round(s); waiting for "
                    f"{MIN_ROUNDS_TO_JUDGE} before judging it or layering another revision"
                ),
            }

    triggers = []
    if coverage is not None and coverage < MIN_COVERAGE:
        triggers.append(f"coverage {coverage} < {MIN_COVERAGE}")
    if validity is not None and validity < MIN_VALIDITY:
        triggers.append(f"validity {validity} < {MIN_VALIDITY}")
    blind = field_blind_spots(field_failures, keywords)
    coverage_trigger = coverage is not None and coverage < MIN_COVERAGE
    field_trigger = len(blind) >= MIN_FIELD_BLIND_SPOTS
    if field_trigger:
        triggers.append(f"field: {len(blind)} failures match no topic")

    changes: list[str] = []
    new_topics = dict(policy["topics"])

    # 2. Coverage repair: mine the blind spots.
    # Explicit booleans, not string prefixes, decide what gets mined
    # (Codex review of PR #10, round 28).
    mining_input = list(current.get("unclassified_findings", []))
    if field_trigger or coverage_trigger:
        mining_input.extend(blind)
    mined = (
        mine_topics(
            mining_input, keywords, field_vocabulary(entries), background_tokens(entries, keywords)
        )
        if (coverage_trigger or field_trigger)
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

    # 3. Weight repair: restorations are always evaluated; discounts only
    #    when validity actually failed.
    # Every validity comparison in this decision uses the rounds the evidence
    # snapshot covers, exactly as the rollback check does, so a candidate
    # cannot pass on later findings and then be rolled back on the snapshot
    # (Codex review of PR #10, round 8).
    covered = entries_covered_by_evidence(entries, measurement)
    weighted_topics, weight_changes = weight_repair(
        {**policy, "topics": new_topics},
        current,
        covered,
        discount=any(t.startswith("validity") for t in triggers),
    )
    new_topics = weighted_topics
    changes.extend(weight_changes)

    if not triggers and not any(c.startswith("restored") for c in changes):
        return {
            "action": "none",
            "reason": "policy signal still predicts the field within thresholds",
        }
    if not triggers:
        triggers.append("field evidence corroborates a discounted topic")

    if not changes or all(
        c.startswith("kept weights unchanged") or c.startswith("skipped discounts") for c in changes
    ):
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
    # The whole candidate, not just its weight changes, must not regress
    # validity against the policy it replaces (Codex review of PR #10, round 4).
    v_before = validity_under(policy, covered, candidate_anchor(current, policy))
    v_after = validity_under(revised, covered, candidate_anchor(current, revised))
    rejected = rejected_configuration(revised, history, measurement)
    if rejected is not None:
        return {
            "action": "none",
            "reason": (
                f"candidate reproduces configuration {policy_mod.policy_hash(revised)}, rolled back as "
                f"v{rejected.get('replaced_version', '?')} on the same archive and evidence; needs new evidence"
            ),
            "triggers": triggers,
            "rejected_changes": changes,
        }
    if validity_regressed(v_before, v_after):
        return {
            "action": "none",
            "reason": f"candidate revision would move validity {v_before} -> {v_after}; refused",
            "triggers": triggers,
            "rejected_changes": changes,
        }
    after = measure_mod.measure(entries, revised, None)["current"]
    if coverage is not None and after["coverage"] is not None and after["coverage"] < coverage:
        return {
            "action": "none",
            "reason": f"candidate revision would lower coverage {coverage} -> {after['coverage']}; refused",
            "triggers": triggers,
            "rejected_changes": changes,
        }
    return {
        "action": "revise",
        "reason": "; ".join(triggers),
        "triggers": triggers,
        "policy": revised,
        "changes": changes,
        "coverage_before": coverage,
        "coverage_after": after["coverage"],
        "validity_before": validity,
        # Same anchor counts, candidate weights; newly mined topics are
        # unknown to the anchor until evidence is re-collected.
        "validity_after": v_after,
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
        "replaced_version": policy["version"],
        "archive_digest": measurement.get("archive_digest"),
        "evidence_collected_at": (measurement.get("anchor") or {}).get("collected_at"),
        "evidence_digest": (measurement.get("anchor") or {}).get("evidence_digest"),
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
    parser.add_argument(
        "--out-json", default=None, help="Also write the decision JSON to this path"
    )
    parser.add_argument(
        "--field-failures",
        default=None,
        help="JSON written by mine-trace-failures.py --out-json; its blind spots feed topic mining",
    )
    args = parser.parse_args(argv[1:])

    if args.out_json:
        policy_mod.assert_safe_output(
            args.out_json,
            inputs=[
                args.archive_path,
                args.measurement,
                args.policy,
                args.history,
                args.field_failures,
            ],
        )
    entries = measure_mod.load_archive(args.archive_path)
    policy = policy_mod.load_policy(args.policy)
    history = policy_mod.load_history(args.history)
    try:
        policy_mod.assert_policy_matches_history(policy, history)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    with open(args.measurement) as f:
        measurement = json.load(f)
    if measurement.get("policy_hash") != policy_mod.policy_hash(policy):
        print(
            f"::error::measurement was taken under policy hash {measurement.get('policy_hash')}, "
            f"but {args.policy} hashes to {policy_mod.policy_hash(policy)}; re-measure first",
            file=sys.stderr,
        )
        return 1
    digest = measure_mod.archive_digest(entries)
    if measurement.get("archive_digest") != digest:
        print(
            f"::error::measurement was taken against archive digest {measurement.get('archive_digest')}, "
            f"but {args.archive_path} now digests to {digest}; re-measure first",
            file=sys.stderr,
        )
        return 1
    now = args.now or policy_mod.utc_now_iso()

    field_failures = None
    if args.field_failures:
        with open(args.field_failures) as f:
            field_failures = json.load(f)
    decision = decide(entries, policy, history, measurement, now, field_failures)
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
            # Both destinations are checked before either is written, so a
            # refused history path cannot leave a policy in force without its
            # record (Codex review of PR #10, round 9).
            # Role-specific destinations: the policy goes only to the policy
            # component and the record only to the history component, so
            # swapped or duplicated arguments are refused before any write
            # (Codex review of PR #10, rounds 20 and 35).
            policy_mod.assert_ai_may_write(out_policy, role="policy")
            policy_mod.assert_ai_may_write(args.history, role="history")
            if Path(out_policy).resolve() == Path(args.history).resolve():
                raise PermissionError("--out-policy and --history must be different files")
            policy_mod.append_history(
                history_entry(decision, policy, measurement, now), args.history
            )
            policy_mod.save_policy(decision["policy"], out_policy)
            print(
                f"  wrote {policy_mod.relative_to_repo(out_policy)} and {policy_mod.relative_to_repo(args.history)}"
            )

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(dict(decision), indent=2, default=str) + "\n")
    print("---")
    print(json.dumps(dict(decision), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
