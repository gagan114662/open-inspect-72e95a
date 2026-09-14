#!/usr/bin/env python3
"""Redact configured secret values from a text file.

Extracted from .github/workflows/codex-review.yml after that workflow's own
review process (Codex reviewing the workflow that runs Codex reviews) found
the inline version of this logic duplicated across two call sites — one for
the review's stdout, one for its stderr on a failed run — with the stderr
path initially missing the redaction entirely. A standalone, tested script
is the fix: one implementation, reusable everywhere a secret needs redacting
before untrusted-adjacent text is printed to logs or posted as a comment,
instead of being re-derived inline in each new workflow that needs it.

Secrets are read from environment variables, not arguments, so they never
appear in a process list. CODEX_AUTH_JSON is treated as a JSON document and
every string value inside it (regardless of key name) is redacted
independently — an individual field (e.g. a bare access token) echoed on its
own would survive redaction if only the whole serialized blob were matched.
CODEX_API_KEY / OPENAI_API_KEY / TRACES_API_KEY are treated as opaque
whole-value secrets. If CODEX_HOME is set and a readable auth.json exists
under it, that file's
*current* contents are collected too, on top of CODEX_AUTH_JSON's original
value — codex can rotate its own refresh token mid-run and write the new
value to that file; redacting only the value captured at the start of the
run would miss a rotated token that later appears in output.

Usage:
    python3 redact-secrets.py <src> <dst>

Reads CODEX_AUTH_JSON, CODEX_API_KEY, OPENAI_API_KEY, TRACES_API_KEY, and
CODEX_HOME from the environment. Writes <dst> with every occurrence of
every collected secret value replaced by [REDACTED]. If no secret is
configured, copies <src> to <dst> unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MIN_SECRET_LENGTH = 8


def collect_secret_strings(obj: object, out: set[str]) -> None:
    """Recursively collect every string value in a JSON-decoded structure.

    Short strings (below MIN_SECRET_LENGTH) are skipped to avoid redacting
    incidental short words that happen to match a trivial field value.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            collect_secret_strings(value, out)
    elif isinstance(obj, list):
        for value in obj:
            collect_secret_strings(value, out)
    elif isinstance(obj, str) and len(obj) >= MIN_SECRET_LENGTH:
        out.add(obj)


def collect_secrets_from_auth_json_text(auth_json: str, out: set[str]) -> None:
    if not auth_json:
        return
    try:
        collect_secret_strings(json.loads(auth_json), out)
    except ValueError:
        # Not valid JSON — treat the whole value as one opaque secret rather
        # than silently redacting nothing.
        out.add(auth_json)


def collect_secrets_from_env(env: dict[str, str]) -> set[str]:
    secrets: set[str] = set()

    collect_secrets_from_auth_json_text(env.get("CODEX_AUTH_JSON", ""), secrets)

    codex_home = env.get("CODEX_HOME", "")
    if codex_home:
        auth_json_path = Path(codex_home) / "auth.json"
        try:
            current_auth_json = auth_json_path.read_text()
        except OSError:
            current_auth_json = ""
        # Covers a refresh-token rotation that happened after the original
        # CODEX_AUTH_JSON env var was captured, mid-run.
        collect_secrets_from_auth_json_text(current_auth_json, secrets)

    for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "TRACES_API_KEY"):
        value = env.get(key, "")
        if value:
            secrets.add(value)

    return secrets


def redact(text: str, secrets: set[str]) -> str:
    # Longest-first: if one secret is a substring of another, redacting the
    # shorter one first would mutate the text before the longer match could
    # be found, leaving part of the longer secret exposed in the remainder.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: redact-secrets.py <src> <dst>", file=sys.stderr)
        return 2

    src, dst = argv[1], argv[2]
    secrets = collect_secrets_from_env(os.environ)

    with open(src, errors="replace") as f:
        text = f.read()

    if secrets:
        text = redact(text, secrets)

    with open(dst, "w") as f:
        f.write(text)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
