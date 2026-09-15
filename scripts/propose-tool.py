#!/usr/bin/env python3
"""Draft a tool for a recurring finding class, as a proposal a human merges.

The agent cannot create tools on the fly: scripts/ and .github/ are fixed
infrastructure its write guard refuses, so a new capability always arrives
as a pull request reviewed by Codex and merged by a human. This script is
how the agent asks. For every topic the detector recommends a
mechanism-level fix for, and that no registered tool covers, it drafts:

    proposals/tools/<topic>/check-<topic>.py        a pre-push check
    proposals/tools/<topic>/check_<topic>_test.py   its tests
    proposals/tools/<topic>/manifest-entry.json     the registry entry
    proposals/tools/<topic>/README.md               what it is and how to promote it

The draft is deterministic: the check scans a diff or any text for the
topic's policy keywords and reports each hit alongside the archived review
findings that established the class (public review text, scrubbed). It is
a starting point with tests, not a finished mechanism; promoting it means
moving the files under scripts/ and tools/manifest.json in a follow-up
commit, which is a human decision.

usage: propose-tool.py [ARCHIVE] [--policy PATH] [--manifest PATH]
                       [--out-dir DIR] [--topic NAME ...] [--threshold N]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "proposals" / "tools"
DEFAULT_MANIFEST = REPO_ROOT / "tools" / "manifest.json"
MAX_EXAMPLES = 6
GENERATED_MARKER = "Drafted automatically by `scripts/propose-tool.py`"
SAFE_TOPIC_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _load_sibling_module(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy_mod = _load_sibling_module("improvement_policy", "improvement_policy.py")
detect_mod = _load_sibling_module("detect_recurring_pattern", "detect-recurring-pattern.py")


def covered_topics(manifest: dict) -> set[str]:
    return {t for tool in manifest.get("tools", []) for t in tool.get("covers", [])}


def topics_needing_a_tool(
    entries: list[dict], policy: dict, manifest: dict, threshold: int | None = None
) -> list[dict]:
    """Mechanism-level recommendations whose topic no registered tool covers."""
    keywords = policy_mod.topic_keywords(policy)
    weights = policy_mod.topic_weights(policy)
    result = detect_mod.analyze(
        entries, threshold if threshold is not None else policy["threshold"], keywords, weights
    )
    covered = covered_topics(manifest)
    return [
        rec
        for rec in result["recommendations"]
        if rec["recommended_action"] == "mechanism" and rec["topic"] not in covered
    ]


def examples_for(topic: str, entries: list[dict], keywords: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for entry in entries:
        for finding in entry.get("findings", []) or []:
            if not isinstance(finding, str):
                continue
            if policy_mod.classify_finding(finding, keywords) == topic:
                text = policy_mod.scrub_secrets(finding).strip()[:240]
                if text not in out:
                    out.append(text)
            if len(out) >= MAX_EXAMPLES:
                return out
    return out


def check_script(topic: str, words: list[str], examples: list[str]) -> str:
    return f'''#!/usr/bin/env python3
"""Pre-push check for the `{topic}` finding class.

Drafted by scripts/propose-tool.py from the review archive: this class of
finding recurred often enough for the loop to recommend a mechanism-level
fix. The check reads a diff (or any text) and reports every line that
touches the class's keywords, next to what reviewers found before, so the
author sees the pattern before a reviewer does.

usage: check-{topic}.py [FILE]   (reads stdin when FILE is omitted)
       --strict                   exit 1 when anything is reported
"""

from __future__ import annotations

import argparse
import sys

KEYWORDS = {words!r}
PRIOR_FINDINGS = {examples!r}


def scan(text: str) -> list[tuple[int, str, str]]:
    """(line number, keyword, line) for every line that mentions a keyword.
    In a unified diff only ADDED lines count: context and removed lines are
    not the author's change."""
    is_diff = any(line.startswith(("+++ ", "@@ ")) for line in text.splitlines())
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if is_diff and not (line.startswith("+") and not line.startswith("+++")):
            continue
        lowered = line.lower()
        for word in KEYWORDS:
            if word in lowered:
                hits.append((number, word, line.strip()))
                break
    return hits


