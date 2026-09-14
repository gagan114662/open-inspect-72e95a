"""Tests for mine-trace-failures.py.

Run with: python3 -m pytest scripts/mine_trace_failures_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

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
            "output": "Exit code 1\n3 failed, 10 passed",
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
            "status": "success",
            "output": "! [rejected] main -> main (non-fast-forward)",
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
    traces = mine.list_traces("traces", "/repo", ["claude-code"], 50)
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
