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
        "definitions": {**policy_mod.BUILTIN_TOPIC_KEYWORDS, "archive-ops": ["archive"]},
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
    stamped = [
        {**e, "policy_hash": policy_mod.policy_hash(bad), "policy_version": bad["version"]}
        for e in _archive()
    ]
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
    noisy = [
        *_archive(),
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
        "definitions": {n: policy_mod.BUILTIN_TOPIC_KEYWORDS[n] for n in names},
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
    stamped = [
        {**e, "policy_hash": policy_mod.policy_hash(child), "policy_version": child["version"]}
        for e in _four_topic_archive()
    ]
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
    under_parent = [
        {**e, "policy_hash": policy_mod.policy_hash(parent), "policy_version": parent["version"]}
        for e in _archive()
    ]
    decision = revise.decide(
        under_parent, bad, history, measure.measure(under_parent, bad, None), NOW
    )
    assert decision["action"] != "rollback"
    assert revise.rounds_under(under_parent, bad) == 0
    one_under = [
        *under_parent[:-1],
        {
            **_archive()[-1],
            "policy_hash": policy_mod.policy_hash(bad),
            "policy_version": bad["version"],
        },
    ]
    assert revise.rounds_under(one_under, bad) == 1
    decision = revise.decide(one_under, bad, history, measure.measure(one_under, bad, None), NOW)
    assert decision["action"] != "rollback"


def test_no_new_revision_until_the_current_one_has_been_judged():
    parent = policy_mod.builtin_policy()
    child = policy_mod.new_version(
        parent,
        topics=parent["topics"],
        threshold=3,
        origin="revision",
        rationale="x",
        created_at="2026-09-14T14:00:00Z",
    )
    history = [
        {"version": 1, "policy": parent},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 0.5, "policy": child},
    ]
    one_under = [
        *_archive()[:-1],
        {
            **_archive()[-1],
            "policy_hash": policy_mod.policy_hash(child),
            "policy_version": child["version"],
        },
    ]
    measurement = measure.measure(one_under, child, None)
    assert (
        measurement["current"]["coverage"] < revise.MIN_COVERAGE
    )  # would otherwise trigger mining
    decision = revise.decide(one_under, child, history, measurement, NOW)
    assert decision["action"] == "none"
    assert "waiting for" in decision["reason"]


def test_discounts_wait_for_fresh_evidence():
    policy = _four_topic_policy([1, 1, 1, 1])
    evidence = _four_topic_evidence([0, 1, 2, 2])
    evidence["collected_at"] = "2026-09-14T15:30:00Z"  # rounds 2 and 3 are newer
    measurement = measure.measure(_four_topic_archive(), policy, evidence)
    assert measurement["current"]["rounds_after_evidence"] == 2
    decision = revise.decide(_four_topic_archive(), policy, [], measurement, NOW)
    assert decision["action"] == "none"
    assert all(spec.get("weight", 1.0) == 1.0 for spec in policy["topics"].values())


def test_rounds_under_requires_matching_version_not_just_contents():
    parent = policy_mod.builtin_policy()
    same_contents_later = policy_mod.new_version(
        parent,
        topics=parent["topics"],
        threshold=parent["threshold"],
        origin="revision",
        rationale="recreated",
        created_at="2026-09-14T18:00:00Z",
    )
    assert policy_mod.policy_hash(same_contents_later) == policy_mod.policy_hash(parent)
    stamped_under_v1 = [
        {**e, "policy_hash": policy_mod.policy_hash(parent), "policy_version": 1}
        for e in _archive()
    ]
    assert revise.rounds_under(stamped_under_v1, parent) == 3
    assert revise.rounds_under(stamped_under_v1, same_contents_later) == 0


def test_validity_rollback_ignores_rounds_newer_than_the_evidence():
    parent = _four_topic_policy([1, 1, 1, 1])
    child = policy_mod.new_version(
        parent,
        topics=_four_topic_policy([0.25, 1, 1, 1])["topics"],
        threshold=3,
        origin="revision",
        rationale="d",
        created_at="2026-09-14T14:00:00Z",
    )
    history = [
        {"version": 1, "policy": parent},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": child},
    ]
    evidence = _four_topic_evidence([3, 1, 2, 0])
    evidence["collected_at"] = "2026-09-14T14:30:00Z"  # before every round in the archive
    stamped = [
        {**e, "policy_hash": policy_mod.policy_hash(child), "policy_version": 2}
        for e in _four_topic_archive()
    ]
    measurement = measure.measure(stamped, child, evidence)
    decision = revise.decide(stamped, child, history, measurement, NOW)
    # With no covered rounds there is no validity to compare, so no validity rollback.
    assert not (decision["action"] == "rollback" and "validity" in decision["reason"])
    assert revise.entries_covered_by_evidence(stamped, measurement) == []


