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
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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


def load_cases(cases_dir: Path, only: list[str] | None, limit: int | None) -> list[dict]:
    cases = [json.loads(p.read_text()) for p in sorted(cases_dir.glob("round-*.json"))]
    if only:
        wanted = set(only)
        cases = [c for c in cases if c["id"] in wanted]
    cases = [c for c in cases if c["expected_topics"]]
    return cases[:limit] if limit else cases


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


def run_codex(prompt: str, codex_bin: str = "codex") -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as out:
        path = out.name
    result = subprocess.run(
        [codex_bin, "exec", "-s", "read-only", "-o", path, prompt],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
    )
    text = Path(path).read_text() if Path(path).exists() else ""
    Path(path).unlink(missing_ok=True)
    return text or result.stdout


def parse_findings(text: str) -> list[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [f for f in data.get("findings", []) if isinstance(f, dict)]


def grade_codex(case: dict, keywords: dict[str, list[str]], runner=run_codex) -> dict:
    topics = "\n".join(f"- {t}: {', '.join(w)}" for t, w in keywords.items())
    text = runner(REVIEW_PROMPT.format(topics=topics, diff=case["diff"]))
    findings = parse_findings(text)
    named = {str(f.get("topic")) for f in findings}
    # A finding the reviewer labelled "other" still counts if its summary
    # classifies under the expected topic by the policy's own rule.
    classified = {
        policy_mod.classify_finding(str(f.get("summary", "")), keywords) for f in findings
    }
    found = {t for t in case["expected_topics"] if t in named or t in classified}
    return score(
        case, found, detail={"method": "codex exec review", "reviewer_findings": findings[:20]}
    )


def score(case: dict, found: set[str], detail: dict) -> dict:
    expected = set(case["expected_topics"])
    return {
        "id": case["id"],
        "round": case["round"],
        "expected_topics": sorted(expected),
        "found_topics": sorted(found),
        "recall": round(len(found & expected) / len(expected), 4) if expected else None,
        **detail,
    }


def summarize(results: list[dict]) -> dict:
    scored = [r for r in results if r["recall"] is not None]
    per_topic: dict[str, list[int]] = {}
    for r in scored:
        for t in r["expected_topics"]:
            per_topic.setdefault(t, []).append(1 if t in r["found_topics"] else 0)
    return {
        "cases": len(scored),
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
            result = grade_codex(case, keywords, lambda p: run_codex(p, args.codex_bin))
        else:
            result = grade_keywords(case, keywords)
        results.append(result)
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
