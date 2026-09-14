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
    current = {**parent, "version": 3, "parent": 2, "origin": "rollback"}
    again = revise.decide(
        _archive(), current, history, measure.measure(_archive(), current, None), NOW
    )
    assert again["action"] == "none"
    assert "rolled back" in again["reason"]
    # New archive content lifts the block.
    grown = [
        *_archive(),
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
    with pytest.raises(PermissionError, match="different files"):
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