def test_candidate_acceptance_and_rollback_use_the_same_evidence_window():
    policy = _four_topic_policy([0.5, 1, 1, 1])
    evidence = _four_topic_evidence([3, 1, 3, 1])
    evidence["collected_at"] = "2026-09-14T14:30:00Z"  # predates every round
    measurement = measure.measure(_four_topic_archive(), policy, evidence)
    decision = revise.decide(_four_topic_archive(), policy, [], measurement, NOW)
    # No covered rounds: there is no validity to judge a restoration on, so
    # the candidate is neither accepted on later findings nor rolled back later.
    assert decision["action"] == "none" or decision.get("validity_after") is None


def test_rolled_back_configuration_is_not_retried_on_the_same_evidence():
    parent = policy_mod.builtin_policy()
    measurement = measure.measure(_archive(), parent, None)
    first = revise.decide(_archive(), parent, [], measurement, NOW)
    assert first["action"] == "revise"
    rejected_hash = policy_mod.policy_hash(first["policy"])
    history = [
        {"version": 1, "policy": parent},
        {
            "version": 2,
            "parent": 1,
            "origin": "revision",
            "coverage_before": 0.4,
            "policy": first["policy"],
        },
        {
            "version": 3,
            "parent": 2,
            "origin": "rollback",
            "replaced_policy_hash": rejected_hash,
            "replaced_version": 2,
            "archive_digest": measurement["archive_digest"],
            "evidence_collected_at": None,
            "policy": {**parent, "version": 3, "parent": 2, "origin": "rollback"},
        },
    ]
    current = {**parent, "version": 3, "parent": 2, "origin": "rollback", "restored_version": 1}
    # The rollback has to serve its own waiting period first (Codex, round 36).
    waiting = revise.decide(
        _archive(), current, history, measure.measure(_archive(), current, None), NOW
    )
    assert waiting["action"] == "none" and "waiting for" in waiting["reason"]
    stamped = [
        {**e, "policy_version": 3, "policy_hash": policy_mod.policy_hash(current)}
        for e in _archive()
    ]
    measurement_r = measure.measure(stamped, current, None)
    rollback_entry = next(e for e in history if e.get("origin") == "rollback")
    rollback_entry["archive_digest"] = measurement_r["archive_digest"]
    again = revise.decide(stamped, current, history, measurement_r, NOW)
    assert again["action"] == "none"
    assert "rolled back" in again["reason"]
    # New archive content lifts the block.
    grown = [
        *stamped,
        {
            "round": 4,
            "occurred_at": "2026-09-14T18:00:00Z",
            "findings": ["[P2] Archive queue overflow again."],
        },
    ]
    retry = revise.decide(grown, current, history, measure.measure(grown, current, None), NOW)
    assert retry["action"] == "revise"


def test_evidence_against_a_removed_topic_survives_for_candidates():
    current = {
        "anchor": {"credential-redaction": 1},
        "anchor_evidence": {"credential-redaction": 1, "archive-ops": 0, "shell-semantics": 2},
        "anchor_definitions": {
            "credential-redaction": policy_mod.BUILTIN_TOPIC_KEYWORDS["credential-redaction"],
            "archive-ops": ["archive"],
            "shell-semantics": policy_mod.BUILTIN_TOPIC_KEYWORDS["shell-semantics"],
        },
    }
    policy = policy_mod.builtin_policy()
    policy["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    merged = revise.candidate_anchor(current, policy)
    assert merged == {"credential-redaction": 1, "archive-ops": 0, "shell-semantics": 2}
    # Re-mined with different keywords: the old counts no longer apply.
    policy["topics"]["archive-ops"] = {"keywords": ["archive", "branch"], "weight": 1.0}
    assert revise.candidate_anchor(current, policy)["archive-ops"] is None


def test_main_writes_nothing_when_the_history_path_is_refused(tmp_path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    monkeypatch.setattr(
        policy_mod, "AI_OWNED_COMPONENTS", {"policy": policy_mod.relative_to_repo(policy_path)}
    )
    before = policy_path.read_text()
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
                str(tmp_path / "h.jsonl"),
                "--now",
                NOW,
            ]
        )
    assert policy_path.read_text() == before


