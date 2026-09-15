#!/usr/bin/env python3
"""Mine actual failures out of agent session traces, so the field anchor
counts things that went wrong, never things people said.

The first field anchor searched transcript text for the taxonomy's
keywords. Every hit turned out to be narration — the assistant summarising
review findings — so the "field" merely echoed the reviews it was supposed
to check. This tool reads the events of each working session directly
through `traces show --json` and keeps only:

  * tool results Traces itself marked `status: "error"` — nothing else.
    The failure shape of the output (traceback, non-zero exit, test
    failure, permission or auth error, git rejection, timeout) only names
    the kind; displayed text never turns a successful execution into a
    failure.

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
import tempfile
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
MAX_TEXT_CHARS = 400_000  # per failure, all occurrences' output kept for matching

# Failure shapes, each named so a report can say what kind of thing broke.
FAILURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "traceback": re.compile(r"Traceback \(most recent call last\)"),
    "nonzero-exit": re.compile(r"^Exit code [1-9]\d*", re.M),
    # "0 failed" is a pass; only a positive count is a failure.
    "test-failure": re.compile(r"\b[1-9]\d* failed\b|^FAILED ", re.M),
    "permission": re.compile(
        r"Permission denied|EACCES|denied by the .* classifier|Operation not permitted"
    ),
    # HTTP-shaped only: a bare "401" is far more often a line number in a
    # file read than an auth failure.
    "auth": re.compile(
        r"HTTP/?[\d.]* ?40[13]\b|\b40[13] (?:Unauthorized|Forbidden)|status(?: code)?[:=]? ?40[13]\b"
        r"|\bUnauthorized\b|\bForbidden\b|token (?:expired|invalid)|authentication failed",
        re.I,
    ),
    "git-rejected": re.compile(r"non-fast-forward|rejected\]|^fatal: |merge conflict", re.I | re.M),
    "timeout": re.compile(r"timed out|timeout of \d+|ETIMEDOUT|TLS handshake timeout", re.I),
    "not-found": re.compile(r"No such file or directory|command not found|ENOENT", re.I),
}


class TracesCliError(RuntimeError):
    pass


def parse_cli_json(stdout: str) -> dict | None:
    """The CLI may print a hydration notice before the JSON document the
    first time a session's events are loaded; parse from the first brace."""
    for candidate in (stdout, stdout[stdout.find("{") :] if "{" in stdout else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


# Extra arguments appended to every CLI call (an API key on a runner that is
# not logged in). Set once by main(); never printed.
EXTRA_CLI_ARGS: list[str] = []


def run_traces_json(traces_bin: str, args: list[str], *, retries: int = 1) -> dict:
    last_error = "no output"
    for attempt in range(retries + 1):
        # stdout goes to a file, not a pipe: the CLI truncates piped output at
        # 64 KiB (observed: 65519 bytes of an 80 KB document), while a file
        # redirect receives everything.
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
                result = subprocess.run(
                    [traces_bin, *args, *EXTRA_CLI_ARGS, "--json"],
                    stdout=out,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300,
                )
                out.seek(0)
                stdout = out.read()
        except OSError as exc:
            raise TracesCliError(f"Could not run `{traces_bin}`: {exc}") from exc
        if result.returncode != 0:
            raise TracesCliError(f"`{traces_bin} {' '.join(args)}` failed: {result.stderr.strip()}")
        payload = parse_cli_json(stdout)
        if payload is not None:
            if not payload.get("ok"):
                raise TracesCliError(f"`{traces_bin} {' '.join(args)}` reported failure: {payload}")
            return payload["data"]
        last_error = stdout[:120].replace("\n", " ")
        if attempt < retries:
            continue
    raise TracesCliError(f"Non-JSON output from `{traces_bin} {' '.join(args)}`: {last_error}")


def list_traces(
    traces_bin: str, repo_dir: str, agents: list[str] | None, limit: int
) -> tuple[list[dict], bool]:
    """Sessions recorded in the folder, and whether the listing was complete.
    A listing that fills its limit may have missed sessions, and evidence
    built from it must say so rather than report confirmed zero counts
    (Codex review of PR #10, round 27)."""
    found: dict[str, dict] = {}
    complete = True
    for agent_args in [["--agent", a] for a in agents] if agents else [[]]:
        data = run_traces_json(
            traces_bin, ["list", "--dir", repo_dir, *agent_args, "--limit", str(limit)]
        )
        traces = data.get("traces", [])
        if len(traces) >= limit:
            complete = False
        for trace in traces:
            found[trace["id"]] = trace
    return sorted(found.values(), key=lambda t: (t.get("timestamp") or 0, t["id"])), complete


def list_namespace_traces(
    traces_bin: str, namespace: str, agents: list[str] | None
) -> tuple[list[dict], bool]:
    """Sessions shared to a traces.com namespace (for a runner that has no
    local sessions of its own). `--all` returns the whole namespace, so the
    listing is complete by construction; agents are filtered client-side."""
    if not namespace.startswith("@"):
        namespace = f"@{namespace}"
    data = run_traces_json(traces_bin, ["list", namespace, "--all"])
    traces = [
        t
        for t in data.get("traces", [])
        if isinstance(t.get("id"), str) and (agents is None or t.get("agentId") in agents)
    ]
    return sorted(traces, key=lambda t: (t.get("timestamp") or 0, t["id"])), True


def sync_trace(traces_bin: str, trace_id: str) -> None:
    """Pull a shared trace's events into the local database so `show` can
    page through them (a fresh runner has none)."""
    run_traces_json(traces_bin, ["sync", trace_id])


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
    """A failure is an execution the tool itself reported as an error
    (`status: "error"`). Output text never decides whether something
    failed — a displayed transcript can contain "Exit code 1" or "HTTP 401"
    verbatim (Codex review of PR #10, rounds 27-30). The failure shape only
    names the kind once the status says it failed."""
    if event.get("status") != "error":
        return None
    output = str(event.get("output") or event.get("text") or "")
    # Most specific shape first; a bare non-zero exit is the fallback name.
    for name, pattern in FAILURE_PATTERNS.items():
        if name != "nonzero-exit" and pattern.search(output):
            return name
    if FAILURE_PATTERNS["nonzero-exit"].search(output):
        return "nonzero-exit"
    return "tool-error"


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
        # Anything that leaves this process is scrubbed first: a report, a
        # log line, or the policy's mined_from evidence is public the moment
        # the proposal PR is pushed (Codex review of PR #10, round 38).
        excerpt = policy_mod.scrub_secrets(excerpt_for(kind, output))
        command = policy_mod.scrub_secrets(command)
        # The command is part of identity: two commands with the same output
        # are two failures, and classification reads the command
        # (Codex review of PR #10, round 28).
        key = (tool, " ".join(command.split())[:EXCERPT_CHARS], excerpt)
        if key in failures:
            failures[key]["count"] += 1
            # A repeat with the same header but different later diagnostics
            # still contributes its text to topic matching (round 38).
            failures[key]["_text"] = (failures[key]["_text"] + " " + output.lower())[
                :MAX_TEXT_CHARS
            ]
            continue
        failures[key] = {
            "_text": f"{command} {output}".lower()[:MAX_TEXT_CHARS],
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


def failure_text(failure: dict) -> str:
    """The text a topic is matched against: the full command and the full
    tool output while mining (kept in memory under a private key and never
    written out), so a keyword past the excerpt still counts; the stored
    command and excerpt for records read back from disk (Codex full-branch
    review, finding 3)."""
    full = failure.get("_text")
    if isinstance(full, str):
        return full
    return f"{failure['command']} {failure['excerpt']}".lower()


def public_failure(failure: dict) -> dict:
    return {k: v for k, v in failure.items() if not k.startswith("_")}


def matching_topics(failure: dict, keywords: dict[str, list[str]]) -> list[str]:
    """Every topic whose keywords appear in the failure, independently of
    taxonomy order, so a stored count never depends on which other topics
    existed when it was collected (Codex review of PR #10, round 27)."""
    text = failure_text(failure)
    return [topic for topic, words in keywords.items() if any(w.lower() in text for w in words)]


def classify(failure: dict, keywords: dict[str, list[str]]) -> str | None:
    text = f"{failure['command']} {failure['excerpt']}"
    return policy_mod.classify_finding(text, keywords)


def build_evidence(
    failures: list[dict],
    keywords: dict[str, list[str]],
    repo_dir: str,
    agents: list[str] | None,
    complete: bool = True,
    scanned: list[str] | None = None,
    namespace: str | None = None,
) -> dict:
    per_topic: dict[str, dict[str, dict]] = defaultdict(dict)
    for failure in failures:
        for topic in matching_topics(failure, keywords):
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
        "truncated": [] if complete else list(keywords),
        "listing_complete": complete,
        "failure_count": len(failures),
        # Every session scanned, matched or not: a complete scan with only
        # unclassified failures is still an observed field (round 36).
        "sessions": sorted(set(scanned or []) | {f["trace_id"] for f in failures}),
        "namespace": namespace,
    }


def report(failures: list[dict], keywords: dict[str, list[str]]) -> tuple[list[str], dict]:
    lines: list[str] = []
    by_kind = Counter(f["kind"] for f in failures)
    by_topic: Counter[str] = Counter()
    blind: list[dict] = []
    for failure in failures:
        topics = matching_topics(failure, keywords)
        if not topics:
            blind.append(failure)
        for topic in topics:
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
        "failures": [public_failure(f) for f in failures],
        "by_kind": dict(by_kind),
        "by_topic": {t: by_topic.get(t, 0) for t in keywords},
        "blind_spots": [public_failure(f) for f in blind],
        "sessions": sorted(sessions),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--repo-dir", help="mine the local sessions recorded in this folder")
    where.add_argument(
        "--namespace",
        help="mine every session shared to this traces.com namespace (e.g. @gagan114); "
        "for runners with no local sessions",
    )
    parser.add_argument(
        "--traces-key",
        default=None,
        help="API key passed as --key to every traces call (CI); omit when logged in locally",
    )
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

    # Two keyword sets with two jobs: blind spots are what the CURRENT policy
    # cannot classify (a retired topic must not keep hiding fresh failures
    # from mining), while evidence keeps counting every definition any
    # policy version ever had, so a rolled-back topic retains the adverse
    # evidence that blocks re-mining it (Codex review of PR #10, round 34).
    current_keywords = dict(policy_mod.topic_keywords(policy))
    search_keywords = dict(current_keywords)
    search_keywords.update(
        measure_mod.historical_definitions(policy_mod.load_history(args.history), current_keywords)
    )
    agents = (
        None
        if args.agents.strip() == "all"
        else [a.strip() for a in args.agents.split(",") if a.strip()]
    )

    if args.traces_key:
        EXTRA_CLI_ARGS[:] = ["--key", args.traces_key]

    try:
        if args.namespace:
            traces, complete = list_namespace_traces(args.traces_bin, args.namespace, agents)
        else:
            traces, complete = list_traces(args.traces_bin, args.repo_dir, agents, args.limit)
        if not complete:
            print(
                f"::warning::session listing hit --limit {args.limit}; evidence counts are marked unknown, raise --limit"
            )
        failures: list[dict] = []
        for trace in traces:
            if agents is None and trace.get("agentId") == VERIFIER_AGENT:
                pass  # "all" deliberately includes the verifier's own sessions
            if args.namespace:
                sync_trace(args.traces_bin, trace["id"])
            failures.extend(mine_trace(args.traces_bin, trace))
    except TracesCliError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    lines, summary = report(failures, current_keywords)
    summary["traces_scanned"] = len(traces)
    summary["listing_complete"] = complete
    summary["repo_dir"] = args.repo_dir
    summary["namespace"] = args.namespace
    for line in lines:
        print(line)
    if args.save_evidence:
        Path(args.save_evidence).write_text(
            json.dumps(
                build_evidence(
                    failures,
                    search_keywords,
                    args.repo_dir or "",
                    agents,
                    complete,
                    scanned=[t["id"] for t in traces],
                    namespace=args.namespace,
                ),
                indent=2,
            )
            + "\n"
        )
        print(f"evidence written to {args.save_evidence}")
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(summary, indent=2) + "\n")
    print("---")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
