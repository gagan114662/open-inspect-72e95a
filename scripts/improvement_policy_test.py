"""Tests for improvement_policy.py.

Run with: python3 -m pytest scripts/improvement_policy_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parent / "improvement_policy.py"
_spec = importlib.util.spec_from_file_location("improvement_policy", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
improvement_policy = importlib.util.module_from_spec(_spec)
sys.modules["improvement_policy"] = improvement_policy
_spec.loader.exec_module(improvement_policy)


def test_checked_in_policy_validates_and_descends_from_the_builtin_v1():
    checked_in = improvement_policy.load_policy()
    builtin = improvement_policy.builtin_policy()
    assert checked_in["version"] >= 1
    if checked_in["version"] == 1:
        assert improvement_policy.policy_hash(checked_in) == improvement_policy.policy_hash(builtin)
    else:
        history = improvement_policy.load_history()
        first = next(e for e in history if e["version"] == 2)
        assert first["parent"] == 1
        assert first["replaced_policy_hash"] == improvement_policy.policy_hash(builtin)
        # v1's taxonomy is preserved verbatim at the front of every descendant.
        assert list(checked_in["topics"])[: len(builtin["topics"])] == list(builtin["topics"])


def test_policy_hash_ignores_metadata_but_tracks_decision_fields():
    base = improvement_policy.builtin_policy()
    relabeled = {**base, "rationale": "different words", "created_at": "2030-01-01T00:00:00Z"}
    assert improvement_policy.policy_hash(relabeled) == improvement_policy.policy_hash(base)
    retuned = json.loads(json.dumps(base))
    retuned["threshold"] = 2
    assert improvement_policy.policy_hash(retuned) != improvement_policy.policy_hash(base)


def test_policy_hash_is_sensitive_to_topic_order():
    base = improvement_policy.builtin_policy()
    reordered = {**base, "topics": dict(reversed(list(base["topics"].items())))}
    assert improvement_policy.policy_hash(reordered) != improvement_policy.policy_hash(base)


def test_history_snapshots_preserve_topic_order(tmp_path):
    history = tmp_path / "history.jsonl"
    allowed = {"history": improvement_policy.relative_to_repo(history)}
    policy = improvement_policy.builtin_policy()
    policy["topics"] = dict(reversed(list(policy["topics"].items())))
    improvement_policy.append_history({"version": 2, "policy": policy}, history, allowed=allowed)
    restored = improvement_policy.load_history(history)[0]["policy"]
    assert list(restored["topics"]) == list(policy["topics"])
    assert improvement_policy.policy_hash(restored) == improvement_policy.policy_hash(policy)


def test_classify_uses_policy_order_and_returns_none_when_uncovered():
    keywords = improvement_policy.topic_keywords(improvement_policy.builtin_policy())
    assert (
        improvement_policy.classify_finding("Leaked secret in logs", keywords)
        == "credential-redaction"
    )
    assert improvement_policy.classify_finding("Concurrency queue drops runs", keywords) is None


def test_new_version_links_to_parent_and_validates():
    parent = improvement_policy.builtin_policy()
    child = improvement_policy.new_version(
        parent,
        topics={
            **parent["topics"],
            "workflow-concurrency": {"keywords": ["concurrency"], "weight": 1.0},
        },
        threshold=parent["threshold"],
        origin="revision",
        rationale="coverage repair",
        created_at="2026-09-14T19:00:00Z",
    )
    assert child["version"] == 2
    assert child["parent"] == 1
    assert "workflow-concurrency" in child["topics"]
    with pytest.raises(ValueError):
        improvement_policy.new_version(
            parent, topics={}, threshold=3, origin="revision", rationale=""
        )
    with pytest.raises(ValueError):
        improvement_policy.new_version(
            parent, topics=parent["topics"], threshold=3, origin="edit", rationale=""
        )


def test_attribution_guard_refuses_writes_outside_ai_owned_files(tmp_path):
    with pytest.raises(PermissionError, match="fixed infrastructure"):
        improvement_policy.save_policy(
            improvement_policy.builtin_policy(),
            improvement_policy.REPO_ROOT / "docs" / "self-improvement-archive.jsonl",
        )
    with pytest.raises(PermissionError):
        improvement_policy.append_history(
            {"x": 1}, improvement_policy.REPO_ROOT / ".github" / "workflows" / "codex-review.yml"
        )
    # A test-scoped allowlist lets the same guard be exercised against tmp files.
    allowed = {"policy": improvement_policy.relative_to_repo(tmp_path / "policy.json")}
    improvement_policy.save_policy(
        improvement_policy.builtin_policy(), tmp_path / "policy.json", allowed=allowed
    )
    assert json.loads((tmp_path / "policy.json").read_text())["version"] == 1


def test_history_round_trips(tmp_path):
    history = tmp_path / "history.jsonl"
    allowed = {"history": improvement_policy.relative_to_repo(history)}
    improvement_policy.append_history(
        {"version": 2, "origin": "revision"}, history, allowed=allowed
    )
    improvement_policy.append_history(
        {"version": 3, "origin": "rollback"}, history, allowed=allowed
    )
    assert [e["version"] for e in improvement_policy.load_history(history)] == [2, 3]
    assert improvement_policy.load_history(tmp_path / "missing.jsonl") == []


def test_validate_policy_rejects_empty_keyword_lists():
    policy = improvement_policy.builtin_policy()
    policy["topics"]["empty"] = {"keywords": [], "weight": 1.0}
    with pytest.raises(ValueError, match="non-empty"):
        improvement_policy.validate_policy(policy)
