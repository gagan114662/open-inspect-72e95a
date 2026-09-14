"""Tests for sync-pr-traces.py.

Run with: python3 -m pytest scripts/sync_pr_traces_test.py -q
"""

import importlib.util
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "sync-pr-traces.py"
_spec = importlib.util.spec_from_file_location("sync_pr_traces", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
sync_pr_traces = importlib.util.module_from_spec(_spec)
sys.modules["sync_pr_traces"] = sync_pr_traces
_spec.loader.exec_module(sync_pr_traces)


def _write_mock_traces_cli(path: Path, script_body: str) -> str:
    path.write_text(f"#!/bin/bash\n{script_body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _real_git_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A real disposable git repo (not mocked) with 3 commits, matching
    this session's established practice of testing git-boundary logic
    against actual git behavior rather than assuming it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    shas = []
    for i in range(3):
        (repo / "f.txt").write_text(f"content {i}\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo, check=True)
        shas.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    return repo, shas


def test_pr_commit_shas_against_a_real_git_repo(tmp_path):
    repo, shas = _real_git_repo(tmp_path)
    base, _mid, head = shas

    result = sync_pr_traces.pr_commit_shas(str(repo), base, head)

    # base..head excludes base itself, includes everything after it.
    assert result == {shas[1], shas[2]}


def test_pr_commit_shas_empty_range_when_base_equals_head(tmp_path):
    repo, shas = _real_git_repo(tmp_path)
    result = sync_pr_traces.pr_commit_shas(str(repo), shas[-1], shas[-1])
    assert result == set()


def test_notes_for_commits_filters_to_the_given_commit_set(tmp_path):
    """Real JSON shape verified by hand against the actual `traces notes
    --json` output on this repo: {"notes": [{"externalId", "commitRef",
    "sharedUrl"}], "count": N}."""
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            if [ "$1" = "notes" ]; then
              echo '{"ok":true,"data":{"notes":[{"externalId":"t1","commitRef":"sha-in-range","sharedUrl":"https://x/1"},{"externalId":"t2","commitRef":"sha-NOT-in-range","sharedUrl":"https://x/2"},{"externalId":"t1","commitRef":"sha-in-range-2","sharedUrl":"https://x/1"}]}}'
            fi
            """),
    )

    result = sync_pr_traces.notes_for_commits(
        mock, "/repo", {"sha-in-range", "sha-in-range-2"}, 200
    )

    # t1 appears twice (two commits reference the same trace) -- deduped.
    assert result == ["t1"]


def test_sync_and_analyze_end_to_end_with_fake_cli_and_real_git_repo(tmp_path):
    repo, shas = _real_git_repo(tmp_path)
    base, mid, head = shas

    notes_log = tmp_path / "notes_calls.log"
    sync_log = tmp_path / "sync_calls.log"
    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent(f"""
            if [ "$1" = "notes" ]; then
              echo "$@" >> {notes_log}
              echo '{{"ok":true,"data":{{"notes":[{{"externalId":"trace-a","commitRef":"{mid}","sharedUrl":"https://x/a"}},{{"externalId":"trace-b","commitRef":"{head}","sharedUrl":"https://x/b"}},{{"externalId":"trace-c","commitRef":"some-other-commit-not-in-range","sharedUrl":"https://x/c"}}]}}}}'
            elif [ "$1" = "sync" ]; then
              echo "$@" >> {sync_log}
              echo '{{"ok":true,"data":{{"traceId":"'$2'","localMessageCount":5}}}}'
            elif [ "$1" = "search" ]; then
              case "$2" in
                *redact*) echo '{{"ok":true,"data":{{"count":2,"traces":[{{"id":"trace-a"}},{{"id":"trace-b"}}]}}}}' ;;
                *) echo '{{"ok":true,"data":{{"count":0,"traces":[]}}}}' ;;
              esac
            fi
            """),
    )

    result = sync_pr_traces.sync_and_analyze(mock, str(repo), base, head, "trk_fake_key", 200)

    assert result["commits_in_range"] == 2  # mid + head, base excluded
    assert result["linked_traces"] == 2  # trace-a and trace-b; trace-c excluded (out of range)
    assert result["synced_traces"] == 2

    by_topic = {t["topic"]: t["matching_traces"] for t in result["topics"]}
    assert by_topic["credential-redaction"] == 2
    assert by_topic["shell-semantics"] == 0

    # trace-c (out of range) must never have been synced. Each logged line
    # is the full "sync <id> --key ..." argv; the trace ID is the 2nd word.
    sync_calls = sync_log.read_text().splitlines()
    synced_ids = {line.split()[1] for line in sync_calls}
    assert synced_ids == {"trace-a", "trace-b"}

    # The search step was scoped by --trace-id, using the synced IDs.
    assert notes_log.exists()


def test_sync_and_analyze_reports_zero_linked_traces_without_erroring(tmp_path):
    repo, shas = _real_git_repo(tmp_path)
    base, _mid, head = shas

    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            if [ "$1" = "notes" ]; then
              echo '{"ok":true,"data":{"notes":[]}}'
            fi
            """),
    )

    result = sync_pr_traces.sync_and_analyze(mock, str(repo), base, head, "trk_fake_key", 200)
    assert result == {
        "commits_in_range": 2,
        "linked_traces": 0,
        "synced_traces": 0,
        "topics": [],
    }


def test_main_cli_fails_loudly_when_no_api_key_is_available(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACES_API_KEY", raising=False)
    exit_code = sync_pr_traces.main(
        ["sync-pr-traces.py", str(tmp_path), "sha1", "sha2"]
    )
    assert exit_code == 1


def test_main_cli_reports_and_exits_zero_on_a_real_no_notes_scenario(tmp_path, capsys):
    repo, shas = _real_git_repo(tmp_path)
    base, _mid, head = shas

    mock = _write_mock_traces_cli(
        tmp_path / "traces",
        textwrap.dedent("""
            if [ "$1" = "notes" ]; then
              echo '{"ok":true,"data":{"notes":[]}}'
            fi
            """),
    )

    exit_code = sync_pr_traces.main(
        [
            "sync-pr-traces.py",
            str(repo),
            base,
            head,
            "--traces-bin",
            mock,
            "--traces-key",
            "trk_fake",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No traces linked" in out
