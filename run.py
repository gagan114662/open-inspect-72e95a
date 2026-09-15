#!/usr/bin/env python3
"""Run the self-improvement agent once, locally.

    python3 run.py            measure, decide (dry run), render, distill, propose
    python3 run.py --refresh  first mine this machine's working sessions into the field anchor
    python3 run.py --apply    write the decision to the policy files instead of a dry run
    python3 run.py --root DIR run against another checkout (tests use a copy)

Each step is one of the agent's registered tools (tools/manifest.json); this
file only calls them in the order the hourly schedule uses, and prints one
line per step. Nothing here merges, deploys, or touches a secret. The
schedules in .github/workflows/ run the same steps on their own.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = "docs/self-improvement-archive.jsonl"
EVIDENCE = "docs/rsi/trace-evidence.json"
VERIFIER_EVIDENCE = "docs/rsi/trace-evidence-verifier.json"
MEASUREMENT = "docs/rsi/measurement.json"
DASHBOARD = "docs/rsi/dashboard.html"


def replace_evidence(fresh: Path, dest: Path) -> str | None:
    """Replace the committed evidence snapshot with the freshly mined one, under
    the same output guard every tool uses: the destination may not be (or link
    to) the archive, the policy, its history, or the loop's code, and it must
    be a regular file so nothing is followed (Codex review of PR #68, round 3).
    Returns the refusal reason, or None when the snapshot was replaced."""
    guard = ROOT / "scripts" / "improvement_policy.py"
    if not guard.exists():
        return f"{guard.relative_to(ROOT)} is missing; refusing to replace evidence unguarded"
    spec = importlib.util.spec_from_file_location("run_entry_policy", guard)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    if dest.is_symlink():
        return f"{dest.relative_to(ROOT)} is a symlink; evidence must be a regular file"
    try:
        policy.assert_safe_output(dest, inputs=[fresh], kind="evidence")
    except PermissionError as exc:
        return str(exc)
    if dest.exists() and dest.stat().st_nlink > 1:
        return f"{dest.relative_to(ROOT)} has other hard links; evidence must be a regular file"
    # The staging file is created exclusively under a unique name (O_EXCL),
    # never at a predictable path: a `trace-evidence.json.tmp` planted as a
    # symlink or hard link to the archive would otherwise be opened for
    # writing before the atomic replace (Codex review of PR #68, round 5).
    fd, staged_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".staging", dir=dest.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(fresh.read_bytes())
        staged.replace(dest)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return None


def step(name: str, argv: list[str]) -> int:
    """Run one registered tool from the repository root; print its name and
    its last human-readable line; return its exit code. A tool that is not
    installed yet (still a proposal, or not merged) is skipped, not failed."""
    if not (ROOT / argv[0]).exists():
        print(f"[skip] {name}: {argv[0]} is not installed")
        return 0
    result = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True)
    prose = [
        ln
        for ln in (result.stdout or "").splitlines()
        if ln.strip() and not ln.startswith(("{", "}", '"', " ", "---", "[", "]"))
    ]
    if prose:
        summary = prose[-1]
    else:
        err = (result.stderr or "").strip().splitlines()
        summary = err[-1] if err else "(no output)"
    status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
    print(f"[{status}] {name}: {summary[:160]}")
    if result.returncode != 0 and result.stderr:
        print(result.stderr.strip()[-600:], file=sys.stderr)
    return result.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--refresh", action="store_true", help="mine this machine's sessions first")
    parser.add_argument(
        "--repo-dir", default=None, help="folder whose sessions to mine (with --refresh)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the decision instead of a dry run"
    )
    parser.add_argument(
        "--root", default=None, help="checkout to run in (default: this file's folder)"
    )
    args = parser.parse_args(argv[1:])
    if args.root:
        # Tests and operators can point one pass at a copy of the checkout so
        # a run never rewrites the real one (Codex review of PR #68, round 3).
        global ROOT
        ROOT = Path(args.root).expanduser().resolve()
    # Subprocesses run from the repository root; a relative --repo-dir means
    # relative to where the user typed it (Codex review of PR #68, round 2).
    args.repo_dir = str(Path(args.repo_dir or ROOT).expanduser().resolve())

    field_failures: list[str] = []
    scratch: Path | None = None
    if args.refresh:
        # Mine into scratch files first: a refresh that observed no session
        # must never replace the committed snapshot, and the mined blind
        # spots feed the decision (Codex review of PR #68).
        scratch_root = ROOT / "docs" / "rsi" / ".refresh"
        if scratch_root.is_symlink():
            print(f"[exit 1] refresh: {scratch_root.relative_to(ROOT)} is a symlink; refusing")
            return 1
        scratch_root.mkdir(parents=True, exist_ok=True)
        # A fresh, uniquely named scratch folder per run: a planted
        # `.refresh/trace-evidence.json` link to the committed snapshot would
        # otherwise redirect the miner's write onto the protected file before
        # replace_evidence() ever ran (Codex review of PR #68, round 6).
        tmp = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
        fresh, failures = tmp / "trace-evidence.json", tmp / "field-failures.json"
        mine = [
            "scripts/mine-trace-failures.py",
            "--repo-dir",
            args.repo_dir,
            "--save-evidence",
            str(fresh),
            "--out-json",
            str(failures),
        ]
        if step("mine field failures", mine):
            return 1
        observed = len(json.loads(fresh.read_text()).get("sessions") or [])
        if observed:
            refused = replace_evidence(fresh, ROOT / EVIDENCE)
            if refused:
                print(f"[exit 1] refresh: {refused}")
                return 1
            field_failures = ["--field-failures", str(failures)]
            print(f"[ok] refresh: {observed} session(s) observed; evidence snapshot replaced")
        else:
            print("[skip] refresh: no session observed; the committed evidence snapshot is kept")
        scratch = tmp
    try:
        return run_decision(args, field_failures)
    finally:
        # The scratch folder lives until the decision has used the mined
        # blind spots, then goes, on success and on failure alike (Codex
        # review of PR #68, round 7).
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def run_decision(args: argparse.Namespace, field_failures: list[str]) -> int:
    """Measure, decide, render, distill and propose on the checkout in ROOT."""
    evidence = ["--trace-evidence", EVIDENCE] if (ROOT / EVIDENCE).exists() else []
    verifier = (
        ["--verifier-evidence", VERIFIER_EVIDENCE] if (ROOT / VERIFIER_EVIDENCE).exists() else []
    )
    measure_out = subprocess.run(
        [
            sys.executable,
            "scripts/measure-policy-validity.py",
            ARCHIVE,
            *evidence,
            "--out-json",
            MEASUREMENT,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    first = next(
        (ln for ln in measure_out.stdout.splitlines() if ln.startswith("policy v")), "measured"
    )
    print(
        f"[{'ok' if measure_out.returncode == 0 else 'exit ' + str(measure_out.returncode)}] measure: {first}"
    )
    if measure_out.returncode:
        print(measure_out.stderr.strip()[-600:], file=sys.stderr)
        return 1
    decide = [
        "scripts/revise-improvement-policy.py",
        ARCHIVE,
        "--measurement",
        MEASUREMENT,
        *field_failures,
    ]
    if not args.apply:
        decide.append("--dry-run")
    if step("decide" + ("" if args.apply else " (dry run)"), decide):
        return 1
    if args.apply and step(
        "re-measure under the applied policy",
        ["scripts/measure-policy-validity.py", ARCHIVE, *evidence, "--out-json", MEASUREMENT],
    ):
        return 1  # the live measurement must describe the policy now in force
    if step(
        "render dashboard",
        ["scripts/render-rsi-dashboard.py", ARCHIVE, *evidence, *verifier, "--out", DASHBOARD],
    ):
        return 1
    if step("distill skills", ["scripts/distill-skills.py", ARCHIVE]):
        return 1
    if step("propose tools", ["scripts/propose-tool.py", ARCHIVE]):
        return 1
    print(f"done. dashboard: {DASHBOARD}  skills: skills/  proposals: proposals/tools/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
