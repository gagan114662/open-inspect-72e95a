"""Tests for detect-recurring-pattern.py.

Run with: python3 -m pytest scripts/detect_recurring_pattern_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "detect-recurring-pattern.py"
_spec = importlib.util.spec_from_file_location("detect_recurring_pattern", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
detect = importlib.util.module_from_spec(_spec)
sys.modules["detect_recurring_pattern"] = detect
_spec.loader.exec_module(detect)


def test_classify_finding_matches_known_topics():
    assert detect.classify_finding("[P1] Redact the leaked credential") == "credential-redaction"
    assert detect.classify_finding("bash -e aborts before exit code capture") == "shell-semantics"
    # Deliberately avoids the word "token": credential-redaction's keyword
    # list includes "token" too, and is checked first, so any example
    # mentioning GITHUB_TOKEN would match there instead — reasonably, since
    # GITHUB_TOKEN genuinely is credential-adjacent. This example isolates
    # fork-pr-permissions specifically.
    assert (
        detect.classify_finding("Fork-originated pull requests cannot receive posted comments")
        == "fork-pr-permissions"
    )
    # Same reasoning: avoids "credential" (which would match
    # credential-redaction first) to isolate auth-lifecycle specifically.
    assert (
        detect.classify_finding("auth.json rotates and the old value goes stale after expiring")
        == "auth-lifecycle"
    )


def test_credential_redaction_keyword_wins_over_other_topics_when_both_present():
    """Documents the real, reasonable behavior the fixed test above works
    around: a finding mentioning GITHUB_TOKEN is credential-adjacent, so it
    is classified as credential-redaction even when it's really about fork
    permissions specifically. Topic buckets are approximate by design (see
    module docstring) — this pins the actual priority order rather than
    leaving it as an implicit, undocumented side effect."""
    assert (
        detect.classify_finding("Fork PRs fail: GITHUB_TOKEN read-only")
        == "credential-redaction"
    )


def test_classify_finding_returns_none_for_unmatched_text():
    assert detect.classify_finding("this finding matches no known topic at all") is None


def test_recommends_mechanism_fix_once_topic_recurs_at_threshold():
    entries = [
        {"round": 1, "findings": ["[P1] leaked credential in output"]},
        {"round": 2, "findings": ["[P1] secret token exposed again"]},
        {"round": 3, "findings": ["[P2] another credential redaction gap"]},
    ]
    result = detect.analyze(entries, threshold=3)
    rec = next(r for r in result["recommendations"] if r["topic"] == "credential-redaction")
    assert rec["recurrence_count"] == 3
    assert rec["recommended_action"] == "mechanism"
    assert rec["rounds"] == [1, 2, 3]


def test_recommends_target_fix_below_threshold():
    entries = [
        {"round": 1, "findings": ["[P1] leaked credential in output"]},
        {"round": 2, "findings": ["[P1] secret token exposed again"]},
    ]
    result = detect.analyze(entries, threshold=3)
    rec = next(r for r in result["recommendations"] if r["topic"] == "credential-redaction")
    assert rec["recurrence_count"] == 2
    assert rec["recommended_action"] == "target"


def test_same_round_multiple_findings_same_topic_counts_once():
    """Recurrence is measured in distinct ROUNDS a topic appears in, not raw
    finding count -- five credential findings in one round is one round of
    evidence, not five, otherwise a single verbose round could trip the
    threshold on its own."""
    entries = [
        {
            "round": 1,
            "findings": [
                "[P1] credential leak A",
                "[P1] credential leak B",
                "[P2] credential leak C",
            ],
        },
        {"round": 2, "findings": ["[P1] credential leak D"]},
    ]
    result = detect.analyze(entries, threshold=3)
    rec = next(r for r in result["recommendations"] if r["topic"] == "credential-redaction")
    assert rec["recurrence_count"] == 2
    assert rec["recommended_action"] == "target"


def test_real_archive_recommends_mechanism_fix_for_credential_redaction(tmp_path):
    """Regression proof against this repo's own real archive data: this must
    reproduce the same 'revise the mechanism, not just the target' call that
    was made manually before round 5 -- derived from evidence, not asserted."""
    archive_path = Path(__file__).parent.parent / "docs" / "self-improvement-archive.jsonl"
    entries = []
    with open(archive_path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    result = detect.analyze(entries, threshold=3)
    rec = next(
        (r for r in result["recommendations"] if r["topic"] == "credential-redaction"), None
    )
    assert rec is not None, "expected credential-redaction topic to appear in the real archive"
    assert rec["recommended_action"] == "mechanism"
    assert rec["recurrence_count"] >= 3


def test_main_cli_runs_against_a_file_and_exits_zero(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(
        json.dumps({"round": 1, "findings": ["[P1] credential leak"]})
        + "\n"
        + json.dumps({"round": 2, "findings": ["[P1] credential leak again"]})
        + "\n"
    )
    exit_code = detect.main(["detect-recurring-pattern.py", str(archive), "--threshold", "2"])
    assert exit_code == 0
