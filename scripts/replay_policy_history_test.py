"""Tests for replay-policy-history.py: out-of-sample judgement per version."""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "replay-policy-history.py"
_spec = importlib.util.spec_from_file_location("replay_policy_history", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
replay = importlib.util.module_from_spec(_spec)
sys.modules["replay_policy_history"] = replay
_spec.loader.exec_module(replay)
policy_mod = sys.modules["improvement_policy"]


def _entries():
    return [
        {
            "round": 1,
            "source_sha": "a",
            "occurred_at": "2026-09-14T10:00:00Z",
            "findings": ["[P1] leaked credential in output"],
        },
        {
            "round": 2,
            "source_sha": "b",
            "occurred_at": "2026-09-14T12:00:00Z",
            "findings": ["[P2] quartz renderer crashed"],
        },
        {
            "round": 3,
            "source_sha": "c",
            "occurred_at": "2026-09-14T14:00:00Z",
            "findings": ["[P2] quartz renderer crashed again"],
        },
        {
            "round": 4,
            "source_sha": "d",
            "occurred_at": "2026-09-14T16:00:00Z",
            "findings": ["[P1] secret token exposed"],
        },
    ]


def _versions():
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1,
        topics={**v1["topics"], "quartz-crashes": {"keywords": ["quartz"], "weight": 1.0}},
        threshold=v1["threshold"],
        origin="revision",
        rationale="mined",
        created_at="2026-09-14T13:00:00Z",
    )
    return v1, v2


def test_each_version_is_judged_only_on_rounds_after_it_existed():
    v1, v2 = _versions()
    history = [{"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)}]
    result = replay.replay(_entries(), v2, history, None)
    rows = {r["version"]: r for r in result["versions"]}
    assert rows[1]["rounds_oos"] == 4, "v1 predates every round"
    assert rows[2]["rounds_oos"] == 2, "v2 was created at 13:00; rounds 3 and 4 come after"
    # v2 classifies the later quartz round that v1 could not, so its
    # out-of-sample coverage is higher than v1's over the same period.
    assert rows[2]["coverage_oos"] == 1.0
    assert rows[1]["coverage_oos"] < 1.0
    assert rows[2]["coverage_all"] == 1.0 and rows[1]["coverage_all"] == 0.5


def test_replay_includes_the_current_policy_when_history_lacks_it(tmp_path, capsys):
    _v1, v2 = _versions()
    versions = replay.versions_in_order(v2, [])
    assert [v["version"] for v in versions] == [1, 2]
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _entries()) + "\n")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(v2))
    out = tmp_path / "replay.json"
    assert (
        replay.main(
            [
                "r",
                str(archive),
                "--policy",
                str(policy),
                "--history",
                str(tmp_path / "h"),
                "--out-json",
                str(out),
            ]
        )
        == 0
    )
    saved = json.loads(out.read_text())
    assert [r["version"] for r in saved["versions"]] == [1, 2]
    assert "out-of-sample replay" in capsys.readouterr().out


def test_out_json_is_guarded_like_every_other_output(tmp_path):
    import pytest

    archive = tmp_path / "archive.jsonl"
    archive.write_text("")
    with pytest.raises(PermissionError):
        replay.main(
            ["r", str(archive), "--history", str(tmp_path / "h"), "--out-json", str(archive)]
        )


def test_undated_findings_never_enter_a_held_out_set():
    """Codex review of PR #70, round 3: an undated entry used to inherit its
    round's earliest time, so a finding that informed a revision could be
    counted as unseen evidence once a later, dated result line arrived.
    Undated entries are now excluded from every held-out set and counted."""
    v1, v2 = _versions()
    history = [{"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)}]
    entries = [
        *_entries(),
        # Same round as entry 3 (after the v2 cutoff), but its own time is unknown.
        {"round": 3, "source_sha": "c", "findings": ["[P2] quartz renderer leaked a secret"]},
        # A whole round with no time at all.
        {"round": 5, "source_sha": "e", "findings": ["[P1] token exposed in log"]},
    ]
    result = replay.replay(entries, v2, history, None)
    rows = {r["version"]: r for r in result["versions"]}
    assert rows[2]["rounds_oos"] == 2, "undated round 5 is not later evidence for v2"
    assert rows[2]["findings_oos"] == 2, "the undated finding of round 3 is excluded too"
    assert result["undated_excluded"] == 2
    assert result["common_rounds"] == 2
    kept = replay.rounds_after(entries, "2026-09-14T13:00:00Z")
    assert all(e.get("occurred_at") for e in kept)
