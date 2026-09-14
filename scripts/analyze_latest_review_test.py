"""Tests for analyze-latest-review.py.

Run with: python3 -m pytest scripts/analyze_latest_review_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "analyze-latest-review.py"
_spec = importlib.util.spec_from_file_location("analyze_latest_review", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
analyze_mod = importlib.util.module_from_spec(_spec)
sys.modules["analyze_latest_review"] = analyze_mod
_spec.loader.exec_module(analyze_mod)


def _entry(round_num, findings):
    return {"round": round_num, "findings": findings}


def test_topic_not_yet_crossed_and_new_round_pushes_it_over():
    """Two prior rounds mention credential redaction (below threshold 3);
    a new round's findings supply the third -> must be reported as newly
    crossed."""
    archive = [
        _entry(1, ["**[P1]** Secret token leaked in stdout."]),
        _entry(2, ["**[P2]** Credential redaction missed a field."]),
    ]
    new_findings = ["**[P1]** Another secret exposed in stderr."]

    crossed = analyze_mod.find_newly_crossed_topics(archive, new_findings, threshold=3)
    topics = {c["topic"] for c in crossed}
    assert "credential-redaction" in topics


def test_topic_already_at_mechanism_level_is_not_reported_again():
    """A topic that already recommended 'mechanism' in the archive alone
    must NOT be reported every subsequent round -- only the round that
    first tips it over counts as 'newly crossed'."""
    archive = [
        _entry(1, ["**[P1]** Secret leaked."]),
        _entry(2, ["**[P2]** Credential redaction missed a field."]),
        _entry(3, ["**[P1]** Token exposed again."]),
    ]
    # Already at/above threshold 3 without the new round.
    new_findings = ["**[P2]** Yet another credential leak."]

    crossed = analyze_mod.find_newly_crossed_topics(archive, new_findings, threshold=3)
    assert crossed == []


def test_unrelated_new_finding_does_not_falsely_cross():
    archive = [
        _entry(1, ["**[P1]** Secret leaked."]),
        _entry(2, ["**[P2]** Credential redaction missed a field."]),
    ]
    new_findings = ["**[P2]** Minor typo in a comment."]

    crossed = analyze_mod.find_newly_crossed_topics(archive, new_findings, threshold=3)
    assert crossed == []


def test_empty_archive_with_new_round_below_threshold_reports_nothing():
    crossed = analyze_mod.find_newly_crossed_topics([], ["**[P1]** Secret leaked once."], threshold=3)
    assert crossed == []


def test_next_round_number_from_empty_archive_is_one():
    assert analyze_mod.next_round_number([]) == 1


def test_next_round_number_increments_from_max():
    archive = [_entry(1, []), _entry(4, []), _entry(2, [])]
    assert analyze_mod.next_round_number(archive) == 5


def test_main_cli_with_real_archive_and_no_findings_in_review(tmp_path, capsys):
    archive_path = tmp_path / "archive.jsonl"
    archive_path.write_text(
        json.dumps(_entry(1, ["**[P1]** Secret leaked."])) + "\n"
    )
    review_path = tmp_path / "review.txt"
    review_path.write_text("### Codex independent review\n\nNo issues found.\n")

    exit_code = analyze_mod.main(
        ["analyze-latest-review.py", str(archive_path), str(review_path)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"newly_crossed": []' in out


def test_main_cli_reports_newly_crossed_topic(tmp_path, capsys):
    archive_path = tmp_path / "archive.jsonl"
    lines = [
        json.dumps(_entry(1, ["**[P1]** Secret token leaked in stdout."])),
        json.dumps(_entry(2, ["**[P2]** Credential redaction missed a field."])),
    ]
    archive_path.write_text("\n".join(lines) + "\n")

    review_path = tmp_path / "review.txt"
    review_path.write_text(
        "1. **[P1]** Another secret exposed in stderr on failure.\n"
    )

    exit_code = analyze_mod.main(
        ["analyze-latest-review.py", str(archive_path), str(review_path)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out.split("---\n", 1)[1])
    topics = {c["topic"] for c in payload["newly_crossed"]}
    assert "credential-redaction" in topics
