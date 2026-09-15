"""Tests for cap-review-diff.py: the review prompt is cut at file boundaries."""

import importlib.util
import subprocess
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "cap-review-diff.py"
_spec = importlib.util.spec_from_file_location("cap_review_diff", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
capmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capmod)


def _file(path: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}\n"


def test_whole_files_are_kept_in_order_until_the_cap():
    diff = _file("a.py", "+" + "a" * 40) + _file("b.py", "+" + "b" * 40) + _file("c.py", "+c")
    body, omitted = capmod.cap(diff, len(_file("a.py", "+" + "a" * 40)) + 10)
    assert body == _file("a.py", "+" + "a" * 40)
    assert omitted == ["b.py", "c.py"], "once one file is omitted, later ones are too"
    body, omitted = capmod.cap(diff, 10_000)
    assert body == diff and omitted == []


def test_a_file_larger_than_the_cap_is_omitted_whole_not_cut():
    big = _file("evals/cases/round-001.json", "+" + "x" * 500)
    body, omitted = capmod.cap(big + _file("small.py", "+ok"), 300)
    assert body == "" and omitted == ["evals/cases/round-001.json", "small.py"]


def test_cli_appends_the_omitted_list(tmp_path):
    diff = _file("a.py", "+a") + _file("b.py", "+" + "b" * 200)
    path = tmp_path / "diff.txt"
    path.write_text(diff)
    run = subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(path), "--max-chars", "80"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout.startswith(_file("a.py", "+a"))
    assert "DIFF TRUNCATED" in run.stdout and "  - b.py" in run.stdout
    run = subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(path), "--max-chars", "8000"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == diff
