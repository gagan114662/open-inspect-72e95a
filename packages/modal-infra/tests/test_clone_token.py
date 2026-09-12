"""Tests for VCS clone token resolution."""

from unittest.mock import MagicMock

import pytest

from src.clone_token import resolve_clone_token


@pytest.fixture(autouse=True)
def clear_clone_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        "SCM_PROVIDER",
        "GITLAB_ACCESS_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_resolve_clone_token_uses_gitlab_access_token(monkeypatch):
    monkeypatch.setenv("SCM_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_ACCESS_TOKEN", "glpat-token")

    assert resolve_clone_token() == "glpat-token"


def test_resolve_clone_token_returns_none_for_missing_gitlab_token(monkeypatch):
    monkeypatch.setenv("SCM_PROVIDER", "gitlab")

    assert resolve_clone_token() is None


def test_resolve_clone_token_returns_none_without_repo_context(monkeypatch):
    """No repo context must fail closed — never mint an unnarrowed, installation-wide token.

    Uses a plain recording mock rather than a raising stub: resolve_clone_token
    wraps the mint call in a broad `except Exception`, so a stub that raises
    AssertionError to signal "should not be called" is indistinguishable from
    a real narrowing failure — both paths return None either way, so the test
    would pass even if the guard that skips the call entirely were deleted.
    Asserting call counts on a mock that doesn't raise is what actually proves
    the guard fired.
    """
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

    mint = MagicMock(return_value="ghs-should-not-be-returned")
    monkeypatch.setattr("sandbox_runtime.auth.generate_installation_token", mint)

    assert resolve_clone_token() is None
    assert resolve_clone_token("acme", None) is None
    assert resolve_clone_token("acme", "") is None
    mint.assert_not_called()


def test_resolve_clone_token_narrows_to_repo_and_git_only_permissions(monkeypatch):
    """The token handed to a sandbox must be repo-scoped and permission-narrowed."""
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    captured = {}

    def fake_generate_installation_token(**kwargs):
        captured.update(kwargs)
        return "ghs-scoped-token"

    monkeypatch.setattr(
        "sandbox_runtime.auth.generate_installation_token", fake_generate_installation_token
    )

    assert resolve_clone_token("acme", "repo") == "ghs-scoped-token"
    assert captured == {
        "app_id": "123",
        "private_key": "private-key",
        "installation_id": "456",
        "repository": "repo",
        "permissions": {"contents": "write", "metadata": "read"},
    }
    # No pull_requests/issues write scope reaches a sandbox-bound token.
    assert "pull_requests" not in captured["permissions"]
    assert "issues" not in captured["permissions"]


def test_resolve_clone_token_returns_none_when_github_credentials_incomplete(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

    mint = MagicMock(return_value="ghs-should-not-be-returned")
    monkeypatch.setattr("sandbox_runtime.auth.generate_installation_token", mint)

    assert resolve_clone_token("acme", "repo") is None
    mint.assert_not_called()


def test_resolve_clone_token_returns_none_when_github_generation_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

    def raise_from_generate(**_kwargs):
        raise RuntimeError("token generation failed")

    monkeypatch.setattr("sandbox_runtime.auth.generate_installation_token", raise_from_generate)

    assert resolve_clone_token("acme", "repo") is None


def test_resolve_clone_token_fails_closed_does_not_retry_unnarrowed(monkeypatch):
    """A rejected narrowing request must not be retried with a broader grant."""
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    calls = []

    def reject_narrowing(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("422 Validation Failed: repositories not accessible")

    monkeypatch.setattr("sandbox_runtime.auth.generate_installation_token", reject_narrowing)

    assert resolve_clone_token("acme", "repo") is None
    # Exactly one attempt — a narrowed one — never a second, unnarrowed retry.
    assert len(calls) == 1
    assert calls[0]["repository"] == "repo"
