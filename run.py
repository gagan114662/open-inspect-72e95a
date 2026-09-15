#!/usr/bin/env python3
"""Run the self-improvement agent once, locally.

    python3 run.py            measure, decide (dry run), render, distill, propose
    python3 run.py --refresh  first mine this machine's working sessions into the field anchor
    python3 run.py --apply    write the decision to the policy files instead of a dry run

Each step is one of the agent's registered tools (tools/manifest.json); this
file only calls them in the order the hourly schedule uses, and prints one
line per step. Nothing here merges, deploys, or touches a secret. The
schedules in .github/workflows/ run the same steps on their own.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = "docs/self-improvement-archive.jsonl"
EVIDENCE = "docs/rsi/trace-evidence.json"
MEASUREMENT = "docs/rsi/measurement.json"
DASHBOARD = "docs/rsi/dashboard.html"


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
        "--repo-dir", default=str(ROOT), help="folder whose sessions to mine (with --refresh)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the decision instead of a dry run"
    )
    args = parser.parse_args(argv[1:])

    if args.refresh:
        if step(
            "mine field failures",
            [
                "scripts/mine-trace-failures.py",
                "--repo-dir",
                args.repo_dir,
                "--save-evidence",
                EVIDENCE,
            ],
        ):
            return 1
    evidence = ["--trace-evidence", EVIDENCE] if (ROOT / EVIDENCE).exists() else []
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
    decide = ["scripts/revise-improvement-policy.py", ARCHIVE, "--measurement", MEASUREMENT]
    if not args.apply:
        decide.append("--dry-run")
    if step("decide" + ("" if args.apply else " (dry run)"), decide):
        return 1
    if step(
        "render dashboard",
        ["scripts/render-rsi-dashboard.py", ARCHIVE, *evidence, "--out", DASHBOARD],
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
