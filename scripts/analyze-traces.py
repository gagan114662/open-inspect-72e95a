#!/usr/bin/env python3
"""Detect recurring topics in actual agent session traces, not just in PR
diffs reviewed after the fact.

Everything this archive has measured so far (scripts/detect-recurring-pattern.py,
docs/self-improvement-archive.jsonl) comes from Codex's review of a PR's
final diff -- a real, but narrow, signal. It never sees the WORK itself: a
session that struggled with the same class of problem three times before
landing a clean diff looks identical, in that signal, to one that got it
right on the first try. The `traces` CLI (traces.com) indexes local agent
session transcripts (Claude Code, Codex, and others) and can search their
actual content, which is a genuinely different and complementary evidence
source: it can catch a recurring struggle even when the shipped diff never
shows it.

Uses the SAME topic/keyword taxonomy as detect-recurring-pattern.py so a
topic's evidence is comparable across both sources, not a parallel
vocabulary that never lines up.

Usage:
    python3 analyze-traces.py <repo-dir> [--limit N] [--traces-bin PATH]

Requires the `traces` CLI on PATH (https://traces.com), already
authenticated (`traces login`). Prints one line per topic with its
matching-trace count, then a JSON summary after a `---` separator. Exits 0
always (advisory) unless the `traces` binary itself cannot be found or run,
in which case it fails loudly rather than silently reporting zero evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_sibling_module(name: str, filename: str):
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


detect_mod = _load_sibling_module("detect_recurring_pattern", "detect-recurring-pattern.py")


class TracesCliError(RuntimeError):
    pass


def run_traces_json(traces_bin: str, args: list[str]) -> dict:
    try:
        result = subprocess.run(
            [traces_bin, *args, "--json"],
            capture_output=True,
            text=True,
            timeout=60,
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


def list_repo_trace_ids(traces_bin: str, repo_dir: str, limit: int) -> list[str]:
    data = run_traces_json(traces_bin, ["list", "--dir", repo_dir, "--limit", str(limit)])
    return [t["id"] for t in data.get("traces", [])]


def hydrate_trace(traces_bin: str, trace_id: str) -> None:
    """Loads one trace's messages into traces' local search cache. A trace
    saved with only title/metadata is invisible to event-level search until
    shown at least once -- see `traces search-instructions`. Cheap and
    idempotent; failures here are non-fatal (the trace just stays
    metadata-only and search falls back to title matching for it)."""
    subprocess.run(
        [traces_bin, "show", trace_id, "--event-type", "user_message,agent_text", "--limit", "1"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def search_topic(traces_bin: str, pattern: str, repo_dir: str) -> list[dict]:
    data = run_traces_json(
        traces_bin,
        ["search", pattern, "--dir", repo_dir, "--result-level", "trace", "--limit", "100"],
    )
    return data.get("traces", [])


def analyze(traces_bin: str, repo_dir: str, limit: int) -> dict:
    trace_ids = list_repo_trace_ids(traces_bin, repo_dir, limit)
    for trace_id in trace_ids:
        hydrate_trace(traces_bin, trace_id)

    topics = []
    for topic, keywords in detect_mod.TOPIC_KEYWORDS.items():
        pattern = "|".join(keywords)
        matches = search_topic(traces_bin, pattern, repo_dir)
        topics.append(
            {
                "topic": topic,
                "matching_traces": len(matches),
                "trace_ids": [m["id"] for m in matches],
            }
        )

    return {
        "repo_dir": repo_dir,
        "traces_scanned": len(trace_ids),
        "topics": sorted(topics, key=lambda t: -t["matching_traces"]),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_dir")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--traces-bin", default="traces")
    args = parser.parse_args(argv[1:])

    try:
        result = analyze(args.traces_bin, args.repo_dir, args.limit)
    except TracesCliError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    for topic in result["topics"]:
        print(f"[{topic['topic']}] {topic['matching_traces']} matching trace(s)")

    print("---")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
