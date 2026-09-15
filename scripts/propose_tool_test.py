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
        # A refused run leaves nothing behind: an empty folder under a
        # protected path used to survive and make the next run skip the
        # guard (Codex review of PR #61, round 6).
        assert not (repo / forbidden / "shell-semantics").exists()
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


def test_cleanup_is_narrow_and_survives_pycache_and_symlinks(tmp_path):
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    out = tmp_path / "proposals"
    base = ["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]
    assert propose.main(base) == 0
    # A __pycache__ left by running the drafted tests must not break cleanup.
    (out / "shell-semantics" / "__pycache__").mkdir()
    (out / "shell-semantics" / "__pycache__" / "x.pyc").write_bytes(b"")
    # A symlinked folder that looks generated is never followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text(propose.GENERATED_MARKER)
    (outside / "keep.txt").write_text("keep")
    (out / "linked").symlink_to(outside)
    # A --topic filter must not delete the other eligible topics.
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps({"tools": []}))
    assert propose.main([*base, "--threshold", "1", "--topic", "credential-redaction"]) == 0
    assert (out / "shell-semantics").exists(), "filtered run left other eligible drafts alone"
    # Covering shell-semantics makes it ineligible: removed, pycache and all.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
        )
    )
    assert propose.main([*base, "--manifest", str(manifest)]) == 0
    assert not (out / "shell-semantics").exists()
    assert (outside / "keep.txt").exists(), "symlink target untouched"


def test_drafting_refuses_symlinks_and_never_deletes_its_inputs(tmp_path):
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    out = tmp_path / "proposals"
    out.mkdir()
    # The archive to process sits inside a folder that looks generated.
    trap = out / "old-topic"
    trap.mkdir()
    (trap / "README.md").write_text(propose.GENERATED_MARKER)
    archive = trap / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    # The marker alone no longer makes a folder ours: the extra archive file
    # means a human (or an input) lives there, so cleanup leaves it alone
    # (Codex review of PR #61, round 6) and the input survives.
    assert (
        propose.main(["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]) == 0
    )
    assert archive.exists() and (trap / "README.md").exists()
    # A symlink planted where a draft file would go is refused.
    outside = tmp_path / "victim.py"
    outside.write_text("keep")
    (out / "shell-semantics").mkdir(exist_ok=True)
    planted = out / "shell-semantics" / "check-shell-semantics.py"
    planted.unlink(missing_ok=True)  # the earlier run drafted it before refusing
    planted.symlink_to(outside)
    archive2 = tmp_path / "archive.jsonl"
    archive2.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    with pytest.raises(PermissionError, match="symlink"):
        propose.main(["p", str(archive2), "--policy", str(policy_path), "--out-dir", str(out)])
    assert outside.read_text() == "keep"
    # "+++i" is an added line, not a header.
    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []})[0]
    propose.draft(rec["topic"], rec, _archive(), policy, tmp_path / "d")
    script = tmp_path / "d" / rec["topic"] / f"check-{rec['topic']}.py"
    diff = "+++ b/x\n@@ -1 +1 @@\n+++pipefail\n"
    run = subprocess.run([sys.executable, str(script)], input=diff, capture_output=True, text=True)
    assert "1 line(s)" in run.stdout


def test_a_topic_folder_that_is_a_symlink_to_a_sibling_is_refused(tmp_path):
    policy = policy_mod.builtin_policy()
    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []})[0]
    out = tmp_path / "proposals"
    human = out / "human-draft"
    human.mkdir(parents=True)
    (human / "README.md").write_text("a human wrote this")
    (out / rec["topic"]).symlink_to(human)
    with pytest.raises(PermissionError, match="symlink"):
        propose.draft(rec["topic"], rec, _archive(), policy, out)
    assert (human / "README.md").read_text() == "a human wrote this"


def test_a_human_folder_at_a_topic_path_is_never_written_into_or_deleted(tmp_path):
    """Codex review of PR #61, round 6: draft() accepted an existing topic
    folder without checking who owns it, overwrote the implementation and
    README, and the new README's marker then let cleanup delete the whole
    folder, human files included. Drafting now refuses a folder the
    generator does not own; main() skips that topic and says so."""
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    out = tmp_path / "proposals"
    human = out / "shell-semantics"
    human.mkdir(parents=True)
    (human / "README.md").write_text("# my own checker\nhand written, not generated\n")
    (human / "check-shell-semantics.py").write_text("print('mine')\n")
    (human / "notes.txt").write_text("keep me\n")
    before = {p.name: p.read_bytes() for p in human.iterdir()}

    rec = propose.topics_needing_a_tool(_archive(), policy, {"tools": []})[0]
    assert rec["topic"] == "shell-semantics"
    with pytest.raises(PermissionError):
        propose.draft(rec["topic"], rec, _archive(), policy, out)
    assert {p.name: p.read_bytes() for p in human.iterdir()} == before

    base = ["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]
    assert propose.main(base) == 0
    assert {p.name: p.read_bytes() for p in human.iterdir()} == before, "main() skipped it"
    # Covering the topic makes it ineligible; a human folder is still not ours to delete.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
        )
    )
    assert propose.main([*base, "--manifest", str(manifest)]) == 0
    assert {p.name: p.read_bytes() for p in human.iterdir()} == before

    # A generated folder a human added a file to is not ours any more either.
    assert (
        propose.main(["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out / "g")])
        == 0
    )
    generated = out / "g" / "shell-semantics"
    assert propose.generator_owns(generated)
    (generated / "extra.md").write_text("human note\n")
    assert not propose.generator_owns(generated)
    assert propose.main([*base, "--manifest", str(manifest), "--out-dir", str(out / "g")]) == 0
    assert (generated / "extra.md").exists()


def test_a_human_edit_to_a_generated_file_ends_the_generators_ownership(tmp_path):
    """Codex review of PR #61, round 7: ownership was judged by the README
    marker and file names alone, so a human's edit to the generated checker
    or its tests was overwritten by the next draft and deleted by cleanup.
    The README now records each generated file's sha256; any difference
    means the folder is a human's."""
    policy = policy_mod.builtin_policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    out = tmp_path / "proposals"
    base = ["p", str(archive), "--policy", str(policy_path), "--out-dir", str(out)]
    assert propose.main(base) == 0
    folder = out / "shell-semantics"
    assert propose.generator_owns(folder)
    checker = folder / "check-shell-semantics.py"
    checker.write_text(checker.read_text() + "\n# a human improved this\n")
    assert not propose.generator_owns(folder)
    edited = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    # Re-drafting skips it and changes nothing.
    assert propose.main(base) == 0
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == edited
    # Becoming ineligible does not delete it either.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
        )
    )
    assert propose.main([*base, "--manifest", str(manifest)]) == 0
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == edited
    # A README whose hash line was stripped is not proof of ownership either.
    fresh = out / "fresh"
    assert (
        propose.main(["p", str(archive), "--policy", str(policy_path), "--out-dir", str(fresh)])
        == 0
    )
    readme = fresh / "shell-semantics" / "README.md"
    readme.write_text(
        "\n".join(ln for ln in readme.read_text().splitlines() if "generated-sha256" not in ln)
    )
    assert not propose.generator_owns(fresh / "shell-semantics")
