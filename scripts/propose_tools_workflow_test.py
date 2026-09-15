"""The propose-tools step of archive-and-recommend.yml, run for real against a
bare origin with stubbed `gh`: a human commit on the standing branch survives
a later drafting run (Codex review of PR #61, round 8)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "archive-and-recommend.yml"

GH_STUB = """#!/usr/bin/env bash
# Minimal gh: default branch is main, no open PRs, creation is recorded.
case "$1 $2" in
  "repo view") echo main ;;
  "pr list") echo "" ;;
  "pr create") echo "created" >> "$GH_LOG"; echo "https://example.invalid/pr/1" ;;
  "pr close") echo "closed $3" >> "$GH_LOG" ;;
  *) echo "unexpected gh $*" >&2; exit 1 ;;
esac
"""


def _step_script() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    steps = jobs["propose-tools"]["steps"]
    (step,) = [s for s in steps if s.get("name", "").startswith("Draft proposals")]
    return step["run"]


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _archive() -> list[dict]:
    return [
        {
            "round": n,
            "source_sha": f"s{n}",
            "findings": [f"[P2] pipefail missing in step {n}; bash -e semantics"],
        }
        for n in range(1, 4)
    ]


def _seed_repo(tmp_path: Path) -> tuple[Path, Path, dict]:
    """A bare origin whose main holds the scripts and archive, and a clone
    checked out at main, the way actions/checkout leaves the runner."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(tmp_path),
    }
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin), env=env)
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed), env=env)
    (seed / "scripts").mkdir()
    for name in ("propose-tool.py", "improvement_policy.py", "detect-recurring-pattern.py"):
        shutil.copyfile(ROOT / "scripts" / name, seed / "scripts" / name)
    (seed / "docs").mkdir()
    shutil.copyfile(
        ROOT / "docs" / "improvement-policy.json", seed / "docs" / "improvement-policy.json"
    )
    (seed / "docs" / "self-improvement-archive.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _archive()) + "\n"
    )
    _git(seed, "add", "-A", env=env)
    _git(seed, "commit", "-q", "-m", "main: scripts and archive", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)
    return origin, seed, env


def _run_step(workspace: Path, env: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    script = tmp_path / "step.sh"
    script.write_text(_step_script())
    return subprocess.run(
        ["bash", str(script)],
        cwd=workspace,
        env={
            **env,
            "PATH": f"{bin_dir}:{env['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GH_LOG": str(tmp_path / "gh.log"),
            "GH_TOKEN": "x",
        },
        capture_output=True,
        text=True,
    )


def test_a_human_commit_on_the_standing_branch_survives_the_next_drafting_run(tmp_path):
    origin, seed, env = _seed_repo(tmp_path)
    # First run: no standing branch yet; drafts are proposed on a new one.
    ws1 = tmp_path / "ws1"
    _git(tmp_path, "clone", "-q", str(origin), str(ws1), env=env)
    run = _run_step(ws1, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "created" in (tmp_path / "gh.log").read_text()
    first = _git(ws1, "rev-parse", "origin/tool-proposals", env=env)
    # A human fixes the generated checker on the standing branch.
    human = tmp_path / "human"
    _git(tmp_path, "clone", "-q", "-b", "tool-proposals", str(origin), str(human), env=env)
    checker = human / "proposals" / "tools" / "shell-semantics" / "check-shell-semantics.py"
    assert checker.exists()
    checker.write_text(checker.read_text() + "\n# reviewed by a human: keep this fix\n")
    (human / "proposals" / "NOTES.md").write_text("human notes\n")
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "fix the draft by hand", env=env)
    human_sha = _git(human, "rev-parse", "HEAD", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    # Main moves on (a new round), which triggers the job again.
    (seed / "docs" / "self-improvement-archive.jsonl").open("a").write(
        json.dumps({"round": 4, "source_sha": "s4", "findings": ["[P2] pipefail again"]}) + "\n"
    )
    _git(seed, "commit", "-q", "-am", "one more round", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    head = _git(ws2, "rev-parse", "origin/tool-proposals", env=env)
    assert head != first
    _git(ws2, "merge-base", "--is-ancestor", human_sha, head, env=env)
    tree = _git(ws2, "ls-tree", "-r", "--name-only", head, env=env).splitlines()
    assert "proposals/NOTES.md" in tree, "the human's file is still on the branch"
    edited = _git(
        ws2, "show", f"{head}:proposals/tools/shell-semantics/check-shell-semantics.py", env=env
    )
    assert "reviewed by a human: keep this fix" in edited, "the human's edit was not overwritten"
    assert _git(ws2, "merge-base", "--is-ancestor", "origin/main", head, env=env) == ""


def test_the_step_never_uses_a_plain_force_push():
    script = _step_script()
    assert "git push --force origin" not in script
    assert 'git push --force-with-lease="refs/heads/${branch}:${lease}"' in script


if __name__ == "__main__":
    sys.exit(0)