def test_out_json_may_not_target_a_protected_or_input_file(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    for bad in (policy_mod.REPO_ROOT / "docs" / "self-improvement-archive.jsonl", archive, m_path):
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
                    str(tmp_path / "h.jsonl"),
                    "--dry-run",
                    "--out-json",
                    str(bad),
                ]
            )


def test_mined_topic_names_never_collide_with_existing_topics():
    keywords = {
        **policy_mod.topic_keywords(policy_mod.builtin_policy()),
        "archive-concurrency": ["zzz"],
        "archive-concurrency-2": ["yyy"],
    }
    unclassified = [
        {"round": 1, "finding": "Archive queue drops rounds under concurrency."},
        {"round": 2, "finding": "Concurrency group cancels the archive run."},
    ]
    mined = revise.mine_topics(unclassified, keywords)
    assert mined and mined[0]["name"] not in keywords
    assert mined[0]["name"] == "archive-concurrency-3"


def test_late_evidence_rolls_back_past_an_unjudged_parent_to_the_better_ancestor():
    v1 = _four_topic_policy([1, 1, 1, 1])
    v2 = policy_mod.new_version(
        v1,
        topics=_four_topic_policy([0.25, 1, 1, 1])["topics"],
        threshold=3,
        origin="revision",
        rationale="bad weights, no anchor at the time",
        created_at="2026-09-14T13:00:00Z",
    )
    v3 = policy_mod.new_version(
        v2,
        topics={**v2["topics"], "archive-ops": {"keywords": ["archive"], "weight": 1.0}},
        threshold=3,
        origin="revision",
        rationale="coverage repair",
        created_at="2026-09-14T13:30:00Z",
    )
    history = [
        {"version": 1, "policy": v1},
        {
            "version": 2,
            "parent": 1,
            "origin": "revision",
            "coverage_before": 1.0,
            "validity_before": None,
            "policy": v2,
        },
        {
            "version": 3,
            "parent": 2,
            "origin": "revision",
            "coverage_before": 1.0,
            "validity_before": None,
            "policy": v3,
        },
    ]
    evidence = _four_topic_evidence(
        [3, 1, 2, 0]
    )  # the field strongly supports credential-redaction
    stamped = [
        {**e, "policy_hash": policy_mod.policy_hash(v3), "policy_version": 3}
        for e in _four_topic_archive()
    ]
    measurement = measure.measure(stamped, v3, evidence)
    decision = revise.decide(stamped, v3, history, measurement, NOW)
    assert decision["action"] == "rollback"
    assert "against v1" in decision["reason"]
    assert decision["policy"]["topics"]["credential-redaction"]["weight"] == 1.0
    # v3 had an extra topic; the recorded coverage is the restored v1's own figure.
    assert decision["coverage_after"] == measure.measure(stamped, v1, None)["current"]["coverage"]


def test_report_outputs_may_not_overwrite_canonical_evidence(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    canonical = str(policy_mod.REPO_ROOT / "docs" / "rsi" / "trace-evidence.json")
    with pytest.raises(PermissionError, match="canonical evidence"):
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
                "--out-json",
                canonical,
            ]
        )
    # A deliberate evidence refresh is still allowed to target it.
    policy_mod.assert_safe_output(canonical, kind="evidence")


