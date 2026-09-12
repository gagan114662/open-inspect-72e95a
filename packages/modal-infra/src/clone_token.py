"""Resolve VCS clone tokens for Modal sandbox git operations."""

import os

from .log_config import get_logger

log = get_logger("clone_token")


def resolve_clone_token(repo_owner: str | None = None, repo_name: str | None = None) -> str | None:
    """Return a provider-specific clone token, or None when credentials are unavailable.

    For GitHub, the minted token is always narrowed to ``repo_name`` with
    git-only permissions (contents:write, metadata:read) — this token is
    injected directly into a sandbox's environment, so it must never carry
    more than git operations need. Missing repo context is treated as "no
    token available" rather than minting an unnarrowed, installation-wide
    token: this resolver exists specifically to produce sandbox-bound
    credentials, so it must fail closed on its own, independent of whether
    every caller already guards the missing-context case. A narrowing
    failure is likewise NOT retried unnarrowed: it is logged and treated
    the same as "no token available" (fail closed), never silently widened
    to the full installation grant.
    """
    from sandbox_runtime.auth import SANDBOX_SCOPED_PERMISSIONS, generate_installation_token

    scm_provider = os.environ.get("SCM_PROVIDER", "github")

    if scm_provider == "gitlab":
        token = os.environ.get("GITLAB_ACCESS_TOKEN")
        if not token:
            log.warn("gitlab.token_missing")
        return token

    if not repo_name:
        log.warn("github.repo_context_missing", repo_owner=repo_owner, repo_name=repo_name)
        return None

    try:
        app_id = os.environ.get("GITHUB_APP_ID")
        private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")

        if app_id and private_key and installation_id:
            return generate_installation_token(
                app_id=app_id,
                private_key=private_key,
                installation_id=installation_id,
                repository=repo_name,
                permissions=SANDBOX_SCOPED_PERMISSIONS,
            )
    except Exception as e:
        log.warn("github.token_error", exc=e, repo_owner=repo_owner, repo_name=repo_name)

    return None
