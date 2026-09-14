"""Tests for redact-secrets.py.

Run with: python3 -m pytest scripts/redact_secrets_test.py -q
(stdlib-only script; no project venv required.)
"""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "redact-secrets.py"
_spec = importlib.util.spec_from_file_location("redact_secrets", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
redact_secrets = importlib.util.module_from_spec(_spec)
sys.modules["redact_secrets"] = redact_secrets
_spec.loader.exec_module(redact_secrets)


def test_redacts_whole_api_key():
    secrets = redact_secrets.collect_secrets_from_env({"OPENAI_API_KEY": "sk-abcdefgh12345678"})
    out = redact_secrets.redact("token is sk-abcdefgh12345678 here", secrets)
    assert out == "token is [REDACTED] here"


def test_redacts_individual_json_field_not_just_whole_blob():
    """The exact regression this script exists to prevent: an individually
    echoed field value inside CODEX_AUTH_JSON must be redacted even though
    the text never contains the full serialized JSON blob."""
    auth_json = (
        '{"tokens":{"access_token":"access-tok-abc12345",'
        '"refresh_token":"refresh-tok-def67890"},"account_id":"acct-1"}'
    )
    secrets = redact_secrets.collect_secrets_from_env({"CODEX_AUTH_JSON": auth_json})

    out = redact_secrets.redact("leaked: access-tok-abc12345", secrets)
    assert out == "leaked: [REDACTED]"

    out2 = redact_secrets.redact("also leaked: refresh-tok-def67890", secrets)
    assert out2 == "also leaked: [REDACTED]"

    # The full blob concatenated with itself must not appear unredacted either.
    out3 = redact_secrets.redact(auth_json, secrets)
    assert "access-tok-abc12345" not in out3
    assert "refresh-tok-def67890" not in out3


def test_multiple_secrets_configured_simultaneously_each_redacted_independently():
    """The bug an earlier version of this logic had: concatenating all
    configured secrets into one string before matching meant only the full
    concatenation, not each individual value, was ever actually redacted."""
    env = {
        "CODEX_AUTH_JSON": '{"access_token":"json-secret-value-1"}',
        "CODEX_API_KEY": "api-key-secret-value-2",
    }
    secrets = redact_secrets.collect_secrets_from_env(env)

    out = redact_secrets.redact("only json-secret-value-1 appears here", secrets)
    assert out == "only [REDACTED] appears here"

    out2 = redact_secrets.redact("only api-key-secret-value-2 appears here", secrets)
    assert out2 == "only [REDACTED] appears here"


def test_short_field_values_are_not_redacted():
    """A short, incidental field value (e.g. a boolean-ish string) should not
    cause overzealous redaction of ordinary short words in review output."""
    auth_json = '{"active": "yes", "mode": "cli"}'
    secrets = redact_secrets.collect_secrets_from_env({"CODEX_AUTH_JSON": auth_json})
    assert "yes" not in secrets
    assert "cli" not in secrets


def test_no_secrets_configured_leaves_text_unchanged():
    secrets = redact_secrets.collect_secrets_from_env({})
    out = redact_secrets.redact("nothing sensitive here", secrets)
    assert out == "nothing sensitive here"


def test_invalid_json_auth_value_is_still_redacted_as_opaque_string():
    env = {"CODEX_AUTH_JSON": "not-actually-json-but-still-secret"}
    secrets = redact_secrets.collect_secrets_from_env(env)
    out = redact_secrets.redact("leak: not-actually-json-but-still-secret", secrets)
    assert out == "leak: [REDACTED]"


def test_main_cli_redacts_file_contents(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("access-tok-abc12345 was in the output")

    import os

    old_env = dict(os.environ)
    try:
        os.environ["CODEX_AUTH_JSON"] = '{"access_token":"access-tok-abc12345"}'
        os.environ.pop("CODEX_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        exit_code = redact_secrets.main(["redact-secrets.py", str(src), str(dst)])
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert exit_code == 0
    assert dst.read_text() == "[REDACTED] was in the output"


def test_main_cli_copies_unchanged_when_no_secrets_configured(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("plain text, nothing secret")

    import os

    old_env = dict(os.environ)
    try:
        for key in ("CODEX_AUTH_JSON", "CODEX_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(key, None)
        exit_code = redact_secrets.main(["redact-secrets.py", str(src), str(dst)])
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert exit_code == 0
    assert dst.read_text() == "plain text, nothing secret"


def test_main_rejects_wrong_arg_count():
    assert redact_secrets.main(["redact-secrets.py", "only-one-arg"]) == 2
