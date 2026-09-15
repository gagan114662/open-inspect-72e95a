"""Tests for build-evals.py and run-evals.py."""

import importlib.util
import json
import sys
from pathlib import Path


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build = _load("build_evals", "build-evals.py")
run = _load("run_evals", "run-evals.py")
policy_mod = sys.modules["improvement_policy"]
KW = policy_mod.topic_keywords(policy_mod.builtin_policy())


def _entries():
    return [
        {
            "round": 1,
            "source_sha": "aaa",
            "findings": ["[P1] leaked credential token FAKE_TOK_1 in log"],
        },
        {"round": 2, "source_sha": "bbb", "findings": ["[P2] pipefail missing; bash -e trap"]},
        {"round": 3, "source_sha": "gone", "findings": ["[P2] whatever"]},
        {"round": 4, "findings": ["[P2] no sha"]},
    ]


def _diffs(sha):
    return {"aaa": "+ print(token)\n+ token = 'FAKE_TOK_1'\n", "bbb": "+ set -uo pipefail\n"}.get(
        sha
    )


def _diffs_with_base(sha):
    diff = _diffs(sha)
    return ("base", diff) if diff is not None else None


def test_build_makes_one_case_per_reachable_round_and_scrubs_secrets():
    cases, skipped = build.build(_entries(), KW, _diffs_with_base, 60_000)
    assert [c["id"] for c in cases] == ["round-001", "round-002"] and skipped == 2
    assert cases[0]["expected_topics"] == ["credential-redaction"]
    assert "FAKE_TOK_1" not in json.dumps(cases[0])


def test_keyword_grader_measures_what_a_prepush_check_could_catch():
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)
    r1 = run.grade_keywords(cases[0], KW)
    r2 = run.grade_keywords(cases[1], KW)
    assert r1["recall"] == 1.0, "the diff mentions 'token'"
    assert r2["recall"] == 1.0, "the diff mentions 'pipefail'"
    summary = run.summarize([r1, r2])
    assert summary["cases"] == 2 and summary["mean_recall"] == 1.0


def test_codex_grader_scores_recall_from_the_reviewers_json():
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)

    def reviewer_labels_other(_prompt):
        return (
            '{"findings": [{"topic": "other", "summary": "A secret token is printed to the log."}]}'
        )

    def reviewer_babbles(_prompt):
        return "not json at all"

    result = run.grade_codex(cases[0], KW, reviewer_labels_other)
    assert result["recall"] == 1.0, (
        "an 'other' finding still counts when its summary classifies under the topic"
    )
    # Prose instead of the documented JSON is a reviewer that did not review
    # (Codex review of PR #72, round 3, finding 5), not an empty review.
    babbled = run.grade_codex(cases[1], KW, reviewer_babbles)
    assert babbled["status"] == "error" and babbled["recall"] is None


def test_main_writes_a_result_file_outside_protected_paths(tmp_path):
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for c in cases:
        (cases_dir / f"{c['id']}.json").write_text(json.dumps(c))
    out = tmp_path / "results"
    assert run.main(["r", "--cases", str(cases_dir), "--out", str(out), "--limit", "1"]) == 0
    written = list(out.glob("keywords-*.json"))
    assert len(written) == 1 and json.loads(written[0].read_text())["summary"]["cases"] == 1


# ── Codex review of PR #72 ───────────────────────────────────────────────────


def _git(repo, *args):
    import os
    import subprocess

    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "PATH": os.environ["PATH"],
        },
    ).stdout.strip()


def _repo_with_a_merged_branch(tmp_path):
    """main: A; branch: B, C (two files); merge commit M on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "A")
    a = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "first.py").write_text("token_count = len(items)\n")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "B")
    (repo / "second.py").write_text("print(token_count)\n")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "C")
    c = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "M", "feature")
    return repo, a, c


def test_case_diff_covers_the_whole_reviewed_branch_not_just_the_last_commit(tmp_path):
    """Finding 1: the review ran on `origin/main...HEAD`; a case built from
    `HEAD~1..HEAD` alone contains only the last commit's change."""
    repo, base, head = _repo_with_a_merged_branch(tmp_path)
    found_base, diff = build.reviewed_diff(head, repo=repo, main_ref="main")
    assert found_base == base
    assert "first.py" in diff and "second.py" in diff
    # A commit that sits on main's own first-parent line has no recoverable
    # reviewed base: the case is built but is not scorable.
    assert build.reviewed_diff(base, repo=repo, main_ref="main") == (None, None)


