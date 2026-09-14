"""Tests for analyze-traces.py.

Run with: python3 -m pytest scripts/analyze_traces_test.py -q
"""

import importlib.util
import json
import stat
import sys
import textwrap
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "analyze-traces.py"
_spec = importlib.util.spec_from_file_location("analyze_traces", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
analyze_traces = importlib.util.module_from_spec(_spec)
sys.modules["analyze_traces"] = analyze_traces
_spec.loader.exec_module(analyze_traces)


def _write_mock_traces_cli(path: Path, script_body: str) -> str:
    path.write_text(f"#!/bin/bash\n{script_body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_list_repo_trace_ids_parses_real_cli_json_shape(tmp_path):
    """Shape taken directly from a real `traces list --json` invocation
    against this repo's own local trace data, not invented."""
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            echo '{"ok":true,"data":{"traces":[{"id":"abc123","agentId":"codex"},{"id":"def456","agentId":"claude-code"}],"count":2}}'
            """),
    )
    ids = analyze_traces.list_repo_trace_ids(mock, "/some/repo", 200)
    assert ids == ["abc123", "def456"]


def test_search_topic_parses_real_cli_json_shape(tmp_path):
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            echo '{"ok":true,"data":{"count":1,"traces":[{"id":"xyz789","matchCount":2}]}}'
            """),
    )
    matches = analyze_traces.search_topic(mock, "redact|credential", ["--dir", "/some/repo"])
    assert matches == [{"id": "xyz789", "matchCount": 2}]


def test_raises_on_cli_failure_rather_than_silently_reporting_zero(tmp_path):
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            echo "not authenticated" >&2
            exit 1
            """),
    )
    import pytest

    with pytest.raises(analyze_traces.TracesCliError):
        analyze_traces.list_repo_trace_ids(mock, "/some/repo", 200)


def test_raises_on_ok_false_payload_rather_than_treating_it_as_empty(tmp_path):
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            echo '{"ok":false,"error":"boom"}'
            """),
    )
    import pytest

    with pytest.raises(analyze_traces.TracesCliError):
        analyze_traces.list_repo_trace_ids(mock, "/some/repo", 200)


def test_analyze_end_to_end_with_a_fake_cli_reproducing_real_repo_shape(tmp_path):
    """End-to-end test of analyze() against a fake `traces` CLI whose
    responses are shaped exactly like the real tool's output for this repo:
    3 codex-review traces, all matching the credential-redaction topic
    (confirmed manually against the real local trace data before writing
    this test), zero for every other topic."""
    log_path = tmp_path / "calls.log"
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent(f"""
            echo "$@" >> {log_path}
            if [ "$1" = "list" ]; then
              echo '{{"ok":true,"data":{{"traces":[{{"id":"t1"}},{{"id":"t2"}},{{"id":"t3"}}],"count":3}}}}'
            elif [ "$1" = "show" ]; then
              echo '{{"ok":true,"data":{{}}}}'
            elif [ "$1" = "search" ]; then
              case "$2" in
                *redact*)
                  echo '{{"ok":true,"data":{{"count":3,"traces":[{{"id":"t1"}},{{"id":"t2"}},{{"id":"t3"}}]}}}}'
                  ;;
                *)
                  echo '{{"ok":true,"data":{{"count":0,"traces":[]}}}}'
                  ;;
              esac
            fi
            """),
    )

    result = analyze_traces.analyze(mock, "/repo", limit=200)

    assert result["traces_scanned"] == 3
    by_topic = {t["topic"]: t["matching_traces"] for t in result["topics"]}
    assert by_topic["credential-redaction"] == 3
    assert by_topic["shell-semantics"] == 0
    assert by_topic["env-var-precedence"] == 0

    # Every trace was hydrated (shown) before searching, and every topic was
    # actually searched -- not skipped or short-circuited.
    calls = log_path.read_text().splitlines()
    show_calls = [c for c in calls if c.startswith("show ")]
    search_calls = [c for c in calls if c.startswith("search ")]
    assert len(show_calls) == 3
    assert len(search_calls) == len(analyze_traces.detect_mod.TOPIC_KEYWORDS)


def test_main_cli_reports_topics_sorted_by_match_count(tmp_path, capsys):
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            if [ "$1" = "list" ]; then
              echo '{"ok":true,"data":{"traces":[],"count":0}}'
            elif [ "$1" = "search" ]; then
              case "$2" in
                *redact*) echo '{"ok":true,"data":{"count":2,"traces":[{"id":"a"},{"id":"b"}]}}' ;;
                *) echo '{"ok":true,"data":{"count":0,"traces":[]}}' ;;
              esac
            fi
            """),
    )
    exit_code = analyze_traces.main(
        ["analyze-traces.py", "/repo", "--traces-bin", mock]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out.split("---\n", 1)[1])
    assert payload["topics"][0]["topic"] == "credential-redaction"
    assert payload["topics"][0]["matching_traces"] == 2


def test_main_cli_fails_loudly_when_traces_binary_is_missing():
    exit_code = analyze_traces.main(
        ["analyze-traces.py", "/repo", "--traces-bin", "/nonexistent/traces-binary-xyz"]
    )
    assert exit_code == 1
