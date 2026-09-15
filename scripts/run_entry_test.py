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
