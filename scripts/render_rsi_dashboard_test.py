"""Tests for render-rsi-dashboard.py.

Run with: python3 -m pytest scripts/render_rsi_dashboard_test.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "render-rsi-dashboard.py"
_spec = importlib.util.spec_from_file_location("render_rsi_dashboard", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
render = importlib.util.module_from_spec(_spec)
sys.modules["render_rsi_dashboard"] = render
_spec.loader.exec_module(render)
policy_mod = sys.modules["improvement_policy"]


def _archive():
    return [
        {
            "round": 1,
            "occurred_at": "2026-09-14T15:00:00Z",
            "kept": True,
            "target": "x.yml",
            "findings": ["[P1] Secret leaked into logs.", "[P2] Archive concurrency drops rounds."],
        },
        {
            "round": 2,
            "occurred_at": "2026-09-14T16:00:00Z",
            "kept": False,
            "target": "x.yml",
            "findings": ["[P2] Archive PR creation cannot recover."],
        },
    ]


def test_renders_every_section_from_real_shapes(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    v1 = policy_mod.builtin_policy()
    v2 = policy_mod.new_version(
        v1,
        topics={
            **v1["topics"],
            "archive-ops": {
                "keywords": ["archive"],
                "weight": 1.0,
                "mined_from": [{"round": 1, "finding": "..."}],
            },
        },
        threshold=3,
        origin="revision",
        rationale="coverage repair",
        created_at="2026-09-14T17:00:00Z",
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(v2))
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "version": 2,
                "parent": 1,
                "origin": "revision",
                "created_at": "2026-09-14T17:00:00Z",
                "changes": ["added topic archive-ops"],
                "coverage_before": 0.33,
                "coverage_after": 1.0,
                "policy": v2,
            }
        )
        + "\n"
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "source": "traces",
                "agents": ["claude-code"],
                "topics": {"archive-ops": [{"id": "t1", "agentId": "claude-code", "timestamp": 1}]},
            }
        )
    )
    out = tmp_path / "dashboard.html"
    assert (
        render.main(
            [
                "r",
                str(archive),
                "--policy",
                str(policy_path),
                "--history",
                str(history),
                "--trace-evidence",
                str(evidence),
                "--out",
                str(out),
                "--head",
                "abc1234",
            ]
        )
        == 0
    )
    page = out.read_text()
    for needle in (
        "Level 5: recursive meta-improvement",
        "Autonomy matrix",
        "The closed improvement loop",
        "The L5 trigger",
        "Policy lineage",
        "Safe inheritance",
        "Autonomy attribution",
        "Reliable verification",
        "archive-ops",
        "blind spot",
        "abc1234",
        "<svg",
    ):
        assert needle in page, needle
    assert "<script" not in page
    assert "http" not in page.split("<footer>")[0].replace("http://www.w3.org", "")
