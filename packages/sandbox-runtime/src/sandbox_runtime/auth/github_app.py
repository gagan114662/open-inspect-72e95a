"""
GitHub App token generation for git operations.

Generates short-lived installation access tokens for:
- Cloning private repositories during image builds
- Git fetch/sync at sandbox startup
- Git push when creating pull requests

Tokens are valid for ~1 hour.
"""

import time

import httpx
import jwt


def generate_jwt(app_id: str, private_key: str) -> str:
    """
    Generate a JWT for GitHub App authentication.

    Args:
        app_id: The GitHub App's ID
        private_key: The App's private key (PEM format)

    Returns:
        Signed JWT valid for 10 minutes
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60 seconds ago (clock skew tolerance)
        "exp": now + 600,  # Expires in 10 minutes
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


# Default permission set for tokens handed to a sandbox: git push only,
# never pull_requests/issues write. See generate_installation_token.
SANDBOX_SCOPED_PERMISSIONS: dict[str, str] = {"contents": "write", "metadata": "read"}


def get_installation_token(
    jwt_token: str,
    installation_id: str,
    *,
    repository: str | None = None,
    permissions: dict[str, str] | None = None,
) -> str:
    """
    Exchange a JWT for an installation access token.

    Args:
        jwt_token: The signed JWT
        installation_id: The GitHub App installation ID
        repository: If given (not None), narrow the minted token to this
            single repository (name only, not "owner/repo") via GitHub's
            optional `repositories` request field.
        permissions: If given (not None), narrow the minted token to this
            permission subset via GitHub's optional `permissions` request
            field. Requires `repository` — GitHub's `permissions` field
            without `repositories` narrows nothing (still installation-wide).

    Returns:
        Installation access token (valid for 1 hour), narrowed to
        `repository`/`permissions` when supplied. GitHub rejects a
        malformed or over-broad narrowing request outright (raises here) —
        there is no fallback to an unnarrowed token.

    Raises:
        ValueError: if `permissions` is given without `repository`, or if
            either is an empty container — an empty `permissions={}` would
            make GitHub omit the field entirely and return the installation's
            full, unnarrowed permission set, which is exactly the silent
            widening this function must never do.
        httpx.HTTPStatusError: If the GitHub API request fails
    """
    if permissions is not None and repository is None:
        raise ValueError("permissions requires repository — it narrows nothing on its own")
    if repository is not None and not repository:
        raise ValueError("repository must be non-empty when provided")
    if permissions is not None and not permissions:
        raise ValueError("permissions must be non-empty when provided")

    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body: dict[str, object] = {}
    if repository is not None:
        body["repositories"] = [repository]
        if permissions is not None:
            body["permissions"] = permissions

    with httpx.Client() as client:
        response = client.post(url, headers=headers, json=body or None)
        response.raise_for_status()
        data = response.json()

    if repository is not None:
        _assert_grant_not_broader_than_requested(
            data, repositories=[repository], permissions=permissions
        )
    return data["token"]


def _assert_grant_not_broader_than_requested(
    data: dict[str, object],
    *,
    repositories: list[str],
    permissions: dict[str, str] | None,
) -> None:
    """Defense in depth: a scoped mint's response must grant exactly what was
    requested, never more. This never triggers under GitHub's documented,
    correct behavior; it exists to fail loudly rather than silently hand a
    sandbox-bound credential a broader grant if that assumption is ever
    wrong (an API bug or future behavior change).
    """
    granted_permissions = data.get("permissions")
    if not isinstance(granted_permissions, dict) or granted_permissions != (permissions or {}):
        raise ValueError(
            "Scoped installation token grant does not match the requested permissions: "
            f"requested {permissions!r}, granted {granted_permissions!r}"
        )

    granted_repos_raw = data.get("repositories")
    if not isinstance(granted_repos_raw, list):
        raise ValueError("Scoped installation token response is missing repositories")
    granted_repo_names = {repo.get("name") for repo in granted_repos_raw if isinstance(repo, dict)}
    if granted_repo_names != set(repositories):
        raise ValueError(
            "Scoped installation token grant does not match the requested repositories: "
            f"requested {repositories!r}, granted {sorted(n for n in granted_repo_names if n)!r}"
        )


def generate_installation_token(
    app_id: str,
    private_key: str,
    installation_id: str,
    *,
    repository: str | None = None,
    permissions: dict[str, str] | None = None,
) -> str:
    """
    Generate a fresh GitHub App installation token.

    This is the main entry point for token generation. It:
    1. Creates a JWT signed with the App's private key
    2. Exchanges it for an installation access token

    Args:
        app_id: The GitHub App's ID
        private_key: The App's private key (PEM format)
        installation_id: The GitHub App installation ID
        repository: If given, narrow the token to this single repository
            (see get_installation_token).
        permissions: If given, narrow the token's permissions (see
            get_installation_token). Defaults to SANDBOX_SCOPED_PERMISSIONS
            when `repository` is given and this is omitted, since every
            current caller of the narrowed path is minting a
            sandbox-reachable credential.

    Returns:
        Installation access token (valid for 1 hour)

    Raises:
        httpx.HTTPStatusError: If the GitHub API request fails
        jwt.PyJWTError: If JWT encoding fails
    """
    jwt_token = generate_jwt(app_id, private_key)
    resolved_permissions = permissions
    if repository and resolved_permissions is None:
        resolved_permissions = SANDBOX_SCOPED_PERMISSIONS
    return get_installation_token(
        jwt_token,
        installation_id,
        repository=repository,
        permissions=resolved_permissions,
    )
