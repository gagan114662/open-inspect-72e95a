#!/usr/bin/env python3
"""Turn archived review rounds into an eval set.

Every archived round is a real change plus an independent verdict on it:
the commit Codex reviewed and the findings it reported. That is an eval
case: given this diff, does a reviewer (or a pre-push check, or a future
version of the agent with new skills) surface the same classes of problem?

Each case under evals/cases/ holds the diff Codex actually reviewed (the
whole branch against the main line it forked from, credentials scrubbed
source-aware), the findings as expected outcomes, and the topic the current
policy files each finding under. Cases whose commit is no longer reachable
are skipped and counted. A case is `scorable` only when its diff is the
reviewed range and still contains every file its findings name; otherwise it
is kept for the record with a `not_scorable_reason` and the runner skips it.

usage: build-evals.py [ARCHIVE] [--policy PATH] [--out-dir DIR] [--max-diff-chars N]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
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


def _first_parent_chain(repo: Path, main_ref: str | None) -> set[str]:
    """Commits on the main line (first-parent history of origin/main or main)."""
    refs = [main_ref] if main_ref else ["origin/main", "main"]
    for ref in refs:
        result = subprocess.run(
            ["git", "rev-list", "--first-parent", ref], cwd=repo, capture_output=True, text=True
        )
        if result.returncode == 0:
            return set(result.stdout.split())
    return set()


def reviewed_base(sha: str, repo: Path = REPO_ROOT, main_ref: str | None = None) -> str | None:
    """The main-line commit the review diffed against (`origin/main...HEAD`
    resolves to the branch's fork point, or the last main commit merged into
    it). A commit that sits on the main line itself has no recoverable base:
    the branch it was reviewed on has been linearised away."""
    chain = _first_parent_chain(repo, main_ref)
    if not chain or sha in chain:
        return None
    result = subprocess.run(
        ["git", "rev-list", "--topo-order", sha], cwd=repo, capture_output=True, text=True
    )
    if result.returncode:
        return None
    for ancestor in result.stdout.split():
        if ancestor in chain:
            return ancestor
    return None


def reviewed_diff(
    sha: str, repo: Path = REPO_ROOT, main_ref: str | None = None
) -> tuple[str | None, str | None] | None:
    """(base, diff) of the range Codex reviewed; (None, None) when the commit
    is reachable but its reviewed base is not; None when it is unreachable.
    Round 009's case held only the archive update of its last commit while
    the review covered the whole PR (Codex review of PR #72, finding 1)."""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True
    ).returncode:
        return None
    base = reviewed_base(sha, repo, main_ref)
    if base is None:
        return (None, None)
    result = subprocess.run(["git", "diff", base, sha], cwd=repo, capture_output=True, text=True)
    return (base, result.stdout) if result.returncode == 0 else None


scrub_source = policy_mod.scrub_source_secrets

# Relative paths only: an absolute runner path (`/home/runner/work/...`) in a
# markdown link is not a file the diff can be checked for, and a match may
# not start in the middle of a hyphenated directory name.
_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|yml|yaml|ts|tsx|js|mjs|json|jsonl|html|md|sh|toml|txt))\b"
)


def referenced_files(findings: list[str]) -> list[str]:
    """Files the findings name, in order of first mention."""
    seen: list[str] = []
    for finding in findings:
        for match in _PATH_RE.findall(finding):
            path = re.sub(r"^(?:\./)+", "", match)
            if path not in seen:
                seen.append(path)
    return seen


def split_patches(diff: str) -> list[tuple[str, str]]:
    """(path, patch) per file, from the `diff --git` headers."""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    patches = []
    for part in parts:
        if not part.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", part)
        patches.append((m.group(2) if m else "", part))
    return patches


def cap_diff(
    diff: str, referenced: list[str], max_diff_chars: int
) -> tuple[str, bool, list[str], str | None]:
    """Keep the diff under the cap without dropping the files the findings
    name: those patches go first, whole; the rest fill what is left. Returns
    (diff, truncated, omitted_files, not_scorable_reason). A case whose
    findings name a file the reviewed diff never touched has no evidence for
    that finding and is not scorable, capped or not; a truncated case whose
    findings name no file, or name one that did not fit, cannot be scored
    either (Codex review of PR #72, finding 2 and round-3 finding 4)."""
    patches = split_patches(diff)
    present = {path for path, _ in patches}
    absent = [f for f in referenced if not any(p == f or p.endswith("/" + f) for p in present)]
    if absent:
        reason = f"findings name files the reviewed diff does not touch: {', '.join(absent)}"
        if len(diff) <= max_diff_chars:
            return diff, False, [], reason
    if len(diff) <= max_diff_chars:
        return diff, False, [], None
    wanted = list(referenced)

    def is_wanted(path: str) -> bool:
        return any(path == f or path.endswith("/" + f) for f in wanted)

    ordered = [pp for pp in patches if is_wanted(pp[0])] + [
        pp for pp in patches if not is_wanted(pp[0])
    ]
    kept: list[str] = []
    omitted: list[str] = []
    used = 0
    for path, patch in ordered:
        if used + len(patch) <= max_diff_chars:
            kept.append(patch)
            used += len(patch)
        else:
            omitted.append(path)
    reason = None
    if absent:
        reason = f"findings name files the reviewed diff does not touch: {', '.join(absent)}"
    elif not wanted:
        reason = "diff truncated and the findings name no file in it"
    else:
        missing = [f for f in wanted if any(is_wanted(o) and o.endswith(f) for o in omitted)]
        if missing:
            reason = f"diff truncated past the files the findings name: {', '.join(missing)}"
    return "".join(kept), True, omitted, reason


