"""Tests for merge-archive-rounds.py."""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "merge-archive-rounds.py"
_spec = importlib.util.spec_from_file_location("merge_archive_rounds", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
merge_rounds = importlib.util.module_from_spec(_spec)
sys.modules["merge_archive_rounds"] = merge_rounds
_spec.loader.exec_module(merge_rounds)


def _round(n, sha, findings=("**[P2]** x.",)):
    return {
        "round": n,
        "source_sha": sha,
        "findings": list(findings),
        "occurred_at": f"2026-09-15T0{n % 10}:00:00Z",
    }


def test_rounds_are_appended_in_order_and_renumbered_after_the_base():
    base = [_round(1, "a"), _round(2, "b")]
    standing = [*base, _round(3, "c"), _round(4, "d")]
    merged, appended = merge_rounds.merge(base, standing, [_round(3, "e")])
    assert [e["source_sha"] for e in merged] == ["a", "b", "c", "d", "e"]
    assert [e["round"] for e in merged] == [1, 2, 3, 4, 5]
    assert appended == [3, 4, 5]


def test_a_round_already_merged_into_the_base_is_dropped_and_a_rerun_changes_nothing():
    base = [_round(1, "a"), _round(2, "c")]  # "c" merged into main meanwhile
    standing = [_round(1, "a"), _round(2, "c"), _round(3, "d")]
    merged, appended = merge_rounds.merge(base, standing, [_round(4, "d")])  # rerun for "d"
    assert [e["source_sha"] for e in merged] == ["a", "c", "d"]
    assert appended == [3]


def test_cli_reports_whether_the_standing_branch_would_change(tmp_path, capsys):
    base = tmp_path / "base.jsonl"
    base.write_text(json.dumps(_round(1, "a")) + "\n")
    standing = tmp_path / "standing.jsonl"
    standing.write_text(json.dumps(_round(1, "a")) + "\n" + json.dumps(_round(2, "b")) + "\n")
    new = tmp_path / "new.jsonl"
    new.write_text(json.dumps(_round(2, "b")) + "\n")
    out = tmp_path / "out.jsonl"
    assert merge_rounds.main(["m", str(base), str(standing), str(new), str(out)]) == 0
    result = json.loads(capsys.readouterr().out.strip())
    assert result == {"appended": 1, "changed": False, "rounds": [2]}
    new.write_text(json.dumps(_round(9, "z")) + "\n")
    assert merge_rounds.main(["m", str(base), str(standing), str(new), str(out)]) == 0
    result = json.loads(capsys.readouterr().out.strip())
    assert result["changed"] is True and result["rounds"] == [2, 3]
    assert [json.loads(line)["source_sha"] for line in out.read_text().splitlines()] == [
        "a",
        "b",
        "z",
    ]


def test_missing_standing_file_means_base_plus_new(tmp_path, capsys):
    base = tmp_path / "base.jsonl"
    base.write_text(json.dumps(_round(1, "a")) + "\n")
    new = tmp_path / "new.jsonl"
    new.write_text(json.dumps(_round(2, "b")) + "\n")
    out = tmp_path / "out.jsonl"
    assert merge_rounds.main(["m", str(base), str(tmp_path / "none"), str(new), str(out)]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {
        "appended": 1,
        "changed": True,
        "rounds": [2],
    }