def test_a_round_without_a_recoverable_base_is_kept_but_not_scorable():
    entries = [{"round": 7, "source_sha": "onmain", "findings": ["[P2] leaked token FAKE_1"]}]
    cases, skipped = build.build(entries, KW, lambda _sha: (None, None), 60_000)
    assert skipped == 0 and cases[0]["scorable"] is False
    assert "base" in cases[0]["not_scorable_reason"]
    assert run.load_cases_from(cases) == [], "the runner skips non-scorable cases"


def _patch(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{body}\n"


def test_truncation_keeps_the_files_the_findings_reference_or_marks_the_case_unscorable():
    """Finding 2: a truncated diff must still contain every file an expected
    finding talks about, otherwise recall on it is meaningless."""
    big = _patch("noise.txt", "x" * 500)
    small = _patch("scripts/thing.py", "token = read()")
    entry = {"round": 1, "source_sha": "s", "findings": ["[P2] scripts/thing.py leaks the token"]}
    # The referenced file comes after 500 chars of noise; a naive prefix cut
    # would drop it. Referenced files are kept first, whole.
    case = build.build_case(entry, "base", big + small, KW, max_diff_chars=len(small) + 10)
    assert case["diff_truncated"] is True and case["scorable"] is True
    assert "scripts/thing.py" in case["diff"] and "noise.txt" not in case["diff"]
    assert case["omitted_files"] == ["noise.txt"]
    # When even the referenced file does not fit, the case is not scorable.
    case = build.build_case(entry, "base", big + small, KW, max_diff_chars=20)
    assert case["scorable"] is False and "scripts/thing.py" in case["not_scorable_reason"]
    # Findings that name no file give nothing to check: a truncated case is
    # then not scorable either.
    vague = {"round": 2, "source_sha": "s", "findings": ["[P2] the token leaks somewhere"]}
    case = build.build_case(vague, "base", big + small, KW, max_diff_chars=len(small) + 10)
    assert case["scorable"] is False


def test_referenced_files_keep_dot_directories_and_ignore_runner_paths():
    refs = build.referenced_files(
        [
            "In `.github/workflows/archive-and-recommend.yml` the marker is last.",
            "[scripts/distill-skills.py:52](/home/runner/work/open-inspect-72e95a/open-inspect-72e95a/scripts/distill-skills.py:52) fails.",
            "See ./docs/plan.md too.",
        ]
    )
    assert refs == [
        ".github/workflows/archive-and-recommend.yml",
        "scripts/distill-skills.py",
        "docs/plan.md",
    ]


def test_source_scrubbing_keeps_expressions_and_removes_real_secrets():
    """Finding 3: `token_count = len(items)` is code, not a credential."""
    src = "\n".join(
        [
            "token_count = len(items)",
            "auth = request.headers.get('Authorization')",
            'GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"',
            "password = 'hunter2hunter2hunter2'",
        ]
    )
    out = build.scrub_source(src)
    assert "token_count = len(items)" in out
    assert "auth = request.headers.get('Authorization')" in out
    assert "ghp_ABC" not in out and "hunter2" not in out
    assert "[REDACTED]" in out


def test_reviewer_execution_failure_is_an_error_not_zero_recall():
    """Finding 4: a reviewer that could not run (auth failure, exit 1) has
    not reviewed anything; its case must not count as recall 0."""
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)

    def reviewer_cannot_run(_prompt):
        return run.ReviewerRun(text="", returncode=1, stderr="not logged in")

    result = run.grade_codex(cases[0], KW, reviewer_cannot_run)
    assert result["status"] == "error" and result["recall"] is None
    assert "not logged in" in result["error"]
    summary = run.summarize([result, run.grade_keywords(cases[1], KW)])
    assert summary["errors"] == 1 and summary["cases"] == 1 and summary["mean_recall"] == 1.0


def test_a_finding_labelled_with_a_valid_topic_is_credited_once():
    """Finding 5: the keyword fallback exists for findings the reviewer
    labelled `other`; a finding already filed under a valid topic must not
    also be credited to a second topic its summary happens to mention."""
    case = {
        "id": "round-099",
        "round": 99,
        "expected_topics": ["fork-pr-permissions", "credential-redaction"],
        "diff": "",
        "scorable": True,
    }

    def reviewer(_prompt):
        return (
            '{"findings": [{"topic": "fork-pr-permissions", '
            '"summary": "A fork PR cannot read the token secret, so credentials are missing."}]}'
        )

    result = run.grade_codex(case, KW, reviewer)
    assert result["found_topics"] == ["fork-pr-permissions"] and result["recall"] == 0.5


