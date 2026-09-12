"""Tests for GitHub App installation-token narrowing validation.

Covers the fail-closed contract get_installation_token/generate_installation_token
must uphold: an empty or inconsistent repository/permissions combination must
never silently mint a broader-than-intended (or fully unnarrowed) token.
"""

import pytest

from sandbox_runtime.auth.github_app import (
    SANDBOX_SCOPED_PERMISSIONS,
    generate_installation_token,
    get_installation_token,
)


def test_get_installation_token_rejects_permissions_without_repository():
    with pytest.raises(ValueError, match="permissions requires repository"):
        get_installation_token("jwt", "456", permissions={"contents": "write"})


def test_get_installation_token_rejects_empty_permissions_dict():
    """An empty {} must not silently drop narrowing and mint the full installation grant."""
    with pytest.raises(ValueError, match="permissions must be non-empty"):
        get_installation_token("jwt", "456", repository="repo", permissions={})


def test_get_installation_token_rejects_empty_repository_string():
    with pytest.raises(ValueError, match="repository must be non-empty"):
        get_installation_token("jwt", "456", repository="", permissions=SANDBOX_SCOPED_PERMISSIONS)


def _fake_client(response_body: dict, captured: dict):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return response_body

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    return FakeClient


def test_get_installation_token_sends_narrowed_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.httpx.Client",
        _fake_client(
            {
                "token": "ghs-scoped",
                "permissions": SANDBOX_SCOPED_PERMISSIONS,
                "repositories": [{"name": "repo"}],
            },
            captured,
        ),
    )

    token = get_installation_token(
        "jwt", "456", repository="repo", permissions=SANDBOX_SCOPED_PERMISSIONS
    )

    assert token == "ghs-scoped"
    assert captured["json"] == {
        "repositories": ["repo"],
        "permissions": SANDBOX_SCOPED_PERMISSIONS,
    }


def test_get_installation_token_rejects_grant_broader_than_requested(monkeypatch):
    """Defense in depth: GitHub returning more than asked for must fail loudly, not silently pass."""
    captured = {}
    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.httpx.Client",
        _fake_client(
            {
                "token": "ghs-broader",
                # Broader than requested: includes pull_requests write.
                "permissions": {**SANDBOX_SCOPED_PERMISSIONS, "pull_requests": "write"},
                "repositories": [{"name": "repo"}],
            },
            captured,
        ),
    )

    with pytest.raises(ValueError, match="does not match the requested permissions"):
        get_installation_token(
            "jwt", "456", repository="repo", permissions=SANDBOX_SCOPED_PERMISSIONS
        )


def test_get_installation_token_rejects_grant_with_extra_repository(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.httpx.Client",
        _fake_client(
            {
                "token": "ghs-broader-repo",
                "permissions": SANDBOX_SCOPED_PERMISSIONS,
                "repositories": [{"name": "repo"}, {"name": "sibling-nobody-asked-for"}],
            },
            captured,
        ),
    )

    with pytest.raises(ValueError, match="does not match the requested repositories"):
        get_installation_token(
            "jwt", "456", repository="repo", permissions=SANDBOX_SCOPED_PERMISSIONS
        )


def test_get_installation_token_unnarrowed_sends_no_body(monkeypatch):
    """Legacy unnarrowed callers (control-plane's own server-side mint) are unaffected."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "ghs-full"}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers, json):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("sandbox_runtime.auth.github_app.httpx.Client", FakeClient)

    token = get_installation_token("jwt", "456")

    assert token == "ghs-full"
    assert captured["json"] is None


def test_generate_installation_token_defaults_permissions_when_repository_given(monkeypatch):
    captured = {}

    def fake_get_installation_token(
        jwt_token, installation_id, *, repository=None, permissions=None
    ):
        captured["repository"] = repository
        captured["permissions"] = permissions
        return "ghs-scoped"

    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.get_installation_token", fake_get_installation_token
    )
    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.generate_jwt", lambda app_id, private_key: "jwt"
    )

    token = generate_installation_token("123", "key", "456", repository="repo")

    assert token == "ghs-scoped"
    assert captured == {"repository": "repo", "permissions": SANDBOX_SCOPED_PERMISSIONS}


def test_generate_installation_token_rejects_explicit_empty_permissions(monkeypatch):
    """Explicitly passing {} must not be silently replaced by the default — it's an error."""
    monkeypatch.setattr(
        "sandbox_runtime.auth.github_app.generate_jwt", lambda app_id, private_key: "jwt"
    )

    with pytest.raises(ValueError, match="permissions must be non-empty"):
        generate_installation_token("123", "key", "456", repository="repo", permissions={})
