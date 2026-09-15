#!/usr/bin/env python3
"""Run the eval set: does a reviewer surface what the archive says was there?

Two graders:

  keywords   deterministic baseline. For each expected topic, does the diff
             itself contain any of that topic's policy keywords? That is the
             share of archived problems a pre-push keyword check could have
             caught before review. No model involved.
  codex      the real thing. Runs `codex exec` on the diff with a review
             prompt and asks for findings labelled with the policy's topics;
             recall is the share of expected topics the reviewer named.
             Needs the codex CLI and its login; use --limit to sample.

Scores are per case (recall of expected topics) and aggregated. Results go
to evals/results/<grader>-<stamp>.json so runs under different skills,
prompts or models can be compared.

usage: run-evals.py [--cases DIR] [--grader keywords|codex] [--limit N]
                    [--case ID ...] [--policy PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = REPO_ROOT / "evals" / "cases"
DEFAULT_OUT = REPO_ROOT / "evals" / "results"


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


def load_cases_from(
    cases: list[dict], only: list[str] | None = None, limit: int | None = None
) -> list[dict]:
    """Cases the runner scores: with expected topics, and scorable (built
    from the reviewed range and still holding the files the findings name;
    Codex review of PR #72, findings 1 and 2)."""
    if only:
        wanted = set(only)
        cases = [c for c in cases if c["id"] in wanted]
    cases = [c for c in cases if c["expected_topics"] and c.get("scorable", False)]
    return cases[:limit] if limit else cases


def load_cases(cases_dir: Path, only: list[str] | None, limit: int | None) -> list[dict]:
    cases = [json.loads(p.read_text()) for p in sorted(cases_dir.glob("round-*.json"))]
    return load_cases_from(cases, only, limit)


def grade_keywords(case: dict, keywords: dict[str, list[str]]) -> dict:
    text = case["diff"].lower()
    found = {
        t for t in case["expected_topics"] if any(w.lower() in text for w in keywords.get(t, []))
    }
    return score(case, found, detail={"method": "policy keywords present in the diff"})


REVIEW_PROMPT = """You are an independent code reviewer. Review the unified diff below for correctness and security problems.
Classify each finding under exactly one of these topics, using the topic name verbatim:
{topics}
Respond with JSON only: {{"findings": [{{"topic": "<topic name or other>", "summary": "<one sentence>"}}]}}. No prose outside the JSON.

DIFF:
{diff}
"""


class ReviewerRun(NamedTuple):
    """What the reviewer process produced. A non-zero exit or no output is
    an execution failure, not a review with nothing to say (Codex review of
    PR #72, finding 4)."""

    text: str
    returncode: int
    stderr: str = ""


# Paths whose contents ARE the answers (expected findings, the archive the
# cases were built from, generated playbooks and proposals). They never sit
# in the reviewer's working directory (Codex review of PR #72, round 5).
ANSWER_PATHS: tuple[str, ...] = policy_mod.EVAL_ANSWER_PATHS


class CheckoutUnavailable(RuntimeError):
    """The case's commit cannot be materialised from this repository."""


@contextmanager
def historical_checkout(sha: str) -> Iterator[tuple[Path, list[str]]]:
    """A temporary directory holding ONLY the tree of `sha` minus every
    answer-bearing path, plus a shallow listing of what it holds. The
    reviewer works there, never in the repository root, so it cannot read
    the eval cases or the archive it is being scored against (Codex review
    of PR #72, round 5, finding 3)."""
    root = Path(tempfile.mkdtemp(prefix="eval-checkout-"))
    try:
        archived = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", sha],
            capture_output=True,
            timeout=120,
        )
        if archived.returncode != 0:
            raise CheckoutUnavailable(f"commit {sha[:12]} is not available in this repository")
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as tar:
            tar.extractall(root, filter="data")
        for rel in ANSWER_PATHS:
            target = root / rel
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        # The real CLI refuses to run outside a trusted git repository
        # ("Not inside a trusted directory and --skip-git-repo-check was not
        # specified"), and reviewers reach for `git diff`/`git log`: the
        # extracted tree becomes a one-commit repository of its own (Codex
        # review of PR #72, round 6).
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "eval",
            "GIT_AUTHOR_EMAIL": "eval@localhost",
            "GIT_COMMITTER_NAME": "eval",
            "GIT_COMMITTER_EMAIL": "eval@localhost",
        }
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "--allow-empty", "-m", f"eval case at {sha[:12]}"],
        ):
            subprocess.run(argv, cwd=root, capture_output=True, env=git_env, timeout=120)
        docs = root / "docs"
        entries = [*root.iterdir(), *(docs.iterdir() if docs.is_dir() else [])]
        entries = [p for p in entries if p.name != ".git"]
        listing = sorted(str(p.relative_to(root)) for p in entries)
        yield root, listing
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_codex(prompt: str, codex_bin: str = "codex", cwd: str | Path | None = None) -> ReviewerRun:
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as out:
        path = out.name
    # Never the repository root: without an isolated checkout the reviewer
    # gets an empty folder rather than the answers.
    scratch = None
    if cwd is None:
        scratch = tempfile.mkdtemp(prefix="eval-empty-")
        cwd = scratch
    try:
        result = subprocess.run(
            # --skip-git-repo-check as belt and braces: the checkout is a
            # repository already, but a review must never fail on that check.
            [codex_bin, "exec", "-s", "read-only", "--skip-git-repo-check", "-o", path, prompt],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        Path(path).unlink(missing_ok=True)
        return ReviewerRun(text="", returncode=-1, stderr=type(exc).__name__)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    text = Path(path).read_text() if Path(path).exists() else ""
    Path(path).unlink(missing_ok=True)
    return ReviewerRun(
        text=text or result.stdout, returncode=result.returncode, stderr=result.stderr
    )


def scrubbed(value):
    """Reviewer-controlled text is redacted before it is logged or saved:
    every string in a finding, an error message or an output excerpt (Codex
    review of PR #72, round 5, finding 2)."""
    if isinstance(value, str):
        return policy_mod.scrub_secrets(value)
    if isinstance(value, list):
        return [scrubbed(v) for v in value]
    if isinstance(value, dict):
        return {str(k): scrubbed(v) for k, v in value.items()}
    return value


def parse_findings(text: str) -> tuple[list[dict], str | None]:
    """The reviewer's findings, or (None-findings, reason) when the output is
    not the documented shape: a JSON object whose `findings` is a list of
    objects with string `topic` and `summary`. Prose, `{"findings": null}`
    or a list of numbers is a reviewer that did not review, not an empty
    review (Codex review of PR #72, round 3, finding 5)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return [], "reviewer output is not JSON"
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return [], f"reviewer output is not JSON: {exc.msg}"
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return [], "reviewer output has no `findings` list"
    findings = data["findings"]
    if not all(
        isinstance(f, dict)
        and isinstance(f.get("topic"), str)
        and isinstance(f.get("summary"), str)
        for f in findings
    ):
        return [], "reviewer output has a finding without string `topic` and `summary`"
    # Only the documented fields survive: an undocumented property is
    # reviewer-controlled text that would otherwise be saved under a key the
    # scrubber never looks at (Codex review of PR #72, round 7, finding 2).
    return [{"topic": f["topic"], "summary": f["summary"]} for f in findings], None


def _wants_checkout(runner) -> bool:
    """A runner that takes a working directory gets an isolated checkout of
    the case's commit; a prompt-only runner (a stub that never runs a
    process) needs none."""
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return True
    required = [p for p in params.values() if p.default is inspect.Parameter.empty]
    return "cwd" in params or len(required) >= 2


def _error(case: dict, message: str) -> dict:
    return {
        "id": case["id"],
        "round": case["round"],
        "expected_topics": sorted(case["expected_topics"]),
        "found_topics": [],
        "recall": None,
        "status": "error",
        "method": "codex exec review",
        "error": scrubbed(message),
    }


def grade_codex(case: dict, keywords: dict[str, list[str]], runner=run_codex) -> dict:
    topics = "\n".join(f"- {t}: {', '.join(w)}" for t, w in keywords.items())
    prompt = REVIEW_PROMPT.format(topics=topics, diff=case["diff"])
    sha = str(case.get("source_sha") or "")
    listing: list[str] | None = None
    if _wants_checkout(runner):
        try:
            with historical_checkout(sha) as (workdir, listing):
                # By keyword: the second POSITIONAL parameter of run_codex is
                # the binary path (Codex review of PR #72, round 7, finding 4).
                ran = runner(prompt, cwd=workdir)
        except CheckoutUnavailable as exc:
            return _error(case, f"no isolated checkout: {exc}")
    else:
        ran = runner(prompt)
    if isinstance(ran, str):
        ran = ReviewerRun(text=ran, returncode=0)
    # Scrub the COMPLETE output before taking an excerpt: a slice that cut
    # away `password=` but kept its value would let the value through
    # (Codex review of PR #72, round 9, finding 2).
    if ran.returncode != 0 or not ran.text.strip():
        error = scrubbed((ran.stderr or ran.text or "no output").strip())[-500:]
        return _error(case, f"reviewer exited {ran.returncode}: {error}")
    findings, invalid = parse_findings(ran.text)
    if invalid:
        return _error(case, f"{invalid}: {scrubbed(ran.text.strip())[-300:]}")
    findings = scrubbed(findings)
    # Each finding is credited to exactly one topic: the one the reviewer
    # named, or, only when it named none of ours ("other" or an unknown
    # label), the topic its summary classifies under by the policy's own
    # rule (Codex review of PR #72, finding 5).
    credited: set[str] = set()
    for f in findings:
        label = str(f.get("topic"))
        if label in keywords:
            credited.add(label)
        else:
            fallback = policy_mod.classify_finding(str(f.get("summary", "")), keywords)
            if fallback:
                credited.add(fallback)
    found = {t for t in case["expected_topics"] if t in credited}
    return score(
        case,
        found,
        detail={
            "method": "codex exec review",
            "reviewer_findings": findings[:20],
            "reviewer_checkout": listing,
        },
    )


def score(case: dict, found: set[str], detail: dict) -> dict:
    expected = set(case["expected_topics"])
    return {
        "id": case["id"],
        "round": case["round"],
        "expected_topics": sorted(expected),
        "found_topics": sorted(found),
        "recall": round(len(found & expected) / len(expected), 4) if expected else None,
        "status": "completed",
        **detail,
    }


def summarize(results: list[dict]) -> dict:
    """Mean recall over completed reviews only; runs the reviewer could not
    complete are counted in `errors`, never as recall 0."""
    errors = [r for r in results if r.get("status") == "error"]
    scored = [r for r in results if r["recall"] is not None and r.get("status") != "error"]
    per_topic: dict[str, list[int]] = {}
    for r in scored:
        for t in r["expected_topics"]:
            per_topic.setdefault(t, []).append(1 if t in r["found_topics"] else 0)
    return {
        "cases": len(scored),
        "errors": len(errors),
        "mean_recall": round(sum(r["recall"] for r in scored) / len(scored), 4) if scored else None,
        "per_topic_recall": {t: round(sum(v) / len(v), 4) for t, v in sorted(per_topic.items())},
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--grader", choices=["keywords", "codex"], default="keywords")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--codex-bin", default="codex")
    args = parser.parse_args(argv[1:])
    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    keywords = policy_mod.topic_keywords(policy)
    cases = load_cases(Path(args.cases), args.case, args.limit)
    results = []
    for case in cases:
        if args.grader == "codex":
            result = grade_codex(
                case, keywords, lambda p, cwd=None: run_codex(p, args.codex_bin, cwd)
            )
        else:
            result = grade_keywords(case, keywords)
        results.append(result)
        if result.get("status") == "error":
            print(f"  {result['id']}: ERROR {result['error']}")
        else:
            print(
                f"  {result['id']}: recall {result['recall']}  expected {result['expected_topics']}  found {result['found_topics']}"
            )
    summary = summarize(results)
    report = {
        "grader": args.grader,
        "policy_version": policy["version"],
        "policy_hash": policy_mod.policy_hash(policy),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary,
        "results": results,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{args.grader}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    policy_mod.assert_safe_output(target, inputs=[args.policy])
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"grader": args.grader, **summary, "written": policy_mod.relative_to_repo(target)},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
