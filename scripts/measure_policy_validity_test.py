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
        "definitions": policy_mod.topic_keywords(policy_mod.builtin_policy()),
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
    archive = [
        *_archive(),
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


def test_truncated_searches_are_unknown_not_absolute():
    evidence = _evidence()
    evidence["truncated"] = ["shell-semantics"]
    current = measure.measure(_archive(), policy_mod.builtin_policy(), evidence)["current"]
    assert current["anchor"]["shell-semantics"] is None
    assert "shell-semantics" in current["anchor_unknown_topics"]
    assert current["anchor"]["credential-redaction"] == 1


def test_findings_newer_than_the_evidence_do_not_mark_topics_dev_only():
    evidence = _evidence()
    evidence["topics"]["credential-redaction"] = []
    evidence["collected_at"] = "2026-09-14T15:30:00Z"  # after round 1 only
    result = measure.measure(_archive(), policy_mod.builtin_policy(), evidence)
    current = result["current"]
    assert current["rounds_after_evidence"] == 2
    # credential-redaction recurs in rounds 1-3 but only round 1 predates the snapshot.
    assert current["dev_only_topics"] == []
    # Reported validity is the covered-window figure; the all-rounds value is kept, labelled.
    assert "validity_all_rounds" in current
    assert set(current["anchor_evidence"]) == set(evidence["topics"])
    fresh = dict(evidence, collected_at="2026-09-14T18:00:00Z")
    assert measure.measure(_archive(), policy_mod.builtin_policy(), fresh)["current"][
        "dev_only_topics"
    ] == ["credential-redaction"]


def test_empty_anchor_is_treated_as_no_anchor():
    evidence = {
        "source": "traces",
        "agents": ["claude-code"],
        "definitions": dict(policy_mod.BUILTIN_TOPIC_KEYWORDS),
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


def test_out_json_and_newline_safe_report(tmp_path, capsys):
    archive = tmp_path / "archive.jsonl"
    entries = _archive()
    entries[2]["findings"].append("[P2] A finding with\n---\nan embedded boundary.")
    archive.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    out = tmp_path / "m.json"
    assert measure.main(["m", str(archive), "--policy", str(policy), "--out-json", str(out)]) == 0
    report = capsys.readouterr().out
    human = report.split("---\n", 1)[0]
    assert "embedded boundary" in human and "\n---\n" not in human.replace(human.rstrip(), "")
    assert json.loads(out.read_text())["current"]["findings_total"] == 7


def test_evidence_searched_under_a_different_definition_is_unknown():
    policy = policy_mod.builtin_policy()
    evidence = _evidence()
    evidence["definitions"]["shell-semantics"] = ["something", "else"]
    current = measure.measure(_archive(), policy, evidence)["current"]
    assert current["anchor"]["shell-semantics"] is None
    assert current["anchor"]["credential-redaction"] == 1
    legacy = _evidence()
    del legacy["definitions"]
    assert (
        measure.measure(_archive(), policy, legacy)["current"]["anchor"]["credential-redaction"]
        is None
    )


def test_side_outputs_may_not_overwrite_protected_files(tmp_path):
    import pytest

    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    protected = str(policy_mod.REPO_ROOT / "docs" / "self-improvement-archive.jsonl")
    with pytest.raises(PermissionError):
        measure.main(["m", str(archive), "--policy", str(policy), "--out-json", protected])
    with pytest.raises(PermissionError):
        measure.main(["m", str(archive), "--policy", str(policy), "--out-json", str(archive)])


def test_out_json_and_save_evidence_may_not_be_the_same_file(tmp_path, capsys):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    same = tmp_path / "same.json"
    code = measure.main(
        [
            "m",
            str(archive),
            "--policy",
            str(policy),
            "--repo-dir",
            str(tmp_path),
            "--out-json",
            str(same),
            "--save-evidence",
            str(same),
        ]
    )
    assert code == 1
    assert "must be different files" in capsys.readouterr().err
    assert not same.exists()


def test_epoch_without_a_timestamp_has_unknown_evidence_not_all_of_it():
    archive = [{"round": 1, "findings": ["[P1] Secret leaked."]}, *_archive()[1:]]
    result = measure.measure(archive, policy_mod.builtin_policy(), _evidence())
    first = result["epochs"][0]
    assert first["timestamp_ms"] is None
    assert all(v is None for v in first["anchor"].values())
    assert first["validity"] is None
    # The current (non-historical) measurement still uses the whole snapshot.
    assert result["current"]["anchor"]["shell-semantics"] == 2


def test_evidence_refresh_keeps_searching_topics_from_earlier_policy_versions():
    current = policy_mod.topic_keywords(policy_mod.builtin_policy())
    v2 = policy_mod.builtin_policy()
    v2["topics"]["archive-branch"] = {"keywords": ["archive", "branch"], "weight": 1.0}
    history = [
        {"version": 2, "policy": v2},
        {"version": 3, "origin": "rollback", "policy": policy_mod.builtin_policy()},
    ]
    extra = measure.historical_definitions(history, current)
    assert extra == {"archive-branch": ["archive", "branch"]}
    assert "credential-redaction" not in extra


def test_undated_traces_make_historical_counts_unknown_but_not_current_ones():
    evidence = _evidence()
    evidence["topics"]["shell-semantics"].append(
        {"id": "u", "agentId": "claude-code", "timestamp": None}
    )
    result = measure.measure(_archive(), policy_mod.builtin_policy(), evidence)
    assert all(e["anchor"]["shell-semantics"] is None for e in result["epochs"])
    assert result["epochs"][1]["anchor"]["credential-redaction"] == 1
    assert result["current"]["anchor"]["shell-semantics"] == 3


def test_history_path_counts_as_an_input_for_output_guards(tmp_path):
    import pytest

    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_mod.builtin_policy()))
    history = tmp_path / "history.jsonl"
    history.write_text("")
    with pytest.raises(PermissionError, match="input of this run"):
        measure.main(
            [
                "m",
                str(archive),
                "--policy",
                str(policy),
                "--history",
                str(history),
                "--out-json",
                str(history),
            ]
        )


def test_evidence_collection_counts_only_tool_results_and_errors(monkeypatch):
    calls = []

    def fake_run(_bin, args):
        calls.append(args)
        return {"traces": [{"id": "t1", "agentId": "claude-code", "timestamp": 1}]}

    monkeypatch.setattr(measure, "run_traces_json", fake_run)
    evidence = measure.collect_trace_evidence(
        "traces", "/repo", {"credential-redaction": ["secret"]}, ["claude-code"]
    )
    assert evidence["event_types"] == "tool_result,error"
    for args in calls:
        assert args[args.index("--event-type") + 1] == "tool_result,error"
    assert evidence["topics"]["credential-redaction"][0]["id"] == "t1"
