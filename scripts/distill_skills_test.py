"""Tests for distill-skills.py and the agent directory's tool registry."""

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "distill-skills.py"
_spec = importlib.util.spec_from_file_location("distill_skills", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
distill = importlib.util.module_from_spec(_spec)
sys.modules["distill_skills"] = distill
_spec.loader.exec_module(distill)
policy_mod = sys.modules["improvement_policy"]

REPO = Path(__file__).resolve().parent.parent


def _archive():
    return [
        {
            "round": 1,
            "source_sha": "a",
            "findings": ["[P1] leaked credential in output"],
            "kept": True,
            "mechanism_change_note": "Extracted scripts/redact-secrets.py and ran it on every posted comment.",
        },
        {
            "round": 2,
            "source_sha": "b",
            "findings": ["[P2] token exposed again; also shell -e trap"],
            "kept": False,
        },
        {
            "round": 2,
            "source_sha": "b",
            "findings": ["[P2] token exposed again; also shell -e trap"],
        },  # duplicate line
        {"round": 3, "source_sha": "c", "findings": ["[P2] unrelated wording"]},
    ]


def test_distill_groups_findings_by_topic_and_counts_rounds_by_identity():
    skills = distill.distill(_archive(), policy_mod.builtin_policy())
    cred = skills["credential-redaction"]
    assert cred["rounds"] == [1, 2], "the duplicated round-2 line counts once"
    assert [e["round"] for e in cred["examples"]] == [1, 2]
    assert cred["fixes"] == [
        {
            "round": 1,
            "how": "Extracted scripts/redact-secrets.py and ran it on every posted comment.",
        }
    ]
    assert skills["shell-semantics"]["rounds"] == [] or skills["shell-semantics"]["rounds"] == [2]


def test_rendered_skill_never_contains_a_secret():
    entries = [
        {
            "round": 9,
            "source_sha": "z",
            "findings": ["[P1] token exposed: Authorization: Bearer FAKE_SKILL_TOKEN_1234 leaked"],
        }
    ]
    skills = distill.distill(entries, policy_mod.builtin_policy())
    text = distill.render("credential-redaction", skills["credential-redaction"], 3)
    assert "FAKE_SKILL_TOKEN_1234" not in text and "[REDACTED]" in text
    assert text.startswith("# Skill: credential-redaction")


def test_main_writes_one_skill_per_topic_and_removes_stale_ones(tmp_path):
    archive = tmp_path / "archive.jsonl"
    archive.write_text("\n".join(json.dumps(e) for e in _archive()) + "\n")
    out = tmp_path / "skills"
    out.mkdir()
    (out / "retired-topic.md").write_text("old")
    (out / "README.md").write_text("keep")
    assert distill.main(["d", str(archive), "--out-dir", str(out)]) == 0
    names = sorted(p.name for p in out.glob("*.md"))
    assert "retired-topic.md" not in names and "README.md" in names
    assert set(names) >= {f"{t}.md" for t in policy_mod.BUILTIN_TOPIC_KEYWORDS}


def test_tool_registry_matches_the_scripts_directory():
    manifest = json.loads(
        (REPO / "agents" / "self-improver" / "tools" / "manifest.json").read_text()
    )
    registered = {t["script"] for t in manifest["tools"]}
    for script in registered:
        assert (REPO / script).exists(), f"{script} is registered but missing"
    loop_scripts = {
        f"scripts/{p.name}"
        for p in (REPO / "scripts").glob("*.py")
        if not p.name.endswith("_test.py")
        and p.name
        in {
            "archive-round.py",
            "merge-archive-rounds.py",
            "detect-recurring-pattern.py",
            "mine-trace-failures.py",
            "measure-policy-validity.py",
            "revise-improvement-policy.py",
            "render-rsi-dashboard.py",
            "distill-skills.py",
            "improvement_policy.py",
        }
    }
    assert loop_scripts <= registered, f"unregistered loop scripts: {loop_scripts - registered}"
    agent = json.loads((REPO / "agents" / "self-improver" / "agent.json").read_text())
    for rel in agent["schedules"] + agent["channels"]:
        assert (REPO / "agents" / "self-improver" / rel).exists(), rel
    policy = policy_mod.load_policy()
    assert agent["policy"]["version"] == policy["version"]
    assert agent["policy"]["topics"] == list(policy["topics"])
