#!/usr/bin/env python3
"""Pull the agent session traces linked to a PR's commits (via `traces
setup git`'s post-commit notes) and search them for the same recurring
topics detect-recurring-pattern.py already tracks.

This is the CI-side half of scripts/analyze-traces.py: that script searches
whatever is in the LOCAL Traces database, which is only ever your own
machine's session history -- meaningless inside a GitHub Actions runner,
which starts fresh every run with no local session files at all. This
script instead:

  1. Reads `traces notes --json`, which parses git notes under
     refs/notes/traces (see docs/production-hardening-backlog.md item #4's
     trace-analysis note) -- each note records which trace external ID
     produced which commit.
  2. Filters to notes whose commitRef falls in the PR's actual commit range
     (base..head), not just "recent" notes from unrelated work.
  3. Syncs each matched trace from the Traces API using an API key
     (verified against the real API: `traces sync <id> --key $TRACES_API_KEY
     --json` pulls real message content using only the key, no local CLI
     login session required -- exactly CI's situation).
  4. Runs the shared topic search (analyze_traces.analyze_by_topic) scoped
     to exactly those synced trace IDs via --trace-id, not --dir.

Traces only appear in git notes for commits made after both `traces setup
git` (records the note) and `traces setup agents --hooks` (tracks the
active session so the git hook has a trace ID to attach) were installed
locally by whoever made the commit -- a PR from before that setup, or from
a contributor who hasn't installed the hooks, simply has no linked traces,
and this script reports that plainly rather than treating it as an error.

Usage:
    python3 sync-pr-traces.py <repo-dir> <base-sha> <head-sha>
        [--traces-bin PATH] [--traces-key KEY] [--notes-limit N]

Requires TRACES_API_KEY in the environment, or --traces-key. Prints a
report in the same shape as analyze-traces.py, then a JSON summary after a
`---` separator.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


analyze_traces = _load_sibling_module("analyze_traces", "analyze-traces.py")
TracesCliError = analyze_traces.TracesCliError


def pr_commit_shas(repo_dir: str, base_sha: str, head_sha: str) -> set[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H", f"{base_sha}..{head_sha}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise TracesCliError(f"`git log {base_sha}..{head_sha}` failed: {result.stderr.strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def notes_for_commits(
    traces_bin: str, repo_dir: str, commit_shas: set[str], notes_limit: int
) -> list[str]:
    """Returns the deduplicated external trace IDs linked to any of the
    given commits, via `traces notes` (parses refs/notes/traces)."""
    data = analyze_traces.run_traces_json(
        traces_bin, ["notes", "--cwd", repo_dir, "--limit", str(notes_limit)]
    )
    notes = data.get("notes", [])
    matched = [n["externalId"] for n in notes if n.get("commitRef") in commit_shas]
    # Preserve order, drop duplicates (multiple commits can reference the
    # same trace).
    seen: set[str] = set()
    deduped = []
    for trace_id in matched:
        if trace_id not in seen:
            seen.add(trace_id)
            deduped.append(trace_id)
    return deduped


def sync_trace(traces_bin: str, trace_id: str, traces_key: str) -> bool:
    """Pulls one trace's content from the API using only the key -- no
    local CLI login required, matching a fresh CI runner. Returns whether
    the sync succeeded; a single trace failing to sync (e.g. since revoked,
    or the key's namespace no longer matches) doesn't abort the whole run."""
    try:
        analyze_traces.run_traces_json(
            traces_bin, ["sync", trace_id, "--key", traces_key]
        )
        return True
    except TracesCliError:
        return False


def sync_and_analyze(
    traces_bin: str, repo_dir: str, base_sha: str, head_sha: str, traces_key: str, notes_limit: int
) -> dict:
    commit_shas = pr_commit_shas(repo_dir, base_sha, head_sha)
    trace_ids = notes_for_commits(traces_bin, repo_dir, commit_shas, notes_limit)

    synced = [tid for tid in trace_ids if sync_trace(traces_bin, tid, traces_key)]

    if not synced:
        return {
            "commits_in_range": len(commit_shas),
            "linked_traces": len(trace_ids),
            "synced_traces": 0,
            "topics": [],
        }

    scope_args = ["--trace-id", ",".join(synced)]
    return {
        "commits_in_range": len(commit_shas),
        "linked_traces": len(trace_ids),
        "synced_traces": len(synced),
        "topics": analyze_traces.analyze_by_topic(traces_bin, scope_args),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_dir")
    parser.add_argument("base_sha")
    parser.add_argument("head_sha")
    parser.add_argument("--traces-bin", default="traces")
    parser.add_argument("--traces-key", default=os.environ.get("TRACES_API_KEY", ""))
    parser.add_argument("--notes-limit", type=int, default=200)
    args = parser.parse_args(argv[1:])

    if not args.traces_key:
        print("::error::TRACES_API_KEY not set (env var or --traces-key)", file=sys.stderr)
        return 1

    try:
        result = sync_and_analyze(
            args.traces_bin,
            args.repo_dir,
            args.base_sha,
            args.head_sha,
            args.traces_key,
            args.notes_limit,
        )
    except TracesCliError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if result["linked_traces"] == 0:
        print(
            f"No traces linked to any of {result['commits_in_range']} commit(s) in range "
            "(no one had the git/agent hooks installed when these commits were made)."
        )
    else:
        print(
            f"{result['synced_traces']}/{result['linked_traces']} linked trace(s) synced "
            f"from {result['commits_in_range']} commit(s) in range."
        )
        for topic in result["topics"]:
            print(f"[{topic['topic']}] {topic['matching_traces']} matching trace(s)")

    print("---")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
