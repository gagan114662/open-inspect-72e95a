#!/usr/bin/env python3
"""Merge archive rounds onto a base archive without conflicts.

archive-and-recommend.yml keeps ONE standing branch per reviewed pull
request (`archive-rounds-pr-<N>`) instead of one branch per review: fifty
reviews of one PR in a day must not mean fifty pull requests. The standing
branch is rebuilt from the default branch on every run, so it never needs
"Update branch": base archive, then the rounds the standing branch already
holds that the base lacks, then the new round, renumbered in that order.
Identity is the reviewed commit (source_sha), so a rerun for a commit that
is already archived changes nothing.

usage: merge-archive-rounds.py BASE STANDING NEW_ROUND OUT
  BASE       the default branch's archive
  STANDING   the standing branch's archive (may be missing or empty)
  NEW_ROUND  a JSONL file holding this run's round(s) (may be empty)
  OUT        where to write the merged archive
Prints JSON: {"appended": <n>, "changed": <bool>, "rounds": [<round numbers>]}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def merge(
    base: list[dict], standing: list[dict], new_rounds: list[dict]
) -> tuple[list[dict], list[int]]:
    seen = {e.get("source_sha") for e in base if e.get("source_sha")}
    next_round = max((e["round"] for e in base if isinstance(e.get("round"), int)), default=0)
    merged = list(base)
    appended: list[int] = []
    for entry in [*standing, *new_rounds]:
        sha = entry.get("source_sha")
        if not sha or sha in seen:
            continue
        seen.add(sha)
        next_round += 1
        merged.append({**entry, "round": next_round})
        appended.append(next_round)
    return merged, appended


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    base_path, standing_path, new_path, out_path = argv[1:]
    base = load(base_path)
    merged, appended = merge(base, load(standing_path), load(new_path))
    standing_before = load(standing_path)
    changed = merged != (standing_before if standing_before else base)
    Path(out_path).write_text("".join(json.dumps(e) + "\n" for e in merged))
    print(json.dumps({"appended": len(appended), "changed": changed, "rounds": appended}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
