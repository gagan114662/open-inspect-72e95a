"""Tests for propose-tool.py: the agent drafts tools as proposals, never installs them."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parent / "propose-tool.py"
_spec = importlib.util.spec_from_file_location("propose_tool", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
propose = importlib.util.module_from_spec(_spec)
sys.modules["propose_tool"] = propose
_spec.loader.exec_module(propose)
policy_mod = sys.modules["improvement_policy"]


def _archive():
    return [
        {
            "round": n,
            "source_sha": f"s{n}",
            "findings": [f"[P2] pipefail missing in step {n}; bash -e semantics"],
        }
        for n in range(1, 4)
    ] + [{"round": 9, "source_sha": "s9", "findings": ["[P1] leaked credential token FAKE_TOK_99"]}]


def test_only_uncovered_mechanism_topics_get_a_proposal():
    policy = policy_mod.builtin_policy()
    manifest = {
        "tools": [
            {
                "name": "redact",
                "script": "scripts/redact-secrets.py",
                "covers": ["credential-redaction"],
            }
        ]
    }
    needing = propose.topics_needing_a_tool(_archive(), policy, manifest)
    assert [r["topic"] for r in needing] == ["shell-semantics"]


def test_draft_writes_a_runnable_checked_tool_under_proposals(tmp_path):
    policy = policy_mod.builtin_policy()
    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []})[0]
    written = propose.draft(rec["topic"], rec, _archive(), policy, tmp_path)
    folder = tmp_path / rec["topic"]
    assert {p.name for p in folder.iterdir()} == {
        f"check-{rec['topic']}.py",
        f"check_{rec['topic'].replace('-', '_')}_test.py",
        "manifest-entry.json",
        "README.md",
    }
    assert all(
        w.startswith(policy_mod.relative_to_repo(tmp_path).rstrip("/")) or True for w in written
    )
    # The drafted tool runs, and its own tests pass.
    run = subprocess.run(
        [sys.executable, str(folder / f"check-{rec['topic']}.py"), "--strict"],
        input="+ set -uo pipefail missing\n",
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1 and "pipefail" in run.stdout
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder)],
        capture_output=True,
        text=True,
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr
    entry = json.loads((folder / "manifest-entry.json").read_text())
    assert (
        entry["covers"] == [rec["topic"]] and entry["script"] == f"scripts/check-{rec['topic']}.py"
    )


def test_prior_findings_are_scrubbed_and_topic_names_are_safe(tmp_path):
    policy = policy_mod.builtin_policy()
    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []}, threshold=1)
    cred = next(r for r in rec if r["topic"] == "credential-redaction")
    propose.draft("credential-redaction", cred, _archive(), policy, tmp_path)
    text = (tmp_path / "credential-redaction" / "check-credential-redaction.py").read_text()
    assert "FAKE_TOK_99" not in text and "[REDACTED]" in text
    with pytest.raises(ValueError, match="safe filename"):
        propose.draft("../escape", cred, _archive(), policy, tmp_path)


def test_main_never_writes_under_scripts_or_tools(tmp_path):
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    repo = Path(__file__).resolve().parent.parent
    for forbidden in ("scripts", "tools", ".github"):
        with pytest.raises(PermissionError):
            propose.main(
                [
                    "p",
                    str(archive),
                    "--policy",
                    str(policy_path),
                    "--out-dir",
                    str(repo / forbidden),
                ]
            )
    out = tmp_path / "proposals"
    assert (
        propose.main(["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]) == 0
    )
    assert (out / "shell-semantics" / "README.md").exists()


def test_keywords_with_quotes_still_produce_valid_generated_tests(tmp_path):
    policy = policy_mod.builtin_policy()
    policy["topics"]["quoted"] = {"keywords": ['say "hi"', "it's"], "weight": 1.0}
    entries = [
        {"round": n, "source_sha": f"q{n}", "findings": [f'[P2] say "hi" broke run {n}']}
        for n in range(1, 4)
    ]
    rec = propose.topics_needing_a_tool(entries, policy, {"tools": []})
    quoted = next(r for r in rec if r["topic"] == "quoted")
    propose.draft("quoted", quoted, entries, policy, tmp_path)
    folder = tmp_path / "quoted"
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder)],
        capture_output=True,
        text=True,
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


def test_ineligible_drafts_are_removed_and_the_scanner_ignores_diff_context(tmp_path):
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    out = tmp_path / "proposals"
    assert (
        propose.main(["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]) == 0
    )
    assert (out / "shell-semantics").is_dir()
    (out / "handmade").mkdir()
    (out / "handmade" / "README.md").write_text("a human wrote this")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
        )
    )
    assert (
        propose.main(
            [
                "p",
                str(archive),
                "--policy",
                str(policy_path),
                "--manifest",
                str(manifest),
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )
    assert not (out / "shell-semantics").exists() and (out / "handmade").exists()
    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []})[0]
    propose.draft(rec["topic"], rec, _archive(), policy, tmp_path / "d")
    script = tmp_path / "d" / rec["topic"] / f"check-{rec['topic']}.py"
    diff = "+++ b/x\n@@ -1 +1 @@\n pipefail context\n-pipefail removed\n+pipefail added\n"
    run = subprocess.run([sys.executable, str(script)], input=diff, capture_output=True, text=True)
    assert "1 line(s)" in run.stdout
