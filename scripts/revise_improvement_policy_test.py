"""Tests for revise-improvement-policy.py.

Run with: python3 -m pytest scripts/revise_improvement_policy_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parent / "revise-improvement-policy.py"
_spec = importlib.util.spec_from_file_location("revise_improvement_policy", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
revise = importlib.util.module_from_spec(_spec)
sys.modules["revise_improvement_policy"] = revise
_spec.loader.exec_module(revise)
policy_mod = sys.modules["improvement_policy"]
measure = sys.modules["measure_policy_validity"]

NOW = "2026-09-14T19:00:00Z"


def _archive():
    return [
        {
            "round": 1,
            "occurred_at": "2026-09-14T15:00:00Z",
            "findings": ["[P1] Secret leaked into logs."],
        },
        {
            "round": 2,
            "occurred_at": "2026-09-14T16:00:00Z",
            "findings": [
                "[P2] Archive concurrency drops queued rounds.",
                "[P2] Token exposed in a comment.",
            ],
        },
        {
            "round": 3,
            "occurred_at": "2026-09-14T17:00:00Z",
            "findings": [
                "[P2] Archive threshold crossings are permanently missed under concurrency.",
                "[P2] Archive PR creation cannot recover after a partial failure.",
            ],
        },
    ]


def _measurement(policy, evidence=None):
    return measure.measure(_archive(), policy, evidence)


def test_no_revision_when_coverage_and_validity_hold():
    policy = policy_mod.builtin_policy()
    policy["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    decision = revise.decide(_archive(), policy, [], _measurement(policy), NOW)
    assert decision["action"] == "none"


def test_low_coverage_triggers_a_bounded_mined_revision():
    policy = policy_mod.builtin_policy()
    measurement = _measurement(policy)
    assert measurement["current"]["coverage"] < revise.MIN_COVERAGE
    decision = revise.decide(_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "revise"
    revised = decision["policy"]
    assert revised["version"] == 2 and revised["parent"] == 1 and revised["origin"] == "revision"
    new_names = [t for t in revised["topics"] if t not in policy["topics"]]
    assert 1 <= len(new_names) <= revise.MAX_NEW_TOPICS
    assert any("archive" in revised["topics"][n]["keywords"] for n in new_names)
    # Existing classifications are untouched: new topics come after the old ones.
    assert list(revised["topics"])[: len(policy["topics"])] == list(policy["topics"])
    assert decision["coverage_after"] > decision["coverage_before"]
    assert all(
        len(revised["topics"][n]["mined_from"]) >= revise.MIN_FINDINGS_PER_TOPIC for n in new_names
    )


def test_low_validity_discounts_topics_the_field_never_shows():
    policy = policy_mod.builtin_policy()
    policy["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    evidence = {
        "source": "traces",
        "agents": ["claude-code"],
        "topics": {
            "credential-redaction": [],
            "shell-semantics": [{"id": "s1", "agentId": "claude-code", "timestamp": 1}],
            "env-var-precedence": [
                {"id": "e1", "agentId": "claude-code", "timestamp": 1},
                {"id": "e2", "agentId": "claude-code", "timestamp": 1},
            ],
            "fork-pr-permissions": [],
            "auth-lifecycle": [],
            "archive-ops": [],
        },
    }
    measurement = _measurement(policy, evidence)
    assert measurement["current"]["coverage"] == 1.0
    assert measurement["current"]["validity"] < revise.MIN_VALIDITY
    decision = revise.decide(_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "revise"
    assert decision["policy"]["topics"]["credential-redaction"]["weight"] == 0.5
    assert decision["policy"]["topics"]["archive-ops"]["weight"] == 0.5
    assert decision["policy"]["topics"]["shell-semantics"]["weight"] == 1.0


def test_rollback_when_an_adopted_revision_is_worse_than_its_parent():
    parent = policy_mod.builtin_policy()
    parent["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    # A bad revision that replaced the archive topic with one that matches nothing.
    bad_topics = {k: v for k, v in parent["topics"].items() if k != "archive-ops"}
    bad_topics["nothing"] = {"keywords": ["zzzz"], "weight": 1.0}
    bad = policy_mod.new_version(
        parent,
        topics=bad_topics,
        threshold=3,
        origin="revision",
        rationale="oops",
        created_at="2026-09-14T15:30:00Z",
    )
    history = [
        {"version": 1, "policy": parent},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": bad},
    ]
    measurement = _measurement(bad)
    decision = revise.decide(_archive(), bad, history, measurement, NOW)
    assert decision["action"] == "rollback"
    assert decision["policy"]["origin"] == "rollback"
    assert decision["policy"]["version"] == 3
    assert "archive-ops" in decision["policy"]["topics"]


def test_rollback_waits_for_enough_rounds_to_judge():
    parent = policy_mod.builtin_policy()
    bad = policy_mod.new_version(
        parent,
        topics=parent["topics"],
        threshold=3,
        origin="revision",
        rationale="x",
        created_at="2026-09-14T16:30:00Z",
    )
    history = [
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": bad}
    ]
    decision = revise.decide(_archive(), bad, history, _measurement(bad), NOW)
    assert decision["action"] != "rollback"


def test_main_refuses_a_measurement_taken_under_a_different_policy(tmp_path, capsys):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    stale = _measurement(policy_mod.builtin_policy())
    stale["policy_hash"] = "deadbeef0000"
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(stale))
    assert (
        revise.main(
            [
                "r",
                str(archive),
                "--measurement",
                str(m_path),
                "--policy",
                str(policy_path),
                "--history",
                str(tmp_path / "h.jsonl"),
                "--dry-run",
            ]
        )
        == 1
    )
    assert "re-measure first" in capsys.readouterr().err


def test_main_writes_only_ai_owned_files(tmp_path, capsys, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(_measurement(policy_mod.builtin_policy())))
    history = tmp_path / "history.jsonl"
    # Outside the allowlist, the guard refuses to write.
    with pytest.raises(PermissionError):
        revise.main(
            [
                "r",
                str(archive),
                "--measurement",
                str(m_path),
                "--policy",
                str(policy_path),
                "--history",
                str(history),
                "--now",
                NOW,
            ]
        )
    monkeypatch.setattr(
        policy_mod,
        "AI_OWNED_COMPONENTS",
        {
            "policy": policy_mod.relative_to_repo(policy_path),
            "history": policy_mod.relative_to_repo(history),
        },
    )
    assert (
        revise.main(
            [
                "r",
                str(archive),
                "--measurement",
                str(m_path),
                "--policy",
                str(policy_path),
                "--history",
                str(history),
                "--now",
                NOW,
            ]
        )
        == 0
    )
    written = json.loads(policy_path.read_text())
    assert written["version"] == 2
    entries = policy_mod.load_history(history)
    assert entries[-1]["version"] == 2 and entries[-1]["policy"] == written
    assert "REVISION -> policy v2" in capsys.readouterr().out
