"""Tests for archive-round.py.

Run with: python3 -m pytest scripts/archive_round_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "archive-round.py"
_spec = importlib.util.spec_from_file_location("archive_round", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
archive_round = importlib.util.module_from_spec(_spec)
sys.modules["archive_round"] = archive_round
_spec.loader.exec_module(archive_round)
policy_mod = sys.modules["improvement_policy"]


def _write_archive(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""))


def test_appends_new_round_and_tags_it_with_source_sha(tmp_path):
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(archive_path, [{"round": 1, "findings": ["**[P1]** old finding."]}])

    review_path = tmp_path / "review.txt"
    review_path.write_text("1. **[P1]** A brand new finding.\n")

    exit_code = archive_round.main(
        ["archive-round.py", str(archive_path), str(review_path), "sha-abc123"]
    )
    assert exit_code == 0

    lines = archive_path.read_text().strip().splitlines()
    assert len(lines) == 2
    new_entry = json.loads(lines[-1])
    assert new_entry["round"] == 2
    assert new_entry["source_sha"] == "sha-abc123"
    assert new_entry["findings"] == ["**[P1]** A brand new finding."]


def test_target_reflects_the_passed_argument_not_a_hardcoded_file(tmp_path):
    """Real bug found while proving the mechanism end-to-end by hand: the
    first version of build_round_entry() hardcoded target to
    '.github/workflows/codex-review.yml' regardless of what was actually
    reviewed -- so a round produced from reviewing a completely different
    file (e.g. archive-and-recommend.yml itself) was recorded with the
    wrong target. The workflow always reviews a PR's full diff, not one
    fixed file, so this must come from an argument, not a constant."""
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(archive_path, [])

    review_path = tmp_path / "review.txt"
    review_path.write_text("1. **[P1]** A finding about a totally different file.\n")

    archive_round.main(
        [
            "archive-round.py",
            str(archive_path),
            str(review_path),
            "sha-target-test",
            "--target",
            "PR #42 diff",
        ]
    )
    entry = json.loads(archive_path.read_text().strip().splitlines()[-1])
    assert entry["target"] == "PR #42 diff"


def test_target_defaults_to_something_generic_when_not_passed(tmp_path):
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(archive_path, [])
    review_path = tmp_path / "review.txt"
    review_path.write_text("1. **[P1]** A finding.\n")

    archive_round.main(["archive-round.py", str(archive_path), str(review_path), "sha-default"])
    entry = json.loads(archive_path.read_text().strip().splitlines()[-1])
    assert entry["target"] != ".github/workflows/codex-review.yml"
    assert entry["target"]  # non-empty


def test_rerunning_with_same_source_sha_does_not_duplicate(tmp_path):
    """Idempotency: the specific bug Codex's review flagged as a race-prone
    replay risk -- reprocessing the same review comment (same SHA) must be
    a no-op, not a second appended round."""
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(archive_path, [])

    review_path = tmp_path / "review.txt"
    review_path.write_text("1. **[P1]** Some finding.\n")

    archive_round.main(["archive-round.py", str(archive_path), str(review_path), "sha-xyz"])
    first_pass_lines = archive_path.read_text().strip().splitlines()
    assert len(first_pass_lines) == 1

    exit_code = archive_round.main(
        ["archive-round.py", str(archive_path), str(review_path), "sha-xyz"]
    )
    assert exit_code == 0
    second_pass_lines = archive_path.read_text().strip().splitlines()
    assert second_pass_lines == first_pass_lines


def test_cross_pr_accumulation_crosses_threshold_on_the_third_contributing_round(
    tmp_path, capsys
):
    """The core bug this script exists to fix: without persistence, two
    separate PRs each contributing one finding on the same topic never
    combine. With persistence, round 1 (in the seed archive) + round 2 (this
    PR) + round 3 (a later PR) must cross the threshold on round 3."""
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(
        archive_path,
        [{"round": 1, "findings": ["**[P1]** Secret token leaked in stdout."], "source_sha": "sha-1"}],
    )

    review_path_a = tmp_path / "review-a.txt"
    review_path_a.write_text("1. **[P2]** Credential redaction missed a field.\n")
    archive_round.main(
        ["archive-round.py", str(archive_path), str(review_path_a), "sha-2", "--threshold", "3"]
    )
    # Round 2 alone should not yet cross a threshold of 3.
    after_round_2 = json.loads(archive_path.read_text().strip().splitlines()[-1])
    assert after_round_2["round"] == 2

    review_path_b = tmp_path / "review-b.txt"
    review_path_b.write_text("1. **[P1]** Another secret exposed on failure.\n")
    exit_code = archive_round.main(
        ["archive-round.py", str(archive_path), str(review_path_b), "sha-3", "--threshold", "3"]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    topics = {c["topic"] for c in out["newly_crossed"]}
    assert "credential-redaction" in topics


def test_clean_review_is_persisted_as_a_stamped_round(tmp_path, capsys):
    # A review with no findings is still a completed round under the current
    # policy; discarding it would keep a policy that eliminates findings from
    # ever accumulating the rounds needed to judge it (Codex, round 32).
    archive_path = tmp_path / "archive.jsonl"
    _write_archive(archive_path, [{"round": 1, "findings": ["**[P1]** old finding."]}])

    review_path = tmp_path / "review.txt"
    review_path.write_text("### Codex independent review\n\nNo issues found.\n")

    exit_code = archive_round.main(
        ["archive-round.py", str(archive_path), str(review_path), "sha-empty"]
    )
    assert exit_code == 0
    entries = [json.loads(line) for line in archive_path.read_text().splitlines()]
    assert entries[-1]["round"] == 2
    assert entries[-1]["findings"] == []
    assert entries[-1]["source_sha"] == "sha-empty"
    assert entries[-1]["policy_version"] == policy_mod.POLICY_VERSION
    assert entries[-1]["policy_hash"] == policy_mod.POLICY_HASH
    out = json.loads(capsys.readouterr().out.strip())
    assert out["round"] == 2
    assert out["newly_crossed"] == []
    assert out["already_processed"] is False
