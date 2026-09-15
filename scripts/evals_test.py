"""Tests for build-evals.py and run-evals.py."""

import importlib.util
import json
import sys
from pathlib import Path


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build = _load("build_evals", "build-evals.py")
run = _load("run_evals", "run-evals.py")
policy_mod = sys.modules["improvement_policy"]
KW = policy_mod.topic_keywords(policy_mod.builtin_policy())


def _entries():
    return [
        {
            "round": 1,
            "source_sha": "aaa",
            "findings": ["[P1] leaked credential token FAKE_TOK_1 in log"],
        },
        {"round": 2, "source_sha": "bbb", "findings": ["[P2] pipefail missing; bash -e trap"]},
        {"round": 3, "source_sha": "gone", "findings": ["[P2] whatever"]},
        {"round": 4, "findings": ["[P2] no sha"]},
    ]


def _diffs(sha):
    return {"aaa": "+ print(token)\n+ token = 'FAKE_TOK_1'\n", "bbb": "+ set -uo pipefail\n"}.get(
        sha
    )


def test_build_makes_one_case_per_reachable_round_and_scrubs_secrets():
    cases, skipped = build.build(_entries(), KW, _diffs, 60_000)
    assert [c["id"] for c in cases] == ["round-001", "round-002"] and skipped == 2
    assert cases[0]["expected_topics"] == ["credential-redaction"]
    assert "FAKE_TOK_1" not in json.dumps(cases[0])


def test_keyword_grader_measures_what_a_prepush_check_could_catch():
    cases, _ = build.build(_entries(), KW, _diffs, 60_000)
    r1 = run.grade_keywords(cases[0], KW)
    r2 = run.grade_keywords(cases[1], KW)
    assert r1["recall"] == 1.0, "the diff mentions 'token'"
    assert r2["recall"] == 1.0, "the diff mentions 'pipefail'"
    summary = run.summarize([r1, r2])
    assert summary["cases"] == 2 and summary["mean_recall"] == 1.0


def test_codex_grader_scores_recall_from_the_reviewers_json():
    cases, _ = build.build(_entries(), KW, _diffs, 60_000)

    def reviewer_labels_other(_prompt):
        return (
            '{"findings": [{"topic": "other", "summary": "A secret token is printed to the log."}]}'
        )

    def reviewer_babbles(_prompt):
        return "not json at all"

    result = run.grade_codex(cases[0], KW, reviewer_labels_other)
    assert result["recall"] == 1.0, (
        "an 'other' finding still counts when its summary classifies under the topic"
    )
    assert run.grade_codex(cases[1], KW, reviewer_babbles)["recall"] == 0.0


def test_main_writes_a_result_file_outside_protected_paths(tmp_path):
    cases, _ = build.build(_entries(), KW, _diffs, 60_000)
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for c in cases:
        (cases_dir / f"{c['id']}.json").write_text(json.dumps(c))
    out = tmp_path / "results"
    assert run.main(["r", "--cases", str(cases_dir), "--out", str(out), "--limit", "1"]) == 0
    written = list(out.glob("keywords-*.json"))
    assert len(written) == 1 and json.loads(written[0].read_text())["summary"]["cases"] == 1
