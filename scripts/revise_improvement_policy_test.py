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
    stamped = [{**e, "policy_hash": policy_mod.policy_hash(bad)} for e in _archive()]
    measurement = measure.measure(stamped, bad, None)
    decision = revise.decide(stamped, bad, history, measurement, NOW)
    assert decision["action"] == "rollback"
    assert decision["policy"]["origin"] == "rollback"
    assert decision["policy"]["version"] == 3
    assert "archive-ops" in decision["policy"]["topics"]


def test_no_rollback_when_unfamiliar_findings_lower_both_policies():
    parent = policy_mod.builtin_policy()
    child_topics = {**parent["topics"], "archive-ops": {"keywords": ["archive"], "weight": 1.0}}
    child = policy_mod.new_version(
        parent,
        topics=child_topics,
        threshold=3,
        origin="revision",
        rationale="coverage repair",
        created_at="2026-09-14T15:30:00Z",
    )
    history = [
        {
            "version": 2,
            "parent": 1,
            "origin": "revision",
            "coverage_before": 0.4,
            "coverage_after": 1.0,
            "policy": child,
        }
    ]
    noisy = _archive() + [
        {
            "round": 4,
            "occurred_at": "2026-09-14T18:00:00Z",
            "findings": [f"[P2] Unfamiliar problem number {i}." for i in range(20)],
        },
    ]
    measurement = measure.measure(noisy, child, None)
    assert measurement["current"]["coverage"] < 0.4  # far below the parent's historical number
    decision = revise.decide(noisy, child, history, measurement, NOW)
    assert decision["action"] != "rollback"


def test_mined_topics_never_share_a_supporting_finding():
    unclassified = [
        {"round": 1, "finding": "Archive queue drops rounds under concurrency."},
        {"round": 2, "finding": "Archive branch left behind after commit failure."},
        {"round": 3, "finding": "Archive threshold missed; commit compare skipped."},
        {"round": 4, "finding": "Concurrency setting cancels pending queue entries."},
        {"round": 5, "finding": "Concurrency group drops a queued run."},
    ]
    mined = revise.mine_topics(unclassified, policy_mod.topic_keywords(policy_mod.builtin_policy()))
    claimed = [e["finding"] for m in mined for e in m["evidence"]]
    assert len(claimed) == len(set(claimed)), "a finding supported two topics"
    for m in mined:
        assert len(m["evidence"]) >= revise.MIN_FINDINGS_PER_TOPIC
        # Every claimed finding really is classified by that topic's keywords.
        for e in m["evidence"]:
            assert policy_mod.classify_finding(e["finding"], {"_": m["keywords"]}) is not None


def _four_topic_policy(weights):
    policy = policy_mod.builtin_policy()
    names = ["credential-redaction", "shell-semantics", "env-var-precedence", "fork-pr-permissions"]
    policy["topics"] = {
        n: {**policy["topics"][n], "weight": w} for n, w in zip(names, weights, strict=True)
    }
    return policy


def _four_topic_archive():
    # recurrence per topic: credential 2, shell 1, env 2, fork 1
    return [
        {
            "round": 1,
            "occurred_at": "2026-09-14T15:00:00Z",
            "findings": ["[P1] Secret leaked.", "[P2] Precedence of env var wrong."],
        },
        {
            "round": 2,
            "occurred_at": "2026-09-14T16:00:00Z",
            "findings": [
                "[P2] Token exposed.",
                "[P2] Shell exit code ignored.",
                "[P2] Fork PR lacks github_token.",
            ],
        },
        {
            "round": 3,
            "occurred_at": "2026-09-14T17:00:00Z",
            "findings": ["[P2] Environment variable applied unconditionally."],
        },
    ]


def _four_topic_evidence(counts):
    names = ["credential-redaction", "shell-semantics", "env-var-precedence", "fork-pr-permissions"]
    return {
        "source": "traces",
        "agents": ["claude-code"],
        "topics": {
            n: [{"id": f"{n}-{i}", "agentId": "claude-code", "timestamp": 1} for i in range(c)]
            for n, c in zip(names, counts, strict=True)
        },
    }