def test_ancestry_continues_through_a_rollback():
    v1 = _four_topic_policy([1, 1, 1, 1])
    v2 = policy_mod.new_version(
        v1,
        topics=_four_topic_policy([0.25, 1, 1, 1])["topics"],
        threshold=3,
        origin="revision",
        rationale="bad weights",
        created_at="2026-09-14T13:00:00Z",
    )
    v3 = policy_mod.new_version(
        v2,
        topics={**v2["topics"], "archive-ops": {"keywords": ["archive"], "weight": 1.0}},
        threshold=3,
        origin="revision",
        rationale="add topic",
        created_at="2026-09-14T13:30:00Z",
    )
    v4 = policy_mod.new_version(
        v3,
        topics=v2["topics"],
        threshold=3,
        origin="rollback",
        rationale="undo v3",
        created_at="2026-09-14T13:45:00Z",
        restored_version=2,
    )
    history = [
        {"version": 1, "policy": v1},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": v2},
        {"version": 3, "parent": 2, "origin": "revision", "coverage_before": 1.0, "policy": v3},
        {
            "version": 4,
            "parent": 3,
            "origin": "rollback",
            "coverage_before": 1.0,
            "replaced_policy_hash": policy_mod.policy_hash(v3),
            "policy": v4,
        },
    ]
    assert [a["version"] for a in revise.unjudged_ancestors(v4, history)] == [1]
    evidence = _four_topic_evidence([3, 1, 2, 0])
    stamped = [
        {**e, "policy_hash": policy_mod.policy_hash(v4), "policy_version": 4}
        for e in _four_topic_archive()
    ]
    decision = revise.decide(stamped, v4, history, measure.measure(stamped, v4, evidence), NOW)
    assert decision["action"] == "rollback"
    assert "against v1" in decision["reason"]
    assert decision["policy"]["restored_version"] == 1


def test_current_definition_mismatch_does_not_erase_an_ancestors_evidence():
    current = {
        "anchor": {"archive-ops": None},  # current policy's keywords differ from the snapshot
        "anchor_evidence": {"archive-ops": 4},
        "anchor_definitions": {"archive-ops": ["archive"]},
    }
    ancestor = policy_mod.builtin_policy()
    ancestor["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    assert revise.candidate_anchor(current, ancestor)["archive-ops"] == 4
    changed = policy_mod.builtin_policy()
    changed["topics"]["archive-ops"] = {"keywords": ["archive", "branch"], "weight": 1.0}
    assert revise.candidate_anchor(current, changed)["archive-ops"] is None


def test_out_policy_may_not_be_the_history_file(tmp_path, monkeypatch):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    history = tmp_path / "history.jsonl"
    monkeypatch.setattr(
        policy_mod,
        "AI_OWNED_COMPONENTS",
        {
            "policy": policy_mod.relative_to_repo(policy_path),
            "history": policy_mod.relative_to_repo(history),
        },
    )
    with pytest.raises(PermissionError, match="policy component"):
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
                "--out-policy",
                str(history),
                "--now",
                NOW,
            ]
        )
    assert not history.exists()


def test_rollback_judges_a_revision_only_on_its_own_rounds():
    parent = _four_topic_policy([1, 1, 1, 1])
    child = policy_mod.new_version(
        parent,
        topics=_four_topic_policy([0.25, 1, 1, 1])["topics"],
        threshold=3,
        origin="revision",
        rationale="d",
        created_at="2026-09-14T14:00:00Z",
    )

    def stamp(e, p):
        return {**e, "policy_hash": policy_mod.policy_hash(p), "policy_version": p["version"]}

    older = [stamp(e, parent) for e in _four_topic_archive()]
    own = [
        stamp(
            {**e, "round": e["round"] + 10, "occurred_at": e["occurred_at"].replace("T1", "T2")},
            child,
        )
        for e in _four_topic_archive()
    ]
    assert [e["round"] for e in revise.entries_under([*older, *own], child)] == [11, 12, 13]
    assert revise.entries_under(older, child) == []


def test_rollback_check_does_not_clobber_full_archive_coverage():
    # v2 covers 100% of its own rounds but only part of the archive; the
    # trigger must still see the full-archive figure.
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1,
        topics=v1["topics"],
        threshold=3,
        origin="revision",
        rationale="same",
        created_at="2026-09-14T14:00:00Z",
    )
    history = [
        {"version": 1, "policy": v1},
        {"version": 2, "parent": 1, "origin": "revision", "coverage_before": 1.0, "policy": v2},
    ]
    own = [
        {**e, "policy_hash": policy_mod.policy_hash(v2), "policy_version": 2}
        for e in _archive()[:1]
    ]
    older = [
        {**e, "policy_hash": policy_mod.policy_hash(v1), "policy_version": 1}
        for e in _archive()[1:]
    ]
    older.append(
        {
            "round": 9,
            "occurred_at": "2026-09-14T18:00:00Z",
            "policy_hash": policy_mod.policy_hash(v1),
            "policy_version": 1,
            "findings": ["[P2] Zzz unclassifiable.", "[P2] Yyy unclassifiable."],
        }
    )
    entries = [
        *own,
        *older,
        *[
            {
                **e,
                "round": e["round"] + 20,
                "policy_hash": policy_mod.policy_hash(v2),
                "policy_version": 2,
            }
            for e in _archive()[:1]
        ],
    ]
    measurement = measure.measure(entries, v2, None)
    assert measurement["current"]["coverage"] < revise.MIN_COVERAGE
    decision = revise.decide(entries, v2, history, measurement, NOW)
    # Not a rollback (own rounds equal the parent), and the low full-archive
    # coverage must still register as the trigger.
    assert decision["action"] != "rollback"
    assert decision["action"] == "revise" or "coverage" in decision["reason"]


