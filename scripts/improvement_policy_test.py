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


def test_topic_names_may_not_contain_at_signs_and_origin_is_checked():
    policy = improvement_policy.builtin_policy()
    policy["topics"]["shell-semantics@custom"] = {"keywords": ["x"], "weight": 1.0}
    with pytest.raises(ValueError, match="'@'"):
        improvement_policy.validate_policy(policy)
    policy = improvement_policy.builtin_policy()
    policy["origin"] = "whatever"
    with pytest.raises(ValueError, match="origin"):
        improvement_policy.validate_policy(policy)


def test_hard_linked_outputs_are_refused(tmp_path, monkeypatch):
    import os

    archive = tmp_path / "archive.jsonl"
    archive.write_text("{}\n")
    alias = tmp_path / "report.json"
    os.link(archive, alias)
    with pytest.raises(PermissionError, match="input of this run"):
        improvement_policy.assert_safe_output(alias, inputs=[archive])
    protected = tmp_path / "docs" / "self-improvement-archive.jsonl"
    protected.parent.mkdir()
    protected.write_text("{}\n")
    monkeypatch.setattr(improvement_policy, "REPO_ROOT", tmp_path)
    alias2 = tmp_path / "out.json"
    os.link(protected, alias2)
    with pytest.raises(PermissionError, match="same file as protected"):
        improvement_policy.assert_safe_output(alias2)


def test_scrub_secrets_removes_credential_shapes_but_keeps_the_failure_readable():
    scrub = improvement_policy.scrub_secrets
    out = scrub(
        "psql postgres://admin:FAKE_PASSWORD@db.internal:5432/app failed: connection refused"
    )
    assert "FAKE_PASSWORD" not in out and "postgres://admin:[REDACTED]@db.internal" in out
    assert "connection refused" in out
    out = scrub(
        'curl -H "Authorization: Bearer abcdef123456789" https://x; GITHUB_TOKEN=ghp_abcdefghijklmnop123 git push'
    )
    assert "abcdef123456789" not in out and "ghp_abcdefghijklmnop123" not in out
    assert "git push" in out
    assert (
        scrub("ordinary Exit code 1: 3 failed, 10 passed")
        == "ordinary Exit code 1: 3 failed, 10 passed"
    )


def test_scrub_secrets_covers_command_line_flag_forms():
    scrub = improvement_policy.scrub_secrets
    out = scrub(
        "mysql --host db --password FAKE_FLAG_PW --user admin; vault login -token=FAKE_TOK_1234 ok"
    )
    assert "FAKE_FLAG_PW" not in out and "FAKE_TOK_1234" not in out
    assert "--user admin" in out and "--host db" in out
    assert scrub("git --no-pager log --oneline -3") == "git --no-pager log --oneline -3"


def test_scrub_secrets_covers_basic_auth_and_quoted_flag_values():
    scrub = improvement_policy.scrub_secrets
    out = scrub("curl -H 'Authorization: Basic YWRtaW46RkFLRV9QQVNT' https://api; HTTP 401")
    assert "YWRtaW46RkFLRV9QQVNT" not in out and "HTTP 401" in out
    out = scrub('tool --api-key "FAKE_QUOTED_KEY_1" --region us; token FAKEBARE_TOKEN_22 rejected')
    assert "FAKE_QUOTED_KEY_1" not in out and "FAKEBARE_TOKEN_22" not in out
    assert "--region us" in out


def test_scrub_secrets_covers_short_options_and_quoted_values_with_spaces():
    scrub = improvement_policy.scrub_secrets
    out = scrub(
        'mysql -uadmin -pFAKE_PASSWORD_123 -h db; then --password "FAKE PASSWORD VALUE" again'
    )
    assert "FAKE_PASSWORD_123" not in out
    assert "FAKE PASSWORD VALUE" not in out and "PASSWORD VALUE" not in out
    assert "-h db" in out and "again" in out
    out = scrub("export DB_PASSWORD='two words here' && run")
    assert "two words" not in out and "&& run" in out


def test_scrub_secrets_covers_curl_user_password_arguments():
    scrub = improvement_policy.scrub_secrets
    out = scrub(
        "curl -u admin:FAKE_CURL_PW https://api/x -> HTTP 403; curl --user 'bob:FAKE TWO' https://y"
    )
    assert "FAKE_CURL_PW" not in out and "FAKE TWO" not in out
    assert "admin:" in out and "HTTP 403" in out