def test_weight_repair_is_dropped_when_it_would_lower_validity():
    # Validity trigger fires (0.0 < MIN_VALIDITY); restoring credential-redaction's
    # weight to 1.0 would move validity to -0.0556, so the reweighting is refused.
    policy = _four_topic_policy([0.5, 1, 1, 1])
    measurement = measure.measure(_four_topic_archive(), policy, _four_topic_evidence([1, 0, 2, 2]))
    before = measurement["current"]["validity"]
    assert before is not None and before < revise.MIN_VALIDITY
    decision = revise.decide(_four_topic_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "none"
    assert "no bounded, evidence-backed change" in decision["reason"]
    restored = revise.validity_under(
        _four_topic_policy([1, 1, 1, 1]), _four_topic_archive(), measurement["current"]["anchor"]
    )
    assert restored < before


def test_reweighting_that_undefines_validity_is_refused():
    # Codex round-3 counterexample: discounting the only topic with a distinct
    # weighted recurrence makes the weighted signal constant, so validity would
    # go from a number to None. That must not pass as an improvement.
    policy = _four_topic_policy([1, 1, 1, 1])
    entries = _four_topic_archive()
    anchor = {
        "credential-redaction": 0,
        "shell-semantics": 1,
        "env-var-precedence": 2,
        "fork-pr-permissions": 3,
    }
    before = revise.validity_under(policy, entries, anchor)
    assert before is not None
    assert revise.validity_regressed(before, None) is True
    assert revise.validity_regressed(None, None) is False
    assert revise.validity_regressed(0.2, 0.2) is False


def test_corroborated_topic_regains_weight_even_when_scores_are_healthy():
    policy = _four_topic_policy([0.5, 1, 1, 1])
    evidence = _four_topic_evidence(
        [3, 1, 3, 1]
    )  # field strongly corroborates credential-redaction
    measurement = measure.measure(_four_topic_archive(), policy, evidence)
    assert measurement["current"]["coverage"] == 1.0
    assert (
        measurement["current"]["validity"] is not None
        and measurement["current"]["validity"] >= revise.MIN_VALIDITY
    )
    decision = revise.decide(_four_topic_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "revise"
    assert decision["policy"]["topics"]["credential-redaction"]["weight"] == 1.0
    assert "corroborates" in decision["reason"]
    assert (
        decision["validity_after"] is not None
        and decision["validity_after"] >= measurement["current"]["validity"]
    )


def test_rollback_on_validity_regression_with_same_coverage():
    parent = _four_topic_policy([1, 1, 1, 1])
    child = policy_mod.new_version(
        parent,
        topics=_four_topic_policy([0.25, 1, 1, 1])["topics"],
        threshold=3,
        origin="revision",
        rationale="discount",
        created_at="2026-09-14T14:00:00Z",
    )
    history = [
        {"version": 1, "policy": parent},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": child},
    ]
    evidence = _four_topic_evidence([3, 1, 2, 0])  # the field supports credential-redaction
    stamped = [{**e, "policy_hash": policy_mod.policy_hash(child)} for e in _four_topic_archive()]
    measurement = measure.measure(stamped, child, evidence)
    decision = revise.decide(stamped, child, history, measurement, NOW)
    assert decision["action"] == "rollback"
    assert "validity" in decision["reason"]


def test_main_refuses_a_measurement_from_a_different_archive(tmp_path, capsys):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(
        json.dumps(measure.measure(_archive()[:-1], policy_mod.builtin_policy(), None))
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
                str(tmp_path / "h.jsonl"),
                "--dry-run",
            ]
        )
        == 1
    )
    assert "archive digest" in capsys.readouterr().err


def test_rollback_waits_for_rounds_decided_under_the_revision():
    parent = policy_mod.builtin_policy()
    parent["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    bad_topics = {k: v for k, v in parent["topics"].items() if k != "archive-ops"}
    bad = policy_mod.new_version(
        parent,
        topics=bad_topics,
        threshold=3,
        origin="revision",
        rationale="x",
        created_at="2026-09-14T14:00:00Z",
    )
    history = [
        {"version": 1, "policy": parent},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": bad},
    ]
    # Rounds after the proposal's timestamp but stamped with the PARENT's hash
    # (the PR was still open) do not count toward judging the revision.
    under_parent = [{**e, "policy_hash": policy_mod.policy_hash(parent)} for e in _archive()]
    decision = revise.decide(
        under_parent, bad, history, measure.measure(under_parent, bad, None), NOW
    )
    assert decision["action"] != "rollback"
    assert revise.rounds_under(under_parent, bad) == 0
    one_under = under_parent[:-1] + [{**_archive()[-1], "policy_hash": policy_mod.policy_hash(bad)}]
    assert revise.rounds_under(one_under, bad) == 1
    decision = revise.decide(one_under, bad, history, measure.measure(one_under, bad, None), NOW)
    assert decision["action"] != "rollback"


def test_accepted_revision_never_regresses_validity():
    policy = _four_topic_policy([0.5, 1, 1, 1])
    measurement = measure.measure(_four_topic_archive(), policy, _four_topic_evidence([3, 1, 3, 1]))
    decision = revise.decide(_four_topic_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "revise"
    assert not revise.validity_regressed(
        measurement["current"]["validity"], decision["validity_after"]
    )


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
