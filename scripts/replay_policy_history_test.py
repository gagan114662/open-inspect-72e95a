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


def _evidence_for(policy, timestamps: list[int]) -> dict:
    keywords = policy_mod.topic_keywords(policy)
    return {
        "source": "traces",
        "collected_at": "2026-09-14T23:00:00Z",
        "sessions": [f"s{i}" for i in range(len(timestamps))],
        "definitions": {t: list(w) for t, w in keywords.items()},
        "topics": {
            t: [
                {"id": f"s{i}", "agentId": "claude-code", "timestamp": ts, "timestamps": [ts]}
                for i, ts in enumerate(timestamps)
            ]
            for t in keywords
        },
    }


def test_held_out_validity_only_counts_field_evidence_after_the_revision():
    """Codex review of PR #70, round 4: review entries were filtered by the
    cutoff but the whole evidence snapshot was passed to measure(), so field
    traces that informed the revision counted towards its held-out validity."""
    v1, v2 = _versions()
    history = [{"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)}]
    cutoff = replay.measure_mod.parse_timestamp_ms("2026-09-14T13:00:00Z")
    before_only = _evidence_for(v2, [cutoff - 3_600_000, cutoff - 60_000])
    rows = {
        r["version"]: r for r in replay.replay(_entries(), v2, history, before_only)["versions"]
    }
    assert rows[2]["validity_oos"] is None, "no field trace was observed after v2 existed"
    assert rows[2]["validity_common"] is None
    mixed = _evidence_for(v2, [cutoff - 60_000, cutoff + 60_000])
    kept = replay.evidence_after(mixed, cutoff)
    assert all(len(traces) == 1 and traces[0]["id"] == "s1" for traces in kept["topics"].values())
    assert kept["sessions"] == ["s1"]
    # Codex review of PR #70, round 5: a session that matched at t=100 and
    # again at t=300 is still evidence after a cutoff at t=200.
    recurring = _evidence_for(v2, [cutoff - 60_000])
    for traces in recurring["topics"].values():
        traces[0]["timestamps"] = [cutoff - 60_000, cutoff + 60_000]
    kept = replay.evidence_after(recurring, cutoff)
    assert all(len(traces) == 1 for traces in kept["topics"].values())
    # An older snapshot without the per-match list cannot place the session.
    legacy = _evidence_for(v2, [cutoff + 60_000])
    legacy["topics"]["quartz-crashes"][0].pop("timestamps")
    assert "quartz-crashes" not in replay.evidence_after(legacy, cutoff)["topics"]
    undated = _evidence_for(v2, [cutoff + 60_000])
    undated["topics"]["quartz-crashes"][0]["timestamps"] = [None]
    assert "quartz-crashes" not in replay.evidence_after(undated, cutoff)["topics"], (
        "a trace that cannot be placed in time makes the topic's held-out count unknown"
    )
    assert replay.evidence_after(mixed, None) == mixed, "the root version keeps every trace"


def test_a_revision_without_a_valid_timestamp_has_no_held_out_metrics():
    """Codex review of PR #70, round 4: a non-root revision whose created_at
    was missing or unparseable received every round as 'held out' and, as
    the newest version, defined a common window that let training findings
    in. Its held-out metrics are now unavailable and the common comparison
    is not built on it."""
    v1, v2 = _versions()
    history = [{"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)}]
    for created in (None, "", "not a time"):
        broken = dict(v2)
        if created is None:
            broken.pop("created_at", None)
        else:
            broken["created_at"] = created
        hist = [{**history[0], "policy": broken}]
        result = replay.replay(_entries(), broken, hist, None)
        rows = {r["version"]: r for r in result["versions"]}
        assert rows[2]["rounds_oos"] == 0 and rows[2]["coverage_oos"] is None, created
        assert rows[2]["held_out"] == "unavailable: no valid created_at", created
        assert result["common_rounds"] == 0 and result["common_window_after"] is None, created
        assert rows[1]["rounds_oos"] == 4, "the root version still predates the archive"