def test_out_json_may_not_overwrite_the_field_failures_input(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    failures = tmp_path / "failures.json"
    failures.write_text(json.dumps({"blind_spots": []}))
    with pytest.raises(PermissionError, match="input of this run"):
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
                "--field-failures",
                str(failures),
                "--dry-run",
                "--out-json",
                str(failures),
            ]
        )
    assert json.loads(failures.read_text()) == {"blind_spots": []}


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


def test_failure_kind_labels_do_not_hide_field_blind_spots():
    # "tool-error" contains the shell-semantics keyword "-e"; the label must
    # not classify a failure the policy has no topic for (Codex, round 32).
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    assert "-e" in keywords["shell-semantics"]
    failures = [
        {"kind": "tool-error", "excerpt": "ModuleNotFoundError: No module named yaml"},
        {"kind": "tool-error", "excerpt": "bash: set -e aborted the pipeline"},
        {"kind": "tool-error", "excerpt": ""},
    ]
    spots = revise.field_blind_spots({"blind_spots": failures}, keywords)
    assert [s["finding"] for s in spots] == ["ModuleNotFoundError: No module named yaml"]
    assert spots[0]["kind"] == "tool-error"


def test_candidate_anchor_reaches_an_older_definition_of_a_reused_name():
    old_policy = policy_mod.builtin_policy()
    old_policy["topics"]["archive-branch"] = {"keywords": ["archive", "branch"], "weight": 1.0}
    new_policy = policy_mod.builtin_policy()
    new_policy["topics"]["archive-branch"] = {"keywords": ["archive", "commit"], "weight": 1.0}
    current = {
        "anchor": {"archive-branch": 1},
        "anchor_evidence": {"archive-branch": 1, "archive-branch@old": 4},
        "anchor_definitions": {
            "archive-branch": ["archive", "commit"],
            "archive-branch@old": ["archive", "branch"],
        },
    }
    assert revise.candidate_anchor(current, new_policy) == {"archive-branch": 1}
    assert revise.candidate_anchor(current, old_policy) == {"archive-branch": 4}


def test_candidate_anchor_never_borrows_a_count_from_another_definition():
    # The current policy uses the OLD definition, so its measured anchor is
    # the old count (4). A candidate that re-defines the name must be judged
    # on the evidence searched with its own words (1), not on the current
    # policy's count overlaid onto the plain name (Codex, round 33).
    old_policy = policy_mod.builtin_policy()
    old_policy["topics"]["archive-branch"] = {"keywords": ["archive", "branch"], "weight": 1.0}
    new_policy = policy_mod.builtin_policy()
    new_policy["topics"]["archive-branch"] = {"keywords": ["archive", "commit"], "weight": 1.0}
    unrelated = policy_mod.builtin_policy()
    unrelated["topics"]["archive-branch"] = {"keywords": ["never", "searched"], "weight": 1.0}
    current = {
        "anchor": {"archive-branch": 4},
        "anchor_evidence": {"archive-branch": 1, "archive-branch@old": 4, "retired-topic": 2},
        "anchor_definitions": {
            "archive-branch": ["archive", "commit"],
            "archive-branch@old": ["archive", "branch"],
            "retired-topic": ["retired"],
        },
    }
    assert revise.candidate_anchor(current, old_policy)["archive-branch"] == 4
    assert revise.candidate_anchor(current, new_policy)["archive-branch"] == 1
    assert revise.candidate_anchor(current, unrelated)["archive-branch"] is None
    # Evidence for a topic no candidate defines is still carried, so it can
    # block re-mining; variant keys are not exposed as topics.
    result = revise.candidate_anchor(current, new_policy)
    assert result["retired-topic"] == 2
    assert "archive-branch@old" not in result


