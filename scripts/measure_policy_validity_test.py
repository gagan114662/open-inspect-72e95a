"""Tests for measure-policy-validity.py.

Run with: python3 -m pytest scripts/measure_policy_validity_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "measure-policy-validity.py"
_spec = importlib.util.spec_from_file_location("measure_policy_validity", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
measure = importlib.util.module_from_spec(_spec)
sys.modules["measure_policy_validity"] = measure
_spec.loader.exec_module(measure)
policy_mod = sys.modules["improvement_policy"]

T0 = "2026-09-14T15:00:00Z"
T1 = "2026-09-14T16:00:00Z"
T2 = "2026-09-14T17:00:00Z"


def _ms(iso: str) -> int:
    return measure.parse_timestamp_ms(iso)


def _archive():
    return [
        {
            "round": 1,
            "occurred_at": T0,
            "findings": ["[P1] Secret leaked into logs.", "[P2] Shell exit code ignored."],
        },
        {"round": 2, "occurred_at": "pending", "findings": []},
        {
            "round": 2,
            "occurred_at": T1,
            "findings": ["[P2] Token exposed in comment.", "[P2] Archive concurrency drops runs."],
        },
        {
            "round": 3,
            "occurred_at": T2,
            "findings": [
                "[P2] Credential redaction missed a field.",
                "[P2] Archive PR creation cannot recover.",
            ],
        },
    ]


def _evidence():
    return {
        "source": "traces",
        "agents": ["claude-code"],
        "topics": {
            "credential-redaction": [
                {"id": "a", "agentId": "claude-code", "timestamp": _ms(T0) + 1}
            ],
            "shell-semantics": [
                {"id": "b", "agentId": "claude-code", "timestamp": _ms(T1) - 1},
                {"id": "c", "agentId": "claude-code", "timestamp": _ms(T2) - 1},
            ],
            "env-var-precedence": [],
            "fork-pr-permissions": [],
            "auth-lifecycle": [],
        },
    }


def test_rounds_merge_pending_placeholders_and_carry_timestamps():
    rounds = measure.rounds_in_order(_archive())
    assert [r["round"] for r in rounds] == [1, 2, 3]
    assert rounds[1]["timestamp_ms"] == _ms(T1)
    assert len(rounds[1]["findings"]) == 2


def test_coverage_counts_unclassified_findings():
    result = measure.measure(_archive(), policy_mod.builtin_policy(), None)
    current = result["current"]
    assert current["findings_total"] == 6
    assert current["findings_classified"] == 4
    assert current["coverage"] == round(4 / 6, 4)
    assert [u["round"] for u in current["unclassified_findings"]] == [2, 3]
    assert current["validity"] is None
    assert result["anchor"]["source"] == "none"


def test_anchor_is_replayed_per_epoch_by_timestamp():
    result = measure.measure(_archive(), policy_mod.builtin_policy(), _evidence())
    epochs = result["epochs"]
    assert epochs[0]["anchor"]["shell-semantics"] == 0
    assert epochs[1]["anchor"]["shell-semantics"] == 1
    assert epochs[2]["anchor"]["shell-semantics"] == 2
    assert result["current"]["anchor"]["shell-semantics"] == 2
    assert result["anchor"]["traces_considered"] == 3


def test_validity_is_rank_agreement_between_review_signal_and_anchor():
    assert measure.spearman([1, 2, 3], [1, 2, 3]) == 1.0
    assert measure.spearman([1, 2, 3], [3, 2, 1]) == -1.0
    assert measure.spearman([1, 1, 1], [1, 2, 3]) is None
    assert measure.spearman([1, 2], [1, 2]) is None
    result = measure.measure(_archive(), policy_mod.builtin_policy(), _evidence())
    current = result["current"]
    # credential-redaction: 3 rounds vs 1 trace; shell-semantics: 1 round vs 2 traces.
    assert current["validity"] is not None
    assert current["dev_only_topics"] == []
    assert current["anchor_only_topics"] == []


def test_dev_only_topics_flag_review_credit_the_field_never_corroborates():
    evidence = _evidence()
    evidence["topics"]["credential-redaction"] = []
    current = measure.measure(_archive(), policy_mod.builtin_policy(), evidence)["current"]
    assert current["dev_only_topics"] == ["credential-redaction"]


def test_unsearched_topics_are_unknown_not_zero():
    policy = policy_mod.builtin_policy()
    policy["topics"]["archive-ops"] = {"keywords": ["archive"], "weight": 1.0}
    current = measure.measure(_archive(), policy, _evidence())["current"]
    assert current["anchor"]["archive-ops"] is None
    assert current["anchor_unknown_topics"] == ["archive-ops"]
    assert "archive-ops" not in current["dev_only_topics"]
    assert current["dev"]["archive-ops"] == 2


def test_validity_uses_the_weighted_signal_the_detector_decides_on():
    policy = policy_mod.builtin_policy()
    baseline = measure.measure(_archive(), policy, _evidence())["current"]
    policy["topics"]["credential-redaction"]["weight"] = 0.25
    discounted = measure.measure(_archive(), policy, _evidence())["current"]
    assert discounted["dev"] == baseline["dev"]
    assert discounted["dev_weighted"]["credential-redaction"] == 0.75
    assert discounted["validity"] != baseline["validity"]


def test_replay_orders_rounds_by_time_not_round_number():
    archive = _archive() + [
        {"round": 9, "occurred_at": "2026-09-14T17:30:00Z", "findings": ["[P2] Nine first."]},
        {"round": 8, "occurred_at": "2026-09-14T18:00:00Z", "findings": ["[P2] Eight later."]},
    ]
    rounds = measure.rounds_in_order(archive)
    assert [r["round"] for r in rounds] == [1, 2, 3, 9, 8]
    epochs = measure.measure(archive, policy_mod.builtin_policy(), None)["epochs"]
    assert [e["round"] for e in epochs] == [1, 2, 3, 9, 8]
    assert epochs[3]["findings_total"] == 7  # round 8's later finding is not in round 9's epoch


def test_measurement_is_bound_to_the_archive_contents():
    a = measure.measure(_archive(), policy_mod.builtin_policy(), None)["archive_digest"]
    b = measure.measure(_archive()[:-1], policy_mod.builtin_policy(), None)["archive_digest"]
    assert a != b
    assert measure.archive_digest(_archive()) == a


def test_empty_anchor_is_treated_as_no_anchor():
    evidence = {
        "source": "traces",
        "agents": ["claude-code"],
        "topics": {t: [] for t in policy_mod.BUILTIN_TOPIC_KEYWORDS},
    }
    result = measure.measure(_archive(), policy_mod.builtin_policy(), evidence)
    assert result["anchor"]["source"] == "traces (empty)"
    assert result["current"]["anchor"] is None
    assert result["current"]["dev_only_topics"] == []


def test_main_reports_and_emits_json(tmp_path, capsys):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_evidence()))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    assert (
        measure.main(
            ["m", str(archive), "--trace-evidence", str(evidence), "--policy", str(policy)]
        )
        == 0
    )
    out = capsys.readouterr().out
    human, payload = out.split("---\n", 1)
    assert "coverage 0.6667" in human
    assert "unclassified (round 2)" in human
    assert json.loads(payload)["policy_version"] == 1
