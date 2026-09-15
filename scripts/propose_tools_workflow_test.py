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
  "pr list") if [ -n "${GH_OPEN_PR:-}" ]; then echo "$GH_OPEN_PR"; else echo ""; fi ;;
  "pr create")
    if [ -n "${GH_FAIL_CREATE_ONCE:-}" ] && [ -f "$GH_FAIL_CREATE_ONCE" ]; then
      rm -f "$GH_FAIL_CREATE_ONCE"; echo "create failed (simulated)" >&2; exit 1
    fi
    echo "created" >> "$GH_LOG"; echo "https://example.invalid/pr/1" ;;
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


def _run_step(
    workspace: Path, env: dict, tmp_path: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
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
            **(extra_env or {}),
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


def _first_run(tmp_path: Path):
    origin, seed, env = _seed_repo(tmp_path)
    ws1 = tmp_path / "ws1"
    _git(tmp_path, "clone", "-q", str(origin), str(ws1), env=env)
    run = _run_step(ws1, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    return origin, seed, env


def _human_clone(tmp_path: Path, origin: Path, env: dict) -> Path:
    human = tmp_path / "human"
    _git(tmp_path, "clone", "-q", "-b", "tool-proposals", str(origin), str(human), env=env)
    return human


def _advance_main(seed: Path, env: dict, extra: dict | None = None) -> None:
    (seed / "docs" / "self-improvement-archive.jsonl").open("a").write(
        json.dumps({"round": 4, "source_sha": "s4", "findings": ["[P2] pipefail again"]}) + "\n"
    )
    for rel, text in (extra or {}).items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    _git(seed, "add", "-A", env=env)
    _git(seed, "commit", "-q", "-m", "one more round", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)


def test_a_merge_conflict_stops_the_run_and_keeps_the_human_branch(tmp_path):
    """Codex review of PR #61, round 9, finding 1: a failed merge used to
    rebuild the standing branch from the default branch and force-push it,
    so a human commit vanished. Now: abort, warn, push nothing."""
    origin, seed, env = _first_run(tmp_path)
    human = _human_clone(tmp_path, origin, env)
    (human / "proposals" / "NOTES.md").write_text("human notes\n")
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "notes by hand", env=env)
    human_sha = _git(human, "rev-parse", "HEAD", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    # Main changes the same file differently: the merge cannot be automatic.
    _advance_main(seed, env, {"proposals/NOTES.md": "main's notes\n"})
    (tmp_path / "gh.log").write_text("")
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "::warning::" in run.stdout and "conflict" in run.stdout.lower()
    _git(ws2, "fetch", "-q", "origin", "tool-proposals", env=env)
    assert _git(ws2, "rev-parse", "origin/tool-proposals", env=env) == human_sha, (
        "nothing was pushed over the human's branch"
    )
    assert (tmp_path / "gh.log").read_text() == "", "no PR call was made"


def test_the_generator_runs_from_the_default_branch_never_from_the_standing_branch(tmp_path):
    """Codex review of PR #61, round 9, finding 2: the job checked out the
    standing branch and then ran scripts/propose-tool.py from it, so an
    unreviewed change to the generator ran with the job's write token."""
    origin, seed, env = _first_run(tmp_path)
    human = _human_clone(tmp_path, origin, env)
    (human / "scripts" / "propose-tool.py").write_text(
        "import pathlib, json\n"
        "pathlib.Path('PWNED').write_text('ran unreviewed code')\n"
        "print(json.dumps({'proposals': {}, 'skipped': {}}))\n"
    )
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "tamper with the generator", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    _advance_main(seed, env)
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert not (ws2 / "PWNED").exists(), "the branch's generator was executed"
    head = _git(ws2, "rev-parse", "origin/tool-proposals", env=env)
    tree = _git(ws2, "ls-tree", "-r", "--name-only", head, env=env).splitlines()
    assert "PWNED" not in tree
    assert "proposals/tools/shell-semantics/README.md" in tree, "the trusted generator drafted"
    assert "shell-semantics" in run.stdout


def test_a_failed_pr_creation_is_retried_on_the_next_run(tmp_path):
    """Codex review of PR #61, round 9, finding 3: unchanged drafts exited
    before checking for a PR, so a push that succeeded while `gh pr create`
    failed left the proposals without a PR for ever."""
    origin, seed, env = _seed_repo(tmp_path)
    fail_once = tmp_path / "fail-create-once"
    fail_once.write_text("")
    ws1 = tmp_path / "ws1"
    _git(tmp_path, "clone", "-q", str(origin), str(ws1), env=env)
    run = _run_step(ws1, env, tmp_path, {"GH_FAIL_CREATE_ONCE": str(fail_once)})
    assert run.returncode != 0, "the failed creation is not reported as success"
    assert not fail_once.exists(), "gh pr create was attempted and failed"
    assert not (tmp_path / "gh.log").exists(), "no PR was created on the first run"
    _git(ws1, "fetch", "-q", "origin", "tool-proposals", env=env)
    # Nothing changed since: a rerun must still create the missing PR.
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "created" in (tmp_path / "gh.log").read_text()


def test_the_step_never_uses_a_plain_force_push():
    script = _step_script()
    assert "git push --force origin" not in script
    assert 'git push --force-with-lease="refs/heads/${branch}:${lease}"' in script


if __name__ == "__main__":
    sys.exit(0)


def test_a_branch_authored_module_never_runs_with_the_write_token(tmp_path):
    """Codex review of PR #61, round 10: `python3 -c 'import json'` ran from
    the standing branch's tree, so a branch-authored json.py executed with
    GH_TOKEN. Every Python call is now isolated and runs from the trusted
    worktree."""
    origin, seed, env = _first_run(tmp_path)
    human = _human_clone(tmp_path, origin, env)
    marker = tmp_path / "planted-ran"
    (human / "json.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('pwned')\n"
        "def load(*a, **k):\n    return {'proposals': {}}\n"
    )
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "plant a module", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    _advance_main(seed, env)
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert not marker.exists(), "the branch's json.py was imported"


def test_removing_the_last_draft_withdraws_the_open_pr(tmp_path):
    """Codex review of PR #61, round 10: cleanup stages deletions, so the
    'nothing staged' withdrawal path was skipped; the deletions were pushed
    and the empty PR reported as tracking the branch."""
    origin, seed, env = _first_run(tmp_path)
    # main now registers a tool covering shell-semantics: the draft is stale.
    _advance_main(
        seed,
        env,
        {
            "tools/manifest.json": json.dumps(
                {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
            )
        },
    )
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path, {"GH_OPEN_PR": "41"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "closed 41" in (tmp_path / "gh.log").read_text(), "the emptied PR was not withdrawn"
    _git(ws2, "fetch", "-q", "origin", env=env)
    tree = _git(ws2, "ls-tree", "-r", "--name-only", "origin/tool-proposals", env=env)
    assert "proposals/tools/shell-semantics" not in tree, "the stale draft was removed and pushed"


def test_an_empty_archive_ends_the_step_cleanly_without_a_proposals_folder(tmp_path):
    """Codex review of PR #61, round 11: with no qualifying topic the
    proposals folder never exists, and `find` on it failed the whole job
    under set -e before the no-proposals path could run."""
    origin, seed, env = _seed_repo(tmp_path)
    (seed / "docs" / "self-improvement-archive.jsonl").write_text("")
    _git(seed, "add", "-A", env=env)
    _git(seed, "commit", "-q", "-m", "empty archive", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)
    ws = tmp_path / "ws"
    _git(tmp_path, "clone", "-q", str(origin), str(ws), env=env)
    run = _run_step(ws, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "No tool proposals apply." in run.stdout
    assert (
        "created" not in (tmp_path / "gh.log").read_text()
        if (tmp_path / "gh.log").exists()
        else True
    )


def test_a_symlinked_proposals_folder_on_the_branch_is_refused(tmp_path):
    """Codex review of PR #61, round 11: `proposals/tools -> ../scripts`
    committed on the standing branch let the job write scripts/<topic>/."""
    origin, seed, env = _first_run(tmp_path)
    human = _human_clone(tmp_path, origin, env)
    shutil.rmtree(human / "proposals" / "tools")
    (human / "scripts").mkdir(exist_ok=True)
    (human / "proposals" / "tools").symlink_to(Path("..") / "scripts")
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "plant a link", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    _advance_main(seed, env)
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode != 0, "a planted output link must fail the job loudly"
    assert not (ws2 / "scripts" / "shell-semantics").exists()


def test_withdrawal_keeps_a_branch_that_holds_human_changes(tmp_path):
    """Codex review of PR #61, round 12: withdrawal counted draft folders
    only, so when the last draft became ineligible a branch with unmerged
    human notes was closed and deleted."""
    origin, seed, env = _first_run(tmp_path)
    human = _human_clone(tmp_path, origin, env)
    (human / "proposals" / "NOTES.md").write_text("human notes\n")
    _git(human, "add", "-A", env=env)
    _git(human, "commit", "-q", "-m", "notes", env=env)
    _git(human, "push", "-q", "origin", "tool-proposals", env=env)
    _advance_main(
        seed,
        env,
        {
            "tools/manifest.json": json.dumps(
                {"tools": [{"name": "x", "script": "scripts/x.py", "covers": ["shell-semantics"]}]}
            )
        },
    )
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path, {"GH_OPEN_PR": "41"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "closed" not in (tmp_path / "gh.log").read_text(), "the PR with human work was closed"
    assert "human changes" in run.stdout
    _git(ws2, "fetch", "-q", "origin", env=env)
    tree = _git(ws2, "ls-tree", "-r", "--name-only", "origin/tool-proposals", env=env)
    assert "proposals/NOTES.md" in tree and "shell-semantics" not in tree


def test_already_merged_proposals_do_not_open_an_empty_pr(tmp_path):
    """Codex review of PR #61, round 12: after a proposal merged, its
    folders sit on main; a later archive update that leaves the drafts
    unchanged reached `gh pr create` with an empty diff and failed."""
    origin, seed, env = _first_run(tmp_path)
    # Merge the proposal branch into main, as a human would.
    _git(seed, "fetch", "-q", "origin", env=env)
    _git(seed, "merge", "-q", "--no-edit", "origin/tool-proposals", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)
    (tmp_path / "gh.log").unlink(missing_ok=True)
    ws2 = tmp_path / "ws2"
    _git(tmp_path, "clone", "-q", str(origin), str(ws2), env=env)
    run = _run_step(ws2, env, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "nothing to propose" in run.stdout
    assert not (tmp_path / "gh.log").exists() or "created" not in (tmp_path / "gh.log").read_text()