def report(hits: list[tuple[int, str, str]]) -> str:
    if not hits:
        return "check-{topic}: nothing touches this class."
    lines = [f"check-{topic}: {{len(hits)}} line(s) touch `{topic}`:"]
    for number, word, line in hits[:50]:
        lines.append(f"  L{{number}} [{{word}}] {{line[:120]}}")
    lines.append("Reviewers found before:")
    lines.extend(f"  - {{f}}" for f in PRIOR_FINDINGS)
    return "\\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv[1:])
    text = open(args.path).read() if args.path else sys.stdin.read()
    hits = scan(text)
    print(report(hits))
    return 1 if (hits and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


def test_script(topic: str, words: list[str]) -> str:
    slug = topic.replace("-", "_")
    word = words[0]
    return f'''"""Tests for the drafted check-{topic}.py."""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "check-{topic}.py"
_spec = importlib.util.spec_from_file_location("check_{slug}", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
sys.modules["check_{slug}"] = check
_spec.loader.exec_module(check)


WORD = {word!r}


def test_reports_added_lines_that_touch_the_class_and_skips_removed_ones():
    diff = "+++ b/x\\n@@ -1 +1 @@\\n+ first " + WORD + " here\\n- removed " + WORD + "\\n+ unrelated line\\n"
    hits = check.scan(diff)
    assert [(n, w) for n, w, _ in hits] == [(3, WORD)]  # line 3: after the two header lines
    assert "1 line(s)" in check.report(hits)


def test_clean_text_reports_nothing_and_strict_mode_fails_on_hits(tmp_path, capsys):
    clean = tmp_path / "clean.diff"
    clean.write_text("+ nothing relevant\\n")
    assert check.main(["c", str(clean)]) == 0
    assert "nothing touches" in capsys.readouterr().out
    dirty = tmp_path / "dirty.diff"
    dirty.write_text("+ " + WORD + " again\\n")
    assert check.main(["c", str(dirty), "--strict"]) == 1
'''


def readme(topic: str, rec: dict, words: list[str]) -> str:
    return f"""# Proposed tool: check-{topic}

Drafted automatically by `scripts/propose-tool.py` because `{topic}` crossed the
mechanism-level-fix threshold: it recurred in {rec["recurrence_count"]} round(s)
({", ".join(str(r) for r in rec["rounds"])}) and no registered tool covers it.

What it does: scans a diff or any text for the class's keywords
({", ".join(f"`{w}`" for w in words)}) and reports each hit next to what reviewers
found before, so the pattern is seen before a reviewer sees it. Deterministic,
tested, and a starting point: the real mechanism may need more than keywords.

## To promote (a human decision)

```bash
git mv proposals/tools/{topic}/check-{topic}.py scripts/check-{topic}.py
git mv proposals/tools/{topic}/check_{topic.replace("-", "_")}_test.py scripts/check_{topic.replace("-", "_")}_test.py
# then add manifest-entry.json's object to tools/manifest.json and delete this folder
```

The agent cannot do that step: `scripts/` and `tools/` are fixed infrastructure its
write guard refuses. That is the point.
"""


def manifest_entry(topic: str, words: list[str]) -> dict:
    return {
        "name": f"check_{topic.replace('-', '_')}",
        "script": f"scripts/check-{topic}.py",
        "purpose": f"Pre-push check for the `{topic}` finding class: reports lines touching its keywords next to prior review findings.",
        "inputs": ["diff or text file (stdin when omitted)", "--strict"],
        "writes": [],
        "covers": [topic],
        "keywords": list(words),
    }


def draft(topic: str, rec: dict, entries: list[dict], policy: dict, out_dir: Path) -> list[str]:
    if not SAFE_TOPIC_NAME.fullmatch(topic):
        raise ValueError(f"topic name {topic!r} is not a safe filename component")
    keywords = policy_mod.topic_keywords(policy)
    words = [w.lower() for w in keywords[topic]]
    folder = (out_dir / topic).resolve()
    if folder.parent != out_dir.resolve():
        raise ValueError(f"{topic!r} would write outside {out_dir}")
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        folder / f"check-{topic}.py": check_script(
            topic, words, examples_for(topic, entries, keywords)
        ),
        folder / f"check_{topic.replace('-', '_')}_test.py": test_script(topic, words),
        folder / "manifest-entry.json": json.dumps(manifest_entry(topic, words), indent=2) + "\n",
        folder / "README.md": readme(topic, rec, words),
    }
    written = []
    for path, content in files.items():
        policy_mod.assert_safe_output(path)
        path.write_text(content)
        written.append(policy_mod.relative_to_repo(path))
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "archive_path",
        nargs="?",
        default=str(REPO_ROOT / "docs" / "self-improvement-archive.jsonl"),
    )
    parser.add_argument("--policy", default=None)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--topic", action="append", default=None, help="only these topics")
    parser.add_argument("--threshold", type=int, default=None)
    args = parser.parse_args(argv[1:])

    policy = (
        policy_mod.load_policy(args.policy) if args.policy else policy_mod.load_policy_or_builtin()
    )
    entries = [
        json.loads(line)
        for line in Path(args.archive_path).read_text().splitlines()
        if line.strip()
    ]
    manifest = (
        json.loads(Path(args.manifest).read_text())
        if Path(args.manifest).exists()
        else {"tools": []}
    )
    needing = topics_needing_a_tool(entries, policy, manifest, args.threshold)
    if args.topic:
        needing = [r for r in needing if r["topic"] in set(args.topic)]
    out_dir = Path(args.out_dir)
    proposals = {
        rec["topic"]: draft(rec["topic"], rec, entries, policy, out_dir) for rec in needing
    }
    # Drafts for topics that are no longer eligible (covered since, or below
    # threshold) are removed; only folders this script generated (they carry
    # its README marker) are touched (Codex review of PR #61, round 2).
    if out_dir.exists():
        for folder in out_dir.iterdir():
            marker = folder / "README.md"
            if (
                folder.is_dir()
                and folder.name not in proposals
                and marker.exists()
                and GENERATED_MARKER in marker.read_text()
            ):
                for child in folder.iterdir():
                    child.unlink()
                folder.rmdir()
    print(json.dumps({"policy_version": policy["version"], "proposals": proposals}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
