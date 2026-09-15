"""Tests for mine-trace-failures.py.

Run with: python3 -m pytest scripts/mine_trace_failures_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parent / "mine-trace-failures.py"
_spec = importlib.util.spec_from_file_location("mine_trace_failures", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
mine = importlib.util.module_from_spec(_spec)
sys.modules["mine_trace_failures"] = mine
_spec.loader.exec_module(mine)
policy_mod = sys.modules["improvement_policy"]


def _events():
    return [
        {"type": "user_message", "text": "please fix the token expired error", "eventNumber": 1},
        {
            "type": "tool_call",
            "callId": "c1",
            "toolName": "Bash",
            "args": {"command": "pytest scripts/"},
            "eventNumber": 2,
        },
        {
            "type": "tool_result",
            "callId": "c1",
            "toolName": "Bash",
            "status": "error",
            "output": "FAILED scripts/x_test.py::test_y\n3 failed, 10 passed",
            "timestamp": 5,
            "eventNumber": 3,
        },
        {
            "type": "tool_call",
            "callId": "c2",
            "toolName": "Bash",
            "args": {"command": "git push origin main"},
            "eventNumber": 4,
        },
        {
            "type": "tool_result",
            "callId": "c2",
            "toolName": "Bash",
            "status": "error",
            "output": "Exit code 1\n! [rejected] main -> main (non-fast-forward)",
            "timestamp": 6,
            "eventNumber": 5,
        },
        {
            "type": "tool_call",
            "callId": "c3",
            "toolName": "Bash",
            "args": {"command": "gh api /user"},
            "eventNumber": 6,
        },
        {
            "type": "tool_result",
            "callId": "c3",
            "toolName": "Bash",
            "status": "error",
            "output": "HTTP 401 Unauthorized: session expired",
            "timestamp": 7,
            "eventNumber": 7,
        },
        {
            "type": "tool_result",
            "callId": "c3",
            "toolName": "Bash",
            "status": "error",
            "output": "HTTP 401 Unauthorized: session expired",
            "timestamp": 8,
            "eventNumber": 8,
        },
        {
            "type": "agent_text",
            "text": "The secret leaked into logs, this is a Traceback (most recent call last) story",
            "eventNumber": 9,
        },
        {
            "type": "tool_result",
            "callId": "c9",
            "toolName": "Read",
            "status": "success",
            "output": "all good",
            "timestamp": 9,
            "eventNumber": 10,
        },
    ]


def _fake_runner(events_by_trace):
    def run(_bin, args):
        if args[0] == "list":
            return {
                "traces": [
                    {"id": t, "agentId": "claude-code", "timestamp": 1} for t in events_by_trace
                ]
            }
        if args[0] == "show":
            trace_id, offset, limit = args[1], int(args[3]), int(args[5])
            events = events_by_trace[trace_id]
            return {"events": events[offset - 1 : offset - 1 + limit]}
        raise AssertionError(args)

    return run


def test_mines_failed_and_failure_shaped_tool_results_only(monkeypatch):
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": _events()}))
    failures = mine.mine_trace("traces", {"id": "t1", "agentId": "claude-code"})
    kinds = {f["excerpt"]: f["kind"] for f in failures}
    assert any(k == "test-failure" or k == "nonzero-exit" for k in kinds.values())
    assert any(k == "git-rejected" for k in kinds.values())
    assert any(k == "auth" for k in kinds.values())
    # narration mentioning "Traceback" is never a failure; the successful Read is not either
    assert all(f["tool"] == "Bash" for f in failures)
    auth = next(f for f in failures if f["kind"] == "auth")
    assert auth["count"] == 2 and auth["command"] == "gh api /user"


def test_pagination_walks_every_page(monkeypatch):
    many = []
    for i in range(1, 402):
        many.append(
            {
                "type": "tool_result",
                "callId": f"c{i}",
                "toolName": "Bash",
                "status": "error",
                "output": f"Exit code 1\nfailure number {i}",
                "timestamp": i,
                "eventNumber": i,
            }
        )
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": many}))
    failures = mine.mine_trace("traces", {"id": "t1", "agentId": "claude-code"})
    assert len(failures) == 401


def test_evidence_counts_sessions_per_topic_and_records_definitions(monkeypatch):
    monkeypatch.setattr(
        mine, "run_traces_json", _fake_runner({"t1": _events(), "t2": _events()[:3]})
    )
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    traces, complete = mine.list_traces("traces", "/repo", ["claude-code"], 50)
    assert complete
    failures = [f for t in traces for f in mine.mine_trace("traces", t)]
    evidence = mine.build_evidence(failures, keywords, "/repo", ["claude-code"])
    assert evidence["source"] == "trace-failures"
    assert evidence["definitions"] == keywords
    # "session expired" failure matches auth-lifecycle ("expir") in t1 only
    assert [t["id"] for t in evidence["topics"]["auth-lifecycle"]] == ["t1"]
    lines, summary = mine.report(failures, keywords)
    assert summary["blind_spots"], "the pytest failure matches no topic and is a blind spot"
    assert any("blind spots" in line for line in lines)


def test_main_writes_report_and_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": _events()}))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    out = tmp_path / "failures.json"
    evidence = tmp_path / "evidence.json"
    code = mine.main(
        [
            "m",
            "--repo-dir",
            "/repo",
            "--policy",
            str(policy),
            "--history",
            str(tmp_path / "h.jsonl"),
            "--out-json",
            str(out),
            "--save-evidence",
            str(evidence),
        ]
    )
    assert code == 0
    assert json.loads(out.read_text())["traces_scanned"] == 1
    assert "topics" in json.loads(evidence.read_text())
    assert "distinct failure(s)" in capsys.readouterr().out


def test_cli_json_is_parsed_past_a_leading_notice():
    assert mine.parse_cli_json('Hydrating 3 traces...\n{"ok": true, "data": {"events": []}}') == {
        "ok": True,
        "data": {"events": []},
    }
    assert mine.parse_cli_json("not json at all") is None


def test_line_numbers_and_file_contents_are_not_failures():
    read_with_line_numbers = {
        "type": "tool_result",
        "toolName": "Read",
        "status": "success",
        "output": "401 env_vars = {}\n402 json.dumps(x)\nTraceback (most recent call last) appears in this docstring",
    }
    assert mine.failure_kind(read_with_line_numbers) is None
    read_error = {
        "type": "tool_result",
        "toolName": "Read",
        "status": "error",
        "output": "File does not exist",
    }
    assert mine.failure_kind(read_error) == "tool-error"
    real_auth = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "error",
        "output": "HTTP 401 Unauthorized",
    }
    assert mine.failure_kind(real_auth) == "auth"
    displayed = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "success",
        "output": "HTTP 401 Unauthorized token expired\nTraceback (most recent call last)",
    }
    assert mine.failure_kind(displayed) is None, (
        "a successful cat of a file is content, not a failure"
    )
    bare_number = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "success",
        "output": "line 401 of 900",
    }
    assert mine.failure_kind(bare_number) is None
    shown_transcript = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "success",
        "output": "Exit code 1\nHTTP 401 Unauthorized",
    }
    assert mine.failure_kind(shown_transcript) is None, "status decides, output only names the kind"


def test_capped_listing_marks_every_topic_unknown(monkeypatch):
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": _events(), "t2": _events()}))
    _traces, complete = mine.list_traces("traces", "/repo", ["claude-code"], 2)
    assert not complete
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    evidence = mine.build_evidence([], keywords, "/repo", ["claude-code"], complete)
    assert set(evidence["truncated"]) == set(keywords)


def test_evidence_matches_every_topic_independently_of_order():
    failure = {
        "trace_id": "t1",
        "agent": "claude-code",
        "timestamp": 1,
        "command": "git push",
        "excerpt": "secret token expired",
    }
    keywords = {"a": ["secret"], "b": ["expir"]}
    assert mine.matching_topics(failure, keywords) == ["a", "b"]
    reordered = {"b": ["expir"], "a": ["secret"]}
    evidence_1 = mine.build_evidence([failure], keywords, "/repo", None)
    evidence_2 = mine.build_evidence([failure], reordered, "/repo", None)
    assert (
        [t["id"] for t in evidence_1["topics"]["b"]]
        == [t["id"] for t in evidence_2["topics"]["b"]]
        == ["t1"]
    )


def test_same_output_from_different_commands_are_distinct_failures(monkeypatch):
    events = [
        {
            "type": "tool_call",
            "callId": "a",
            "toolName": "Bash",
            "args": {"command": "git push archive-branch"},
            "eventNumber": 1,
        },
        {
            "type": "tool_result",
            "callId": "a",
            "toolName": "Bash",
            "status": "error",
            "output": "Permission denied",
            "timestamp": 1,
            "eventNumber": 2,
        },
        {
            "type": "tool_call",
            "callId": "b",
            "toolName": "Bash",
            "args": {"command": "cat secret.txt"},
            "eventNumber": 3,
        },
        {
            "type": "tool_result",
            "callId": "b",
            "toolName": "Bash",
            "status": "error",
            "output": "Permission denied",
            "timestamp": 2,
            "eventNumber": 4,
        },
    ]
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": events}))
    failures = mine.mine_trace("traces", {"id": "t1", "agentId": "claude-code"})
    assert len(failures) == 2
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    keywords["archive-branch"] = ["archive", "branch"]
    evidence = mine.build_evidence(failures, keywords, "/repo", None)
    assert [t["id"] for t in evidence["topics"]["archive-branch"]] == ["t1"]
    assert [t["id"] for t in evidence["topics"]["credential-redaction"]] == ["t1"]


def test_zero_failed_is_not_a_test_failure():
    passing = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "error",
        "output": "Exit code 1\n10 passed, 0 failed",
    }
    assert mine.failure_kind(passing) == "nonzero-exit"
    failing = {
        "type": "tool_result",
        "toolName": "Bash",
        "status": "error",
        "output": "Exit code 1\n2 failed, 8 passed",
    }
    assert mine.failure_kind(failing) == "test-failure"


def test_retired_topics_count_as_evidence_but_do_not_hide_blind_spots(
    tmp_path, monkeypatch, capsys
):
    # v2 had a "pytest-runs" topic that classified the pytest failure; v3
    # rolled it back. The failure is fresh field evidence for the retired
    # definition (so re-mining it is judged on real counts) AND a blind spot
    # of the current policy (so it can be mined again) (Codex, round 34).
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": _events()}))
    current = policy_mod.builtin_policy()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(current))
    v2 = policy_mod.builtin_policy()
    v2["topics"]["pytest-runs"] = {"keywords": ["failed"], "weight": 1.0}
    history = tmp_path / "h.jsonl"
    history.write_text(
        json.dumps({"version": 2, "policy": v2})
        + "\n"
        + json.dumps({"version": 3, "origin": "rollback", "policy": current})
        + "\n"
    )
    out = tmp_path / "failures.json"
    evidence = tmp_path / "evidence.json"
    code = mine.main(
        [
            "m",
            "--repo-dir",
            "/repo",
            "--policy",
            str(policy),
            "--history",
            str(history),
            "--out-json",
            str(out),
            "--save-evidence",
            str(evidence),
        ]
    )
    assert code == 0
    saved = json.loads(evidence.read_text())
    assert saved["definitions"]["pytest-runs"] == ["failed"]
    assert [t["id"] for t in saved["topics"]["pytest-runs"]] == ["t1"]
    report = json.loads(out.read_text())
    assert "pytest-runs" not in report["by_topic"]
    assert any("failed" in b["excerpt"] for b in report["blind_spots"])


def test_evidence_records_every_scanned_session_even_without_matches(monkeypatch):
    # A complete scan whose failures are all unclassified is an observed field
    # with confirmed zero counts, not an empty anchor (Codex, round 36).
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": _events(), "t2": []}))
    keywords = {"nothing-matches": ["zzzz-never"]}
    traces, complete = mine.list_traces("traces", "/repo", ["claude-code"], 50)
    failures = [f for t in traces for f in mine.mine_trace("traces", t)]
    evidence = mine.build_evidence(
        failures, keywords, "/repo", ["claude-code"], complete, scanned=[t["id"] for t in traces]
    )
    assert evidence["topics"] == {"nothing-matches": []}
    assert evidence["sessions"] == ["t1", "t2"]
    assert sys.modules["measure_policy_validity"].evidence_trace_ids(evidence) == {"t1", "t2"}
    result = sys.modules["measure_policy_validity"].measure(
        [
            {
                "round": 1,
                "findings": ["**[P1]** zzzz-never happened."],
                "occurred_at": "2026-09-14T15:00:00Z",
            }
        ],
        {
            "version": 1,
            "parent": None,
            "origin": "init",
            "threshold": 3,
            "topics": {"nothing-matches": {"keywords": ["zzzz-never"], "weight": 1.0}},
        },
        evidence,
    )
    assert result["current"]["anchor"] == {"nothing-matches": 0}
    assert result["anchor"]["traces_considered"] == 2


def test_namespace_mode_syncs_each_shared_session_before_reading_it(tmp_path, monkeypatch, capsys):
    calls: list[list[str]] = []

    def run(_bin, args):
        calls.append(list(args))
        if args[0] == "list":
            assert args[1] == "@gagan114" and "--all" in args
            return {
                "traces": [
                    {"id": "remote-1", "agentId": "claude-code", "timestamp": 5},
                    {"id": "remote-2", "agentId": "codex", "timestamp": 6},
                ]
            }
        if args[0] == "sync":
            return {"traceId": args[1]}
        if args[0] == "show":
            return {"events": _events() if args[1] == "remote-1" else []}
        raise AssertionError(args)

    monkeypatch.setattr(mine, "run_traces_json", run)
    monkeypatch.setattr(mine, "EXTRA_CLI_ARGS", [])
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    evidence = tmp_path / "evidence.json"
    code = mine.main(
        [
            "m",
            "--namespace",
            "gagan114",
            "--agents",
            "claude-code",
            "--traces-key",
            "tr_secret",
            "--policy",
            str(policy),
            "--history",
            str(tmp_path / "h.jsonl"),
            "--save-evidence",
            str(evidence),
        ]
    )
    assert code == 0
    assert mine.EXTRA_CLI_ARGS == ["--key", "tr_secret"]
    assert ["sync", "remote-1"] in calls
    assert not any(c[:2] == ["sync", "remote-2"] for c in calls), "codex sessions were filtered out"
    saved = json.loads(evidence.read_text())
    assert saved["namespace"] == "gagan114"
    assert saved["sessions"] == ["remote-1"]
    assert saved["listing_complete"] is True
    assert "tr_secret" not in capsys.readouterr().out


def test_secrets_in_failed_output_never_reach_reports_or_evidence(monkeypatch, tmp_path):
    events = [
        {
            "type": "tool_call",
            "callId": "c1",
            "args": {"command": "psql postgres://admin:FAKE_PW_123@db/app"},
        },
        {
            "type": "tool_result",
            "callId": "c1",
            "toolName": "Bash",
            "status": "error",
            "timestamp": 1,
            "eventNumber": 2,
            "output": "psql: error: connection to server failed: password authentication failed for user admin\nAPI_KEY=sk-livefakekey1234567890 was rejected",
        },
    ]
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": events}))
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    failures = mine.mine_trace("traces", {"id": "t1", "agentId": "claude-code"})
    lines, summary = mine.report(failures, keywords)
    evidence = mine.build_evidence(failures, keywords, "/repo", None)
    blob = "\n".join(lines) + json.dumps(summary) + json.dumps(evidence)
    assert "FAKE_PW_123" not in blob and "sk-livefakekey1234567890" not in blob
    assert "_text" not in json.dumps(summary)
    # The failure is still classifiable (credential-redaction keywords match)
    assert evidence["topics"]["credential-redaction"]


def test_repeated_failures_keep_every_occurrences_diagnostics_for_matching(monkeypatch):
    # Same header and same excerpt (the matched line plus the next one);
    # only the later diagnostics differ.
    head = "Exit code 1\njob crashed\n"
    events = [
        {"type": "tool_call", "callId": "c1", "args": {"command": "python3 job.py"}},
        {
            "type": "tool_result",
            "callId": "c1",
            "toolName": "Bash",
            "status": "error",
            "timestamp": 1,
            "eventNumber": 2,
            "output": head + "plain failure, nothing else",
        },
        {"type": "tool_call", "callId": "c2", "args": {"command": "python3 job.py"}},
        {
            "type": "tool_result",
            "callId": "c2",
            "toolName": "Bash",
            "status": "error",
            "timestamp": 2,
            "eventNumber": 4,
            "output": head + "later detail: the session expired, log in again",
        },
    ]
    monkeypatch.setattr(mine, "run_traces_json", _fake_runner({"t1": events}))
    failures = mine.mine_trace("traces", {"id": "t1", "agentId": "claude-code"})
    assert len(failures) == 1 and failures[0]["count"] == 2
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    assert "auth-lifecycle" in mine.matching_topics(failures[0], keywords)


def test_cli_timeouts_and_errors_never_expose_the_api_key(monkeypatch):
    import subprocess

    monkeypatch.setattr(mine, "EXTRA_CLI_ARGS", ["--key", "tr_SENTINEL_KEY"])

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(["traces", "list", "--key", "tr_SENTINEL_KEY"], 300)

    monkeypatch.setattr(mine.subprocess, "run", boom)
    with pytest.raises(mine.TracesCliError) as exc:
        mine.run_traces_json("traces", ["list", "@slug", "--all"])
    assert "tr_SENTINEL_KEY" not in str(exc.value) and "timed out" in str(exc.value)

    class Result:
        returncode = 1
        stderr = "auth failed for key tr_SENTINEL_KEY (Authorization: Bearer tr_SENTINEL_KEY)"

    monkeypatch.setattr(mine.subprocess, "run", lambda *_a, **_k: Result())
    with pytest.raises(mine.TracesCliError) as exc:
        mine.run_traces_json("traces", ["list", "@slug", "--all"])
    assert "tr_SENTINEL_KEY" not in str(exc.value)
