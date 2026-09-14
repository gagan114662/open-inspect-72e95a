"""Tests for parse-review-findings.py.

Run with: python3 -m pytest scripts/parse_review_findings_test.py -q
"""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "parse-review-findings.py"
_spec = importlib.util.spec_from_file_location("parse_review_findings", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
parse_mod = importlib.util.module_from_spec(_spec)
sys.modules["parse_review_findings"] = parse_mod
_spec.loader.exec_module(parse_mod)


def test_parses_a_real_multi_finding_review_comment():
    """Uses the actual shape of a real round-4 review comment from this
    archive, not a synthetic example."""
    text = (
        "### Codex independent review\n\n"
        '1. **[P1]** The "Check for Codex credentials" step embeds all '
        "configured credentials directly into the generated shell script.\n"
        "That temporary file remains readable by the reviewing agent.\n\n"
        "2. **[P2]** Subscription authentication discards refreshed "
        "credentials.\n\n"
        "3. **[P2]** The command-failure handler is unreachable on nonzero "
        "exits.\n\n"
        "Static review only; local execution was blocked by the sandbox.\n"
    )
    findings = parse_mod.parse_findings(text)
    assert len(findings) == 3
    assert findings[0].startswith("**[P1]**")
    assert findings[1].startswith("**[P2]**")
    assert findings[2].startswith("**[P2]**")


def test_no_findings_in_a_clean_review():
    text = "### Codex independent review\n\nNo issues found. This diff looks correct.\n"
    assert parse_mod.parse_findings(text) == []


def test_ignores_non_finding_numbered_lists():
    """A numbered list that isn't in the **[P1]**/**[P2]** finding format
    (e.g. a step-by-step explanation inside a finding, or unrelated prose)
    must not be misparsed as a finding."""
    text = (
        "1. **[P1]** Real finding here.\n\n"
        "Steps to reproduce:\n"
        "1. Open the file\n"
        "2. Run the command\n"
        "3. Observe the crash\n"
    )
    findings = parse_mod.parse_findings(text)
    assert findings == ["**[P1]** Real finding here."]


def test_main_cli_reads_from_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("1. **[P1]** stdin finding\n"))
    exit_code = parse_mod.main(["parse-review-findings.py"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "stdin finding" in out


def test_main_cli_reads_from_file(tmp_path):
    f = tmp_path / "review.txt"
    f.write_text("1. **[P1]** file finding\n")
    exit_code = parse_mod.main(["parse-review-findings.py", str(f)])
    assert exit_code == 0