def build_case(
    entry: dict,
    base: str | None,
    diff: str | None,
    keywords: dict[str, list[str]],
    max_diff_chars: int,
) -> dict:
    findings = [f for f in entry.get("findings", []) or [] if isinstance(f, str)]
    expected = [
        {
            "finding": policy_mod.scrub_secrets(f).strip(),
            "topic": policy_mod.classify_finding(f, keywords),
        }
        for f in findings
    ]
    referenced = referenced_files(findings)
    answers_removed: list[str] = []
    if diff is None:
        capped, truncated, omitted, reason = "", False, [], "no reviewed base is recoverable"
        diff_chars = 0
    else:
        # Patches to answer-bearing paths (the archive, evals, generated
        # playbooks) never reach the reviewer: a branch that updated the
        # archive would otherwise hand it the expected findings (Codex review
        # of PR #72, round 7, finding 3).
        kept_patches = []
        for path, patch in split_patches(diff):
            if policy_mod.is_answer_path(path):
                answers_removed.append(path)
            else:
                kept_patches.append(patch)
        answers_removed.sort()
        scrubbed = scrub_source("".join(kept_patches))
        capped, truncated, omitted, reason = cap_diff(scrubbed, referenced, max_diff_chars)
        diff_chars = len(scrubbed)
        if reason and answers_removed and referenced:
            only_answers = all(
                any(a == f or a.endswith("/" + f) for a in answers_removed) for f in referenced
            )
            if only_answers:
                reason = (
                    "findings reference only answer-bearing paths removed from the diff: "
                    + ", ".join(answers_removed)
                )
    return {
        "id": f"round-{int(entry['round']):03d}",
        "round": entry["round"],
        "source_sha": entry.get("source_sha"),
        "base_sha": base,
        "target": entry.get("target"),
        "occurred_at": entry.get("occurred_at"),
        "expected": expected,
        "expected_topics": sorted({e["topic"] for e in expected if e["topic"]}),
        "referenced_files": referenced,
        "diff": capped,
        "diff_truncated": truncated,
        "diff_chars": diff_chars,
        "omitted_files": omitted,
        "answer_paths_removed": answers_removed,
        "scorable": reason is None,
        "not_scorable_reason": reason,
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
        reviewed = diff_for(sha)
        if reviewed is None:
            skipped += 1
            continue
        base, diff = reviewed
        cases.append(build_case(entry, base, diff, keywords, max_diff_chars))
    return cases, skipped


_GENERATED_CASE = re.compile(r"^round-\d{3}\.json$")


def write_cases(cases: list[dict], out_dir: Path, inputs: list[str | None]) -> list[str]:
    """Write every case and remove generated case files for rounds no longer
    produced (an unreachable commit, a removed entry), so the runner never
    scores a stale case (Codex review of PR #72, round 3, finding 3). Only
    files named like a generated case are touched; anything else in the
    folder is left alone. Returns the removed file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = set()
    for case in cases:
        target = out_dir / f"{case['id']}.json"
        policy_mod.assert_safe_output(target, inputs=inputs)
        target.write_text(json.dumps(case, indent=2) + "\n")
        produced.add(target.name)
    removed = []
    for stale in sorted(out_dir.iterdir()):
        if stale.name in produced or not _GENERATED_CASE.match(stale.name):
            continue
        if stale.is_symlink() or not stale.is_file():
            continue
        policy_mod.assert_safe_output(stale, inputs=inputs)
        stale.unlink()
        removed.append(stale.name)
    return removed


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
        entries, policy_mod.topic_keywords(policy), reviewed_diff, args.max_diff_chars
    )
    out_dir = Path(args.out_dir)
    removed = write_cases(cases, out_dir, inputs=[args.archive_path, args.policy])
    print(
        json.dumps(
            {
                "cases": len(cases),
                "scorable": sum(1 for c in cases if c["scorable"]),
                "skipped": skipped,
                "removed_stale": removed,
                "policy_version": policy["version"],
                "out_dir": policy_mod.relative_to_repo(out_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