def test_failure_kind_labels_cannot_become_a_mined_topic():
    # Five unrelated failures share only the synthetic label "tool-error".
    # Mining over "kind excerpt" text accepted a topic whose one keyword was
    # the label and which classified no real failure (Codex, round 34).
    keywords = policy_mod.topic_keywords(policy_mod.builtin_policy())
    excerpts = [
        "ModuleNotFoundError: No module named yaml",
        "disk quota exceeded while writing cache",
        "segmentation fault (core dumped)",
        "certificate verify chain broken",
        "address already in use: port 3000",
    ]
    failures = [{"kind": "tool-error", "excerpt": e} for e in excerpts]
    spots = revise.field_blind_spots({"blind_spots": failures}, keywords)
    assert len(spots) == 5
    mined = revise.mine_topics(spots, keywords)
    assert all("tool" not in m["keywords"] and "error" not in m["keywords"] for m in mined)
    assert mined == []


def test_swapped_policy_and_history_destinations_are_refused_before_any_write(
    tmp_path, monkeypatch
):
    # Both paths are AI-owned, so the shared allowlist accepted them in either
    # role; swapped arguments appended a policy to the history and overwrote
    # the policy with a history line (Codex, round 35).
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), policy_mod.builtin_policy(), None)))
    history = tmp_path / "history.jsonl"
    history.write_text("")
    monkeypatch.setattr(
        policy_mod,
        "AI_OWNED_COMPONENTS",
        {
            "policy": policy_mod.relative_to_repo(policy_path),
            "history": policy_mod.relative_to_repo(history),
        },
    )
    before_policy = policy_path.read_text()
    # Swapped arguments are refused either by the lineage check (the policy
    # file is not a history) or by the role check; neither may write.
    try:
        code = revise.main(
            [
                "r",
                str(archive),
                "--measurement",
                str(m_path),
                "--policy",
                str(policy_path),
                "--history",
                str(policy_path),
                "--out-policy",
                str(history),
                "--now",
                NOW,
            ]
        )
    except PermissionError as exc:
        assert "policy component" in str(exc)
    else:
        assert code == 1
    assert policy_path.read_text() == before_policy
    assert history.read_text() == ""
    # The library guards agree with the CLI: a policy may not be saved to the
    # history component, nor a history entry appended to the policy component.
    with pytest.raises(PermissionError, match="policy component"):
        policy_mod.save_policy(policy_mod.builtin_policy(), history)
    with pytest.raises(PermissionError, match="history component"):
        policy_mod.append_history({"version": 1}, policy_path)


