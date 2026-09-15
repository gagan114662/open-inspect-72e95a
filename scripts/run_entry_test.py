"""The root entry point runs the agent's tools in schedule order."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The smallest checkout the agent can run in: its tools, its records, its
# generated folders. The entry test runs against a copy of this, never the
# real checkout (Codex review of PR #68, round 3).
FIXTURE_PATHS = (
    "run.py",
    "agent.json",
    "scripts",
    "tools/manifest.json",
    "skills",
    "docs/improvement-policy.json",
    "docs/improvement-policy-history.jsonl",
    "docs/self-improvement-archive.jsonl",
    "docs/rsi",
)


def load_run(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    return run


def copy_fixture(dest: Path) -> Path:
    for rel in FIXTURE_PATHS:
        src = ROOT / rel
        if not src.exists():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(
                src, target, ignore=shutil.ignore_patterns("__pycache__", ".refresh", "*_test.py")
            )
        else:
            shutil.copyfile(src, target)
    return dest


def checkout_state() -> tuple[str, dict[str, float]]:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    mtimes = {
        rel: (ROOT / rel).stat().st_mtime
        for rel in ("docs/rsi/measurement.json", "docs/rsi/dashboard.html")
        if (ROOT / rel).exists()
    }
    return status, mtimes


def test_run_help_and_dry_run_against_a_copy_of_the_checkout(tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0 and "--refresh" in out.stdout and "--apply" in out.stdout
    assert "--root" in out.stdout
    if not (ROOT / "docs" / "self-improvement-archive.jsonl").exists():
        return
    fixture = copy_fixture(tmp_path / "repo")
    before = checkout_state()
    run = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--root", str(fixture)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    for name in (
        "measure",
        "decide (dry run)",
        "render dashboard",
        "distill skills",
        "propose tools",
    ):
        assert f"] {name}:" in run.stdout, run.stdout
    # The outputs landed in the copy, and the real checkout is untouched.
    assert (fixture / "docs" / "rsi" / "dashboard.html").exists()
    assert checkout_state() == before


def test_refresh_refuses_an_evidence_destination_that_links_to_the_archive(tmp_path, monkeypatch):
    run = load_run("run_entry_symlink")
    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "improvement_policy.py", root / "scripts" / "improvement_policy.py"
    )
    archive = root / "docs" / "self-improvement-archive.jsonl"
    archive.write_text('{"round": 1}\n')
    evidence = root / "docs" / "rsi" / "trace-evidence.json"
    evidence.symlink_to(archive)
    (root / "scripts" / "mine-trace-failures.py").write_text(
        "import sys, json\na=sys.argv\n"
        "open(a[a.index('--save-evidence')+1],'w').write(json.dumps({'sessions': ['s1'], 'topics': {}}))\n"
        "open(a[a.index('--out-json')+1],'w').write('{}')\nprint('1 distinct failure(s)')\n"
    )
    monkeypatch.setattr(run, "ROOT", root)
    assert run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)]) != 0
    assert archive.read_text() == '{"round": 1}\n'
    assert evidence.is_symlink()


def test_refresh_refuses_a_hard_link_to_the_policy(tmp_path, monkeypatch):
    run = load_run("run_entry_hardlink")
    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "improvement_policy.py", root / "scripts" / "improvement_policy.py"
    )
    policy = root / "docs" / "improvement-policy.json"
    shutil.copyfile(ROOT / "docs" / "improvement-policy.json", policy)
    original = policy.read_bytes()
    (root / "docs" / "self-improvement-archive.jsonl").write_text("")
    evidence = root / "docs" / "rsi" / "trace-evidence.json"
    evidence.hardlink_to(policy)
    (root / "scripts" / "mine-trace-failures.py").write_text(
        "import sys, json\na=sys.argv\n"
        "open(a[a.index('--save-evidence')+1],'w').write(json.dumps({'sessions': ['s1'], 'topics': {}}))\n"
        "open(a[a.index('--out-json')+1],'w').write('{}')\nprint('1 distinct failure(s)')\n"
    )
    monkeypatch.setattr(run, "ROOT", root)
    assert run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)]) != 0
    assert policy.read_bytes() == original


def test_refresh_replaces_a_regular_evidence_file(tmp_path, monkeypatch):
    run = load_run("run_entry_regular")
    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "improvement_policy.py", root / "scripts" / "improvement_policy.py"
    )
    (root / "docs" / "self-improvement-archive.jsonl").write_text("")
    evidence = root / "docs" / "rsi" / "trace-evidence.json"
    evidence.write_text('{"sessions": ["old"], "topics": {}}')
    (root / "scripts" / "mine-trace-failures.py").write_text(
        "import sys, json\na=sys.argv\n"
        "open(a[a.index('--save-evidence')+1],'w').write(json.dumps({'sessions': ['s1'], 'topics': {}}))\n"
        "open(a[a.index('--out-json')+1],'w').write('{}')\nprint('1 distinct failure(s)')\n"
    )
    for name in (
        "measure-policy-validity.py",
        "revise-improvement-policy.py",
        "render-rsi-dashboard.py",
        "distill-skills.py",
    ):
        (root / "scripts" / name).write_text("print('policy v1 (x): ok')\n")
    monkeypatch.setattr(run, "ROOT", root)
    assert run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)]) == 0
    assert json.loads(evidence.read_text())["sessions"] == ["s1"]


def test_refresh_keeps_the_committed_evidence_when_nothing_was_observed(tmp_path, monkeypatch):
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location("run_entry", ROOT / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "scripts").mkdir()
    committed = root / "docs" / "rsi" / "trace-evidence.json"
    committed.write_text('{"sessions": ["real"], "topics": {}}')
    (root / "docs" / "self-improvement-archive.jsonl").write_text("")
    (root / "scripts" / "mine-trace-failures.py").write_text(
        "import sys, json\na=sys.argv\nopen(a[a.index('--save-evidence')+1],'w').write(json.dumps({'sessions': [], 'topics': {}}))\n"
        "open(a[a.index('--out-json')+1],'w').write('{}')\nprint('0 distinct failure(s)')\n"
    )
    for name in (
        "measure-policy-validity.py",
        "revise-improvement-policy.py",
        "render-rsi-dashboard.py",
        "distill-skills.py",
    ):
        (root / "scripts" / name).write_text("print('policy v1 (x): ok')\n")
    monkeypatch.setattr(run, "ROOT", root)
    assert run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)]) == 0
    assert _json.loads(committed.read_text())["sessions"] == ["real"]


def test_repo_dir_is_resolved_against_the_callers_directory(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_entry2", ROOT / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    seen = {}

    def fake_step(name, argv):
        if name == "mine field failures":
            seen["repo_dir"] = argv[argv.index("--repo-dir") + 1]
            fresh = Path(argv[argv.index("--save-evidence") + 1])
            fresh.write_text('{"sessions": []}')
        return 0

    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "docs" / "self-improvement-archive.jsonl").write_text("")
    monkeypatch.setattr(run, "ROOT", root)
    monkeypatch.setattr(run, "step", fake_step)
    monkeypatch.setattr(
        run.subprocess,
        "run",
        lambda *_a, **_k: type(
            "R", (), {"returncode": 0, "stdout": "policy v1 (x): ok", "stderr": ""}
        )(),
    )
    monkeypatch.chdir(tmp_path)
    assert run.main(["run.py", "--refresh", "--repo-dir", "."]) == 0
    assert seen["repo_dir"] == str(tmp_path.resolve())


def test_refresh_never_writes_through_a_planted_staging_link(tmp_path, monkeypatch):
    """Codex review of PR #68, round 5: the destination was validated but the
    staging file next to it was not, so `trace-evidence.json.tmp` planted as
    a symlink or hard link to the archive let the copy overwrite the archive
    before the atomic replace. The staging file is now created exclusively
    under a unique name, so a planted path is never opened for writing."""
    run = load_run("run_entry_staging")
    root = tmp_path / "repo"
    (root / "docs" / "rsi").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "improvement_policy.py", root / "scripts" / "improvement_policy.py"
    )
    archive = root / "docs" / "self-improvement-archive.jsonl"
    archive.write_text('{"round": 1}\n')
    policy = root / "docs" / "improvement-policy.json"
    shutil.copyfile(ROOT / "docs" / "improvement-policy.json", policy)
    original_policy = policy.read_bytes()
    evidence = root / "docs" / "rsi" / "trace-evidence.json"
    evidence.write_text("{}")
    # Both link kinds at the old staging path.
    (root / "docs" / "rsi" / "trace-evidence.json.tmp").symlink_to(archive)
    (root / "scripts" / "mine-trace-failures.py").write_text(
        "import sys, json\na=sys.argv\n"
        "open(a[a.index('--save-evidence')+1],'w').write(json.dumps({'sessions': ['s1'], 'topics': {}}))\n"
        "open(a[a.index('--out-json')+1],'w').write('{}')\nprint('1 distinct failure(s)')\n"
    )
    monkeypatch.setattr(run, "ROOT", root)
    run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)])
    assert archive.read_text() == '{"round": 1}\n', "symlinked staging path was written through"
    assert json.loads(evidence.read_text()) == {"sessions": ["s1"], "topics": {}}
    (root / "docs" / "rsi" / "trace-evidence.json.tmp").unlink()
    (root / "docs" / "rsi" / "trace-evidence.json.tmp").hardlink_to(policy)
    run.main(["run.py", "--refresh", "--repo-dir", str(tmp_path)])
    assert policy.read_bytes() == original_policy, "hard-linked staging path was written through"
    leftovers = [
        p.name
        for p in (root / "docs" / "rsi").iterdir()
        if p.name.startswith("trace-evidence.json.") and p.name != "trace-evidence.json.tmp"
    ]
    assert leftovers == [], f"staging files left behind: {leftovers}"
