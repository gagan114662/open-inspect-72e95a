#!/usr/bin/env python3
"""Mine actual failures out of agent session traces, so the field anchor
counts things that went wrong, never things people said.

The first field anchor searched transcript text for the taxonomy's
keywords. Every hit turned out to be narration — the assistant summarising
review findings — so the "field" merely echoed the reviews it was supposed
to check. This tool reads the events of each working session directly
through `traces show --json` and keeps only:

  * tool results Traces itself marked `status: "error"`, and
  * tool results whose output matches a failure shape (traceback, non-zero
    exit, test failure, permission or auth error, git rejection, timeout).

Each failure is paired with the command that produced it, deduplicated per
session, classified with the current improvement policy, and written out
two ways: a human report plus JSON (`--out-json`), and an evidence file in
the shape scripts/measure-policy-validity.py consumes (`--save-evidence`),
where a topic's evidence is the set of sessions in which a failure matching
that topic's keywords actually occurred. Failures no topic claims are the
field's blind spots — the same kind of signal the meta-improver mines from
unclassified review findings.

Usage:
    python3 mine-trace-failures.py --repo-dir DIR [--agents a,b|all]
        [--policy PATH] [--history PATH] [--out-json PATH] [--save-evidence PATH]
        [--traces-bin PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
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

VERIFIER_AGENT = "codex"
PAGE_SIZE = 200
EXCERPT_CHARS = 160

# Failure shapes, each named so a report can say what kind of thing broke.
FAILURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "traceback": re.compile(r"Traceback \(most recent call last\)"),
    "nonzero-exit": re.compile(r"^Exit code [1-9]\d*", re.M),
    "test-failure": re.compile(r"\b\d+ failed\b|^FAILED ", re.M),
    "permission": re.compile(
        r"Permission denied|EACCES|denied by the .* classifier|Operation not permitted"
    ),
    "auth": re.compile(
        r"\b(401|403)\b|Unauthorized|Forbidden|token (?:expired|invalid)|authentication failed",
        re.I,
    ),
    "git-rejected": re.compile(r"non-fast-forward|rejected\]|fatal: |merge conflict", re.I),
    "timeout": re.compile(r"timed out|timeout of \d+|ETIMEDOUT|TLS handshake timeout", re.I),
    "not-found": re.compile(
        r"No such file or directory|command not found|ENOENT|not found: ", re.I
    ),
}


class TracesCliError(RuntimeError):
    pass


def run_traces_json(traces_bin: str, args: list[str]) -> dict:
    try:
        result = subprocess.run(
            [traces_bin, *args, "--json"], capture_output=True, text=True, timeout=120
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


def list_traces(traces_bin: str, repo_dir: str, agents: list[str] | None, limit: int) -> list[dict]:
    found: dict[str, dict] = {}
    for agent_args in [["--agent", a] for a in agents] if agents else [[]]:
        data = run_traces_json(
            traces_bin, ["list", "--dir", repo_dir, *agent_args, "--limit", str(limit)]
        )
        for trace in data.get("traces", []):
            found[trace["id"]] = trace
    return sorted(found.values(), key=lambda t: (t.get("timestamp") or 0, t["id"]))


def iter_events(traces_bin: str, trace_id: str):
    offset = 1
    while True:
        data = run_traces_json(
            traces_bin, ["show", trace_id, "--offset", str(offset), "--limit", str(PAGE_SIZE)]
        )
        events = data.get("events") or []
        yield from events
        if len(events) < PAGE_SIZE:
            return
        offset += len(events)


def failure_kind(event: dict) -> str | None:
    output = str(event.get("output") or event.get("text") or "")
    for name, pattern in FAILURE_PATTERNS.items():
        if pattern.search(output):
            return name
    if event.get("status") == "error":
        return "tool-error"
    return None


def excerpt_for(kind: str, output: str) -> str:
    pattern = FAILURE_PATTERNS.get(kind)
    if pattern is not None:
        match = pattern.search(output)
        if match:
            line_start = output.rfind("\n", 0, match.start()) + 1
            line_end = output.find("\n", match.end())
            line = output[line_start : line_end if line_end != -1 else None]
            rest = output[line_end + 1 :] if line_end != -1 else ""
            # The matched line plus the next non-empty line: "Exit code 1"
            # alone would collapse every distinct failure into one.
            follow = next((ln for ln in rest.splitlines() if ln.strip()), "")
            return " ".join(f"{line} {follow}".split())[:EXCERPT_CHARS]
    return " ".join(output.split())[:EXCERPT_CHARS]


def mine_trace(traces_bin: str, trace: dict) -> list[dict]:
    """Failures in one session, each paired with the command that caused it
    and deduplicated by (tool, excerpt) with an occurrence count."""
    calls: dict[str, dict] = {}
    failures: dict[tuple[str, str], dict] = {}
    for event in iter_events(traces_bin, trace["id"]):
        etype = event.get("type")
        if etype == "tool_call":
            calls[str(event.get("callId"))] = event
            continue
        if etype not in {"tool_result", "error"}:
            continue
        kind = failure_kind(event)
        if kind is None:
            continue
        output = str(event.get("output") or event.get("text") or "")
        tool = str(event.get("toolName") or "")
        call = calls.get(str(event.get("callId")), {})
        args = call.get("args") or {}
        command = str(args.get("command") or args.get("file_path") or args.get("pattern") or "")
        excerpt = excerpt_for(kind, output)
        key = (tool, excerpt)
        if key in failures:
            failures[key]["count"] += 1
            continue
        failures[key] = {
            "trace_id": trace["id"],
            "agent": trace.get("agentId"),
            "event_number": event.get("eventNumber"),
            "timestamp": event.get("timestamp"),
            "tool": tool,
            "kind": kind,
            "command": " ".join(command.split())[:EXCERPT_CHARS],
            "excerpt": excerpt,
            "count": 1,
        }
    return sorted(failures.values(), key=lambda f: (f["event_number"] or 0))


def classify(failure: dict, keywords: dict[str, list[str]]) -> str | None:
    text = f"{failure['command']} {failure['excerpt']}"
    return policy_mod.classify_finding(text, keywords)


def build_evidence(
    failures: list[dict],
    keywords: dict[str, list[str]],
    repo_dir: str,
    agents: list[str] | None,
) -> dict:
    per_topic: dict[str, dict[str, dict]] = defaultdict(dict)
    for failure in failures:
        topic = classify(failure, keywords)
        if topic is None:
            continue
        per_topic[topic].setdefault(
            failure["trace_id"],
            {
                "id": failure["trace_id"],
                "agentId": failure["agent"],
                "timestamp": failure["timestamp"],
            },
        )
    return {
        "source": "trace-failures",
        "collected_at": policy_mod.utc_now_iso(),
        "repo_dir": repo_dir,
        "agents": agents or ["all"],
        "event_types": "tool_result(status=error) or failure-shaped output",
        "definitions": {topic: list(words) for topic, words in keywords.items()},
        "topics": {
            topic: sorted(
                per_topic.get(topic, {}).values(), key=lambda t: (t["timestamp"] or 0, t["id"])
            )
            for topic in keywords
        },
        "truncated": [],
        "failure_count": len(failures),
    }


def report(failures: list[dict], keywords: dict[str, list[str]]) -> tuple[list[str], dict]:
    lines: list[str] = []
    by_kind = Counter(f["kind"] for f in failures)
    by_topic: Counter[str] = Counter()
    blind: list[dict] = []
    for failure in failures:
        topic = classify(failure, keywords)
        if topic is None:
            blind.append(failure)
        else:
            by_topic[topic] += 1
    sessions = {f["trace_id"] for f in failures}
    lines.append(f"{len(failures)} distinct failure(s) across {len(sessions)} session(s)")
    for kind, n in by_kind.most_common():
        lines.append(f"  {kind}: {n}")
    lines.append("by policy topic (failures whose command or output matches the topic's keywords):")
    for topic in keywords:
        lines.append(f"  [{topic}] {by_topic.get(topic, 0)}")
    lines.append(f"unclassified failures (field blind spots): {len(blind)}")
    for failure in blind[:12]:
        lines.append(
            f"  {failure['trace_id'][:8]} #{failure['event_number']} {failure['tool']} ({failure['kind']}, x{failure['count']}): {failure['excerpt'][:110]}"
        )
    return lines, {
        "failures": failures,
        "by_kind": dict(by_kind),
        "by_topic": {t: by_topic.get(t, 0) for t in keywords},
        "blind_spots": blind,
        "sessions": sorted(sessions),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--agents", default=",".join(measure_mod.DEFAULT_ANCHOR_AGENTS))
    parser.add_argument("--policy", default=None)
    parser.add_argument("--history", default=str(policy_mod.HISTORY_PATH))
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--save-evidence", default=None)
    parser.add_argument("--traces-bin", default="traces")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv[1:])

    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    inputs = [args.policy, args.history]
    if args.out_json:
        policy_mod.assert_safe_output(args.out_json, inputs=inputs)
    if args.save_evidence:
        policy_mod.assert_safe_output(args.save_evidence, inputs=inputs, kind="evidence")
    if (
        args.out_json
        and args.save_evidence
        and Path(args.out_json).resolve() == Path(args.save_evidence).resolve()
    ):
        print("::error::--out-json and --save-evidence must be different files", file=sys.stderr)
        return 1

    keywords = dict(policy_mod.topic_keywords(policy))
    keywords.update(
        measure_mod.historical_definitions(policy_mod.load_history(args.history), keywords)
    )
    agents = (
        None
        if args.agents.strip() == "all"
        else [a.strip() for a in args.agents.split(",") if a.strip()]
    )

    try:
        traces = list_traces(args.traces_bin, args.repo_dir, agents, args.limit)
        failures: list[dict] = []
        for trace in traces:
            if agents is None and trace.get("agentId") == VERIFIER_AGENT:
                pass  # "all" deliberately includes the verifier's own sessions
            failures.extend(mine_trace(args.traces_bin, trace))
    except TracesCliError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    lines, summary = report(failures, keywords)
    summary["traces_scanned"] = len(traces)
    summary["repo_dir"] = args.repo_dir
    for line in lines:
        print(line)
    if args.save_evidence:
        Path(args.save_evidence).write_text(
            json.dumps(build_evidence(failures, keywords, args.repo_dir, agents), indent=2) + "\n"
        )
        print(f"evidence written to {args.save_evidence}")
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(summary, indent=2) + "\n")
    print("---")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