def test_rollback_to_the_root_version_still_waits_before_being_judged():
    # judged_from() is None for a rollback to v1 (no parent), which used to
    # skip the waiting period and let the rejected configuration be
    # re-proposed after a single clean round (Codex, round 36).
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1, topics=dict(v1["topics"]), threshold=v1["threshold"], origin="revision", rationale="x"
    )
    v3 = policy_mod.new_version(
        v2,
        topics=dict(v1["topics"]),
        threshold=v1["threshold"],
        origin="rollback",
        rationale="back",
        restored_version=1,
    )
    history = [
        {"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)},
        {
            "version": 3,
            "origin": "rollback",
            "policy": v3,
            "replaced_policy_hash": policy_mod.policy_hash(v2),
        },
    ]
    entries = [
        {
            "round": 1,
            "findings": [],
            "occurred_at": "2026-09-14T15:00:00Z",
            "policy_version": 3,
            "policy_hash": policy_mod.policy_hash(v3),
        }
    ]
    measurement = measure.measure(entries, v3, None)
    decision = revise.decide(entries, v3, history, measurement, NOW)
    assert decision["action"] == "none"
    assert "waiting for" in decision["reason"]


def test_a_rolled_back_configuration_is_never_a_rollback_target():
    # v2 was rolled back; the restored configuration (v3) must not be judged
    # "worse than v2" on coverage two rounds later and rolled back INTO v2,
    # or the loop would ping-pong forever.
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1,
        topics={**v1["topics"], "queue-overflow": {"keywords": ["queue overflow"], "weight": 1.0}},
        threshold=v1["threshold"],
        origin="revision",
        rationale="mined",
    )
    v3 = policy_mod.new_version(
        v2,
        topics=dict(v1["topics"]),
        threshold=v1["threshold"],
        origin="rollback",
        rationale="worse",
        restored_version=1,
    )
    history = [
        {"version": 1, "policy": v1},
        {"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)},
        {
            "version": 3,
            "origin": "rollback",
            "policy": v3,
            "replaced_policy_hash": policy_mod.policy_hash(v2),
        },
    ]
    entries = [
        {**e, "policy_version": 3, "policy_hash": policy_mod.policy_hash(v3)} for e in _archive()
    ]
    decision = revise.decide(entries, v3, history, measure.measure(entries, v3, None), NOW)
    assert decision["action"] != "rollback"
    assert revise.rolled_back_hashes(history) == {policy_mod.policy_hash(v2)}


def test_coverage_lost_at_a_grandparent_still_triggers_rollback():
    # v1 knew "quartz"; v2 dropped it; v3 added something unrelated. On v3's
    # own rounds v3 and v2 tie at 0 coverage, so a parent-only comparison
    # never rolls back, but v1 covers everything (Codex full-branch review,
    # finding 5).
    v1 = policy_mod.builtin_policy()
    v1["topics"]["quartz-crashes"] = {"keywords": ["quartz"], "weight": 1.0}
    v2 = policy_mod.new_version(
        v1,
        topics={k: v for k, v in v1["topics"].items() if k != "quartz-crashes"},
        threshold=v1["threshold"],
        origin="revision",
        rationale="dropped",
    )
    v3 = policy_mod.new_version(
        v2,
        topics={**v2["topics"], "unrelated": {"keywords": ["zzunrelated"], "weight": 1.0}},
        threshold=v2["threshold"],
        origin="revision",
        rationale="added",
    )
    history = [
        {"version": 1, "policy": v1},
        {"version": 2, "policy": v2, "replaced_policy_hash": policy_mod.policy_hash(v1)},
        {"version": 3, "policy": v3, "replaced_policy_hash": policy_mod.policy_hash(v2)},
    ]
    entries = [
        {
            "round": n,
            "occurred_at": f"2026-09-14T1{n}:00:00Z",
            "findings": [f"**[P2]** quartz renderer crashed again ({n})."],
            "policy_version": 3,
            "policy_hash": policy_mod.policy_hash(v3),
        }
        for n in (1, 2)
    ]
    decision = revise.decide(entries, v3, history, measure.measure(entries, v3, None), NOW)
    assert decision["action"] == "rollback"
    assert decision["policy"]["restored_version"] == 1


def test_main_refuses_a_policy_whose_lineage_metadata_was_edited(tmp_path, monkeypatch, capsys):
    # Changing only `origin` used to switch the wait gate off: the hash does
    # not cover lineage metadata (Codex full-branch review, finding 1).
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1, topics=dict(v1["topics"]), threshold=v1["threshold"], origin="revision", rationale="x"
    )
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({"version": 2, "policy": v2}) + "\n")
    edited = {**v2, "origin": "init", "parent": None}
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(edited))
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(measure.measure(_archive(), edited, None)))
    code = revise.main(
        [
            "r",
            str(archive),
            "--measurement",
            str(m_path),
            "--policy",
            str(policy_path),
            "--history",
            str(history),
            "--dry-run",
            "--now",
            NOW,
        ]
    )
    assert code == 1
    assert "lineage metadata" in capsys.readouterr().err
    with pytest.raises(ValueError, match="history records no versions"):
        policy_mod.assert_policy_matches_history(v2, [])
    policy_mod.assert_policy_matches_history(v2, [{"version": 2, "policy": v2}])


def test_evidence_window_filters_by_round_identity_not_number():
    # Two reviews share round 5; one predates the snapshot and one does not.
    entries = [
        {
            "round": 5,
            "source_sha": "old",
            "findings": ["**[P1]** a."],
            "occurred_at": "2026-09-14T15:00:00Z",
        },
        {
            "round": 5,
            "source_sha": "new",
            "findings": ["**[P1]** b."],
            "occurred_at": "2026-09-14T17:00:00Z",
        },
    ]
    measurement = {"anchor": {"collected_at": "2026-09-14T16:00:00Z"}}
    covered = revise.entries_covered_by_evidence(entries, measurement)
    assert [e["source_sha"] for e in covered] == ["old"]