def test_source_scrubbing_redacts_credential_literals_of_any_shape_and_length():
    """Codex review of PR #72, round 3, finding 2: `password = 'demo123'`,
    YAML `password: samplepass` and `--password "samplepass"` survived the
    source scrubber (8-char minimum, bare values excluded, flags dropped).
    A value whose name says credential is redacted whatever its shape or
    length; expressions and references still survive."""
    src = "\n".join(
        [
            "password = 'demo123'",
            "password: samplepass",
            'run: deploy --password "samplepass" --token=tok1 --api-key abc',
            "token_count = len(items)",
            "timeout = 30",
            "auth = request.headers.get('Authorization')",
            "token: ${{ secrets.GITHUB_TOKEN }}",
            "secret = None",
        ]
    )
    out = build.scrub_source(src)
    for leaked in ("demo123", "samplepass", "tok1", "--api-key abc"):
        assert leaked not in out, out
    for kept in (
        "token_count = len(items)",
        "timeout = 30",
        "auth = request.headers.get('Authorization')",
        "token: ${{ secrets.GITHUB_TOKEN }}",
        "secret = None",
    ):
        assert kept in out, out
    assert out.count("[REDACTED]") == 5


def test_rebuilding_removes_obsolete_generated_cases_only(tmp_path):
    """Codex review of PR #72, round 3, finding 3: a case whose round is no
    longer produced (commit unreachable, entry removed) stayed on disk and
    the runner kept scoring it."""
    out_dir = tmp_path / "cases"
    out_dir.mkdir()
    (out_dir / "round-099.json").write_text('{"id": "round-099", "stale": true}')
    (out_dir / "notes.md").write_text("human notes")
    (out_dir / "round-001.json").write_text("{}")
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)
    removed = build.write_cases(cases, out_dir, inputs=[])
    assert removed == ["round-099.json"]
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "notes.md",
        "round-001.json",
        "round-002.json",
    ]
    assert json.loads((out_dir / "round-001.json").read_text())["id"] == "round-001"


def test_a_finding_naming_a_file_absent_from_the_diff_is_not_scorable_even_uncapped():
    """Codex review of PR #72, round 3, finding 4: an uncapped diff was
    scorable without checking the files the findings name, and the capped
    path dropped absent references before validating them."""
    entry = {"round": 1, "source_sha": "s", "findings": ["[P2] missing.py leaks the token"]}
    diff = _patch("other.py", "token = read()")
    case = build.build_case(entry, "base", diff, KW, max_diff_chars=60_000)
    assert case["scorable"] is False and "missing.py" in case["not_scorable_reason"]
    # The capped path validates the same way.
    case = build.build_case(entry, "base", diff + _patch("noise.txt", "x" * 500), KW, 200)
    assert case["scorable"] is False and "missing.py" in case["not_scorable_reason"]
    # A present reference stays scorable on both paths.
    present = {"round": 2, "source_sha": "s", "findings": ["[P2] other.py leaks the token"]}
    assert build.build_case(present, "base", diff, KW, 60_000)["scorable"] is True


def test_malformed_reviewer_output_is_an_error_not_an_empty_review():
    """Codex review of PR #72, round 3, finding 5: non-JSON output became a
    completed review with recall 0, and {"findings": null} raised TypeError
    and aborted the run before results were saved."""
    cases, _ = build.build(_entries(), KW, _diffs_with_base, 60_000)
    for bad in (
        "I could not review this.",
        '{"findings": null}',
        '{"findings": [1, 2]}',
        '{"nope": []}',
    ):
        result = run.grade_codex(cases[0], KW, lambda _p, bad=bad: bad)
        assert result["status"] == "error" and result["recall"] is None, bad
        assert "reviewer output" in result["error"], bad
    ok = run.grade_codex(cases[0], KW, lambda _p: '{"findings": []}')
    assert ok["status"] == "completed" and ok["recall"] == 0.0
    summary = run.summarize([ok, run.grade_codex(cases[0], KW, lambda _p: "garbage")])
    assert summary["errors"] == 1 and summary["cases"] == 1


def test_source_scrubbing_keeps_redacting_short_credential_flags():
    """Codex review of PR #72, round 4, finding 1: replacing the flag shape
    dropped `-a VALUE`, `-p VALUE` and `-pVALUE`, so `redis-cli -a samplepass
    PING` kept the password in a serialized eval case."""
    src = "\n".join(
        [
            "redis-cli -a samplepass PING",
            "mysql -u root -pS3cretPw mydb",
            'mysql -p "quoted pass" mydb',
            "ls -la /tmp",
            "kubectl get pods -A",
            "curl -a 8080",
        ]
    )
    out = build.scrub_source(src)
    assert "samplepass" not in out and "S3cretPw" not in out and "quoted pass" not in out
    assert "redis-cli -a [REDACTED] PING" in out
    assert "-u root -p[REDACTED] mydb" in out
    assert "ls -la /tmp" in out, "an -l/-a combination is not a credential flag"
    assert "kubectl get pods -A" in out, "flags followed by nothing are untouched"
