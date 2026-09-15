"""The root entry point runs the agent's tools in schedule order."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_run_help_and_dry_run_from_the_repository_root(tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0 and "--refresh" in out.stdout and "--apply" in out.stdout
    if not (ROOT / "docs" / "self-improvement-archive.jsonl").exists():
        return
    run = subprocess.run(
        [sys.executable, str(ROOT / "run.py")], capture_output=True, text=True, cwd=tmp_path
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
