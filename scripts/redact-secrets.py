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
CODEX_API_KEY / OPENAI_API_KEY are treated as opaque whole-value secrets.

Usage:
    python3 redact-secrets.py <src> <dst>

Reads CODEX_AUTH_JSON, CODEX_API_KEY, OPENAI_API_KEY from the environment.
Writes <dst> with every occurrence of every collected secret value replaced
by [REDACTED]. If no secret is configured, copies <src> to <dst> unchanged.
"""

from __future__ import annotations

import json
import os
import sys

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


def collect_secrets_from_env(env: dict[str, str]) -> set[str]:
    secrets: set[str] = set()

    auth_json = env.get("CODEX_AUTH_JSON", "")
    if auth_json:
        try:
            collect_secret_strings(json.loads(auth_json), secrets)
        except ValueError:
            # Not valid JSON — treat the whole value as one opaque secret
            # rather than silently redacting nothing.
            secrets.add(auth_json)

    for key in ("CODEX_API_KEY", "OPENAI_API_KEY"):
        value = env.get(key, "")
        if value:
            secrets.add(value)

    return secrets


def redact(text: str, secrets: set[str]) -> str:
    for secret in secrets:
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
