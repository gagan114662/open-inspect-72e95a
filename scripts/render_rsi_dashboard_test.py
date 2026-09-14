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
                "definitions": {"archive-ops": ["archive"]},
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
    # Field-trace column shows validated counts; the archive-ops topic was searched (1 trace),
    # credential-redaction was not searched in this evidence file, so it reads n/a.
    assert "<td>1</td></tr>" in page and "<td>n/a</td></tr>" in page
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


def test_revision_markers_sit_at_the_epoch_they_were_created_after():
    epochs = [
        {"round": 1, "timestamp_ms": 1000},
        {"round": 2, "timestamp_ms": 2000},
        {"round": 3, "timestamp_ms": 3000},
    ]
    assert render.marker_epoch_index(epochs, "1970-01-01T00:00:02.500Z") == 1
    assert render.marker_epoch_index(epochs, "1970-01-01T00:00:00.500Z") == 0
    assert render.marker_epoch_index(epochs, "1970-01-01T00:00:09Z") == 2
    assert render.marker_epoch_index(epochs, None) == 2
    assert render.marker_epoch_index([], "1970-01-01T00:00:09Z") == 0


def test_evidence_strings_are_escaped_in_the_echo_note(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps(_archive()[0]) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    hostile = {
        "source": "<script>alert(1)</script>",
        "agents": ["<img src=x onerror=alert(1)>"],
        "topics": {"credential-redaction": [{"id": "t", "agentId": "x", "timestamp": 1}]},
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(hostile))
    out = tmp_path / "d.html"
    assert (
        render.main(
            [
                "r",
                str(archive),
                "--policy",
                str(policy_path),
                "--history",
                str(tmp_path / "h.jsonl"),
                "--trace-evidence",
                str(evidence),
                "--verifier-evidence",
                str(evidence),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    page = out.read_text()
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_trigger_chart_plots_current_policy_validity():
    before = {"policy_version": 1, "epochs": [{"round": 1, "coverage": 0.5, "validity": 0.9}]}
    after = {"policy_version": 2, "epochs": [{"round": 1, "coverage": 0.8, "validity": 0.1}]}
    svg = render.trigger_chart(before, after, [], 0.8)
    # y(v) = pad_t + (h - pad_t - pad_b) * (1 - v): 0.1 -> 236, 0.9 -> 44 (minus the 3px offset)
    assert 'y="233.0" width="6" height="6"' in svg
    assert 'y="41.0" width="6" height="6"' not in svg
    assert "v2 validity" in svg


def test_head_label_is_escaped_in_the_loop_diagram():
    svg = render.loop_diagram(
        {
            "rounds": 1,
            "findings": 1,
            "kept": 1,
            "target": "<b>t</b>",
            "head": "<img src=x onerror=alert(1)>",
            "policy_version": 1,
            "policy_hash": "abc",
        }
    )
    assert "<img src=x" not in svg and "<b>t</b>" not in svg
    assert "&lt;img src=x onerror=alert(1)&gt;" in svg


def test_out_may_not_overwrite_protected_or_input_files(tmp_path):
    import pytest

    archive = tmp_path / "archive.jsonl"
    archive.write_text(json.dumps(_archive()[0]) + "\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_mod.builtin_policy()))
    for bad in (
        policy_mod.REPO_ROOT / "docs" / "self-improvement-archive.jsonl",
        archive,
        policy_path,
    ):
        with pytest.raises(PermissionError):
            render.main(
                [
                    "r",
                    str(archive),
                    "--policy",
                    str(policy_path),
                    "--history",
                    str(tmp_path / "h.jsonl"),
                    "--out",
                    str(bad),
                ]
            )


def test_history_version_labels_are_escaped_in_the_chart():
    before = {
        "policy_version": 1,
        "epochs": [{"round": 1, "coverage": 0.5, "validity": None, "timestamp_ms": 1}],
    }
    after = {
        "policy_version": 2,
        "epochs": [{"round": 1, "coverage": 0.8, "validity": None, "timestamp_ms": 1}],
    }
    hostile = [
        {
            "version": "</text><script>alert(1)</script><text>",
            "origin": "revision",
            "created_at": None,
        }
    ]
    svg = render.trigger_chart(before, after, hostile, 0.8)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_reproduce_commands_only_use_flags_their_scripts_define():
    # The footer once told readers to run measure-policy-validity.py with
    # --repo-dir/--save-evidence, which it does not accept (Codex, round 35).
    scripts_dir = Path(__file__).parent
    for command in render.REPRODUCE_COMMANDS:
        tokens = command.split()
        assert tokens[:2] == ["python3", tokens[1]] and tokens[1].startswith("scripts/")
        source = (scripts_dir / Path(tokens[1]).name).read_text()
        for flag in (t for t in tokens if t.startswith("--")):
            assert f'"{flag}"' in source, f"{tokens[1]} does not define {flag}"
    footer = render.render(_archive(), policy_mod.builtin_policy(), [], None, None, "abc123")
    assert "mine-trace-failures.py" in footer
    assert "--repo-dir" not in footer.split("measure-policy-validity.py", 1)[1].split("</code>")[0]
