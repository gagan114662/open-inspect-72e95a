#!/usr/bin/env python3
"""Render the recursive self-improvement dashboard: one self-contained HTML
page that shows, from real repository data, whether the L5 loop is doing
what docs/plans/recursive-meta-improvement.md says it must.

Inputs are the artifacts the loop already produces -- the review archive,
the versioned improvement policy and its history, and the Traces evidence
file measure-policy-validity.py saves -- so the page is a rendering of
state, not a story about it. No external assets: inline CSS and SVG only,
so it opens from a file:// URL, a PR artifact, or a static host identically.

Usage:
    python3 render-rsi-dashboard.py <archive.jsonl> [--policy PATH] [--history PATH]
        [--trace-evidence PATH] [--verifier-evidence PATH] [--out PATH]

--verifier-evidence is an optional second evidence file collected with
`--anchor-agents all` (the verifier's own review sessions included); the
page shows its validity next to the proper anchor's to make the echo effect
visible rather than argued.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import sys
from pathlib import Path


def _load_sibling_module(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy_mod = _load_sibling_module("improvement_policy", "improvement_policy.py")
measure_mod = _load_sibling_module("measure_policy_validity", "measure-policy-validity.py")
revise_mod = _load_sibling_module("revise_improvement_policy", "revise-improvement-policy.py")

NAVY = "#0b2a5b"
ORANGE = "#f28c28"
GREEN = "#2e8b57"
RED = "#c0392b"
GREY = "#8a94a6"

LEVELS = [
    (
        1,
        "Execution",
        "objective, strategy, validation",
        "execution",
        "task outcome",
        "Claude Code applies each round's fix (archive `proposal`/`fixes_applied`)",
    ),
    (
        2,
        "Strategy",
        "objective, task bounds, validation",
        "search rules",
        "search strategy",
        "rounds choose what to try next from the previous round's findings",
    ),
    (
        3,
        "Experience",
        "environment parameters, validation",
        "data generation",
        "practice curriculum",
        "analyze-traces.py / sync-pr-traces.py pull the loop's own session evidence",
    ),
    (
        4,
        "Deployment",
        "governance rules, rollbacks",
        "state management",
        "deployed state",
        "archive-round.py persists rounds; archive-and-recommend.yml acts on thresholds",
    ),
    (
        5,
        "Meta-improvement",
        "final oversight",
        "the improver mechanism",
        "the verifier/improver",
        "revise-improvement-policy.py rewrites improvement-policy.json from measured validity",
    ),
]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def fmt(value: object) -> str:
    """Format a number for display. Anything that is not a number renders
    as n/a, and the result is HTML-escaped, so a hostile history file
    cannot smuggle markup through a coverage field (Codex review of PR #10,
    round 25)."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return esc(value)


# --- data ------------------------------------------------------------------


def initial_policy(history: list[dict]) -> dict:
    return revise_mod.snapshot_for_version(1, history) or policy_mod.builtin_policy()


def lineage(policy: dict, history: list[dict]) -> list[dict]:
    versions = [
        {
            "version": 1,
            "parent": None,
            "origin": "init",
            "created_at": initial_policy(history).get("created_at"),
            "changes": ["taxonomy and threshold transcribed from detect-recurring-pattern.py"],
            "coverage_before": None,
            "coverage_after": None,
        }
    ]
    versions.extend(history)
    if all(v.get("version") != policy["version"] for v in versions):
        versions.append(
            {
                "version": policy["version"],
                "parent": policy.get("parent"),
                "origin": policy.get("origin"),
                "created_at": policy.get("created_at"),
                "changes": [policy.get("rationale", "")],
                "coverage_before": None,
                "coverage_after": None,
            }
        )
    return versions


def load_evidence(path: str | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    with open(path) as f:
        return json.load(f)


# --- svg -------------------------------------------------------------------


# The commands the footer prints; a test checks each one against the script
# it invokes, so the documented refresh cannot drift from the real CLIs
# (Codex review of PR #10, round 35).
REPRODUCE_COMMANDS: tuple[str, ...] = (
    "python3 scripts/mine-trace-failures.py --repo-dir . --save-evidence docs/rsi/trace-evidence.json",
    "python3 scripts/measure-policy-validity.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json --out-json docs/rsi/measurement.json",
    "python3 scripts/revise-improvement-policy.py docs/self-improvement-archive.jsonl --measurement docs/rsi/measurement.json --dry-run",
    "python3 scripts/render-rsi-dashboard.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json --out docs/rsi/dashboard.html",
)


def marker_epoch_index(epochs: list[dict], created_at: str | None) -> int:
    """Index of the last epoch that existed when a policy version was
    created, so a revision is drawn where it happened rather than at the
    newest round (Codex review of PR #10, round 5)."""
    if not epochs:
        return 0
    created_ms = measure_mod.parse_timestamp_ms(created_at)
    if created_ms is None:
        return len(epochs) - 1
    index = 0
    for i, epoch in enumerate(epochs):
        ts = epoch.get("timestamp_ms")
        if ts is not None and ts <= created_ms:
            index = i
    return index


def trigger_chart(before: dict, after: dict, versions: list[dict], min_coverage: float) -> str:
    epochs_b = before["epochs"]
    epochs_a = after["epochs"]
    if not epochs_b:
        return "<p>No rounds archived yet.</p>"
    w, h, pad_l, pad_r, pad_t, pad_b = 760, 300, 48, 24, 20, 40
    n = len(epochs_b)
    xs = [pad_l + (w - pad_l - pad_r) * (i / max(1, n - 1)) for i in range(n)]

    def y(v: float) -> float:
        return pad_t + (h - pad_t - pad_b) * (1 - v)

    def path(points: list[tuple[float, float]]) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{yy:.1f}" for i, (x, yy) in enumerate(points)
        )

    cov_b = [(xs[i], y(e["coverage"] or 0)) for i, e in enumerate(epochs_b)]
    cov_a = [(xs[i], y(e["coverage"] or 0)) for i, e in enumerate(epochs_a)]
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Coverage per round">']
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<line x1="{pad_l}" y1="{y(tick):.1f}" x2="{w - pad_r}" y2="{y(tick):.1f}" stroke="#e3e7ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{y(tick) + 4:.1f}" font-size="11" text-anchor="end" fill="{GREY}">{tick:.2f}</text>'
        )
    parts.append(
        f'<line x1="{pad_l}" y1="{y(min_coverage):.1f}" x2="{w - pad_r}" y2="{y(min_coverage):.1f}" stroke="{RED}" stroke-dasharray="6 4"/>'
    )
    parts.append(
        f'<text x="{w - pad_r}" y="{y(min_coverage) - 6:.1f}" font-size="11" text-anchor="end" fill="{RED}">MIN_COVERAGE {min_coverage}</text>'
    )
    parts.append(f'<path d="{path(cov_b)}" fill="none" stroke="{GREY}" stroke-width="2.5"/>')
    parts.append(f'<path d="{path(cov_a)}" fill="none" stroke="{ORANGE}" stroke-width="3"/>')
    for i, e in enumerate(epochs_b):
        parts.append(f'<circle cx="{xs[i]:.1f}" cy="{cov_b[i][1]:.1f}" r="3.5" fill="{GREY}"/>')
        parts.append(f'<circle cx="{xs[i]:.1f}" cy="{cov_a[i][1]:.1f}" r="3.5" fill="{ORANGE}"/>')
        parts.append(
            f'<text x="{xs[i]:.1f}" y="{h - pad_b + 16}" font-size="11" text-anchor="middle" fill="{GREY}">r{e["round"]}</text>'
        )
        # Validity squares belong to the CURRENT policy, whose coverage the
        # orange line shows (Codex review of PR #10, round 10).
        v = epochs_a[i].get("validity") if i < len(epochs_a) else None
        if v is not None:
            parts.append(
                f'<rect x="{xs[i] - 3:.1f}" y="{y(max(0, v)) - 3:.1f}" width="6" height="6" fill="{NAVY}"/>'
            )
    # revision / rollback markers at the epoch they were created after
    marker_n = 0
    for v in versions:
        if v.get("origin") in {"revision", "rollback"}:
            color = RED if v["origin"] == "rollback" else GREEN
            x = xs[marker_epoch_index(epochs_b, v.get("created_at"))]
            label_y = pad_t + 12 + 14 * (marker_n % 4)
            marker_n += 1
            parts.append(
                f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{h - pad_b}" stroke="{color}" stroke-width="2" stroke-dasharray="3 3"/>'
            )
            parts.append(
                f'<text x="{x - 6:.1f}" y="{label_y}" font-size="11" text-anchor="end" fill="{color}">v{esc(v["version"])} {esc(v["origin"])}</text>'
            )
    parts.append(
        f'<text x="{pad_l}" y="{h - 6}" font-size="11" fill="{GREY}">grey: coverage under v1 · orange: coverage under v{after["policy_version"]} · navy squares: v{after["policy_version"]} validity vs field anchor</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def loop_diagram(stats: dict) -> str:
    boxes = [
        ("1. AI system", "this repository's review-and-fix loop", 20, 40),
        ("2. Improver", f"Claude Code rounds: {stats['rounds']}", 210, 40),
        ("3. Strategy", f"policy v{stats['policy_version']} · {stats['policy_hash']}", 400, 40),
        ("4. Target", str(stats["target"]), 590, 40),
        ("5. Verifier", f"codex-review.yml · {stats['findings']} findings", 590, 170),
        ("6. Improvement", f"kept rounds: {stats['kept']} / {stats['rounds']}", 400, 170),
        ("7. Successor", f"main @ {stats['head']}", 210, 170),
    ]
    parts = [
        '<svg viewBox="0 0 780 300" width="100%" role="img" aria-label="Closed improvement loop">'
    ]
    parts.append(
        '<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#5b6b86"/></marker></defs>'
    )
    for title, sub, x, y in boxes:
        parts.append(f'<rect x="{x}" y="{y}" width="170" height="80" rx="8" fill="{NAVY}"/>')
        parts.append(
            f'<text x="{x + 10}" y="{y + 26}" font-size="14" font-weight="700" fill="#fff">{esc(title)}</text>'
        )
        # Every subtitle is escaped here, at the interpolation point: the
        # foreignObject renders live markup (Codex review of PR #10, round 13).
        parts.append(
            f'<foreignObject x="{x + 10}" y="{y + 34}" width="152" height="44"><div xmlns="http://www.w3.org/1999/xhtml" style="font:11px/1.3 system-ui;color:#dbe4f3">{esc(sub)}</div></foreignObject>'
        )
    arrows = [
        (190, 80, 210, 80),
        (380, 80, 400, 80),
        (570, 80, 590, 80),
        (675, 120, 675, 170),
        (590, 210, 570, 210),
        (400, 210, 380, 210),
        (210, 210, 105, 210),
        (105, 210, 105, 120),
    ]
    for x1, y1, x2, y2 in arrows:
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#5b6b86" stroke-width="2" marker-end="url(#arr)"/>'
        )
    parts.append(f'<rect x="300" y="262" width="360" height="30" rx="15" fill="{ORANGE}"/>')
    parts.append(
        '<text x="480" y="282" font-size="13" font-weight="700" text-anchor="middle" fill="#fff">L5: revise-improvement-policy.py rewrites box 3 and how box 5 is read</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --- page ------------------------------------------------------------------


def render(
    entries: list[dict],
    policy: dict,
    history: list[dict],
    evidence: dict | None,
    verifier_evidence: dict | None,
    head: str,
) -> str:
    v1 = initial_policy(history)
    before = measure_mod.measure(entries, v1, evidence)
    after = measure_mod.measure(entries, policy, evidence)
    echo = measure_mod.measure(entries, policy, verifier_evidence) if verifier_evidence else None
    versions = lineage(policy, history)
    rounds = measure_mod.rounds_in_order(entries)
    kept = sum(1 for e in entries if e.get("kept") is True)
    target = next((e.get("target") for e in reversed(entries) if e.get("target")), "n/a")
    stats = {
        "rounds": len(rounds),
        "findings": after["current"]["findings_total"],
        "kept": kept,
        "target": target,
        "head": head,
        "policy_version": policy["version"],
        "policy_hash": after["policy_hash"],
    }
    rollbacks = [v for v in versions if v.get("origin") == "rollback"]
    revisions = [v for v in versions if v.get("origin") == "revision"]
    regressions = [
        (v.get("coverage_after") or 0) - (v.get("coverage_before") or 0)
        for v in versions
        if v.get("coverage_after") is not None and v.get("coverage_before") is not None
    ]
    worst = min(regressions) if regressions else None
    kw_v1 = policy_mod.topic_keywords(v1)
    kw_now = policy_mod.topic_keywords(policy)
    cur = after["current"]
    decision_now = revise_mod.decide(entries, policy, history, after, policy_mod.utc_now_iso())

    def chip(text: str, color: str) -> str:
        return f'<span class="chip" style="background:{color}">{esc(text)}</span>'

    rows_levels = []
    for lvl, name, human, ai, retained, here in LEVELS:
        cls = ' class="l5"' if lvl == 5 else ""
        rows_levels.append(
            f"<tr{cls}><td><b>L{lvl}</b> {esc(name)}</td><td>{esc(human)}</td><td>{esc(ai)}</td><td>{esc(retained)}</td><td>{esc(here)}</td></tr>"
        )

    rows_versions = []
    for v in versions:
        color = {"init": GREY, "revision": GREEN, "rollback": RED}.get(v.get("origin"), GREY)
        changes = "".join(f"<li>{esc(c)}</li>" for c in v.get("changes", []))
        rows_versions.append(
            f"<tr><td>{chip('v' + str(v['version']), color)}</td><td>{esc(v.get('origin'))}</td>"
            f"<td>{esc(v.get('parent') if v.get('parent') is not None else '—')}</td><td>{esc(v.get('created_at') or '')}</td>"
            f"<td>{fmt(v.get('coverage_before'))} → {fmt(v.get('coverage_after'))}</td><td><ul>{changes}</ul></td></tr>"
        )

    rows_findings = []
    for rnd in rounds:
        for finding in rnd["findings"]:
            t1 = policy_mod.classify_finding(finding, kw_v1)
            t2 = policy_mod.classify_finding(finding, kw_now)
            # Validated counts only: definition mismatches, truncation and
            # unsearched topics read as n/a, never as zero (Codex review of
            # PR #10, round 17).
            hits: object = "n/a"
            if t2 and cur["anchor"] is not None and cur["anchor"].get(t2) is not None:
                hits = cur["anchor"][t2]
            newly = t1 is None and t2 is not None
            cls = ' class="newly"' if newly else ""
            rows_findings.append(
                f"<tr{cls}><td>r{rnd['round']}</td><td>{esc(finding[:140])}</td><td>{esc(t1 or '— (blind spot)')}</td>"
                f"<td>{esc(t2 or '— (blind spot)')}</td><td>{esc(hits)}</td></tr>"
            )

    rows_topics = []
    for topic, spec in policy["topics"].items():
        dev = cur["dev"].get(topic, 0)
        anchor = (cur["anchor"] or {}).get(topic) if cur["anchor"] else None
        mined = "mined" if spec.get("mined_from") else "v1"
        rows_topics.append(
            f"<tr><td>{esc(topic)}</td><td>{esc(', '.join(spec['keywords']))}</td><td>{fmt(spec.get('weight', 1.0))}</td>"
            f"<td>{dev}</td><td>{fmt(anchor)}</td><td>{mined}</td></tr>"
        )

    anchor_note = (
        f"{after['anchor']['source']} · agents {', '.join(after['anchor'].get('agents', []))} · "
        f"{after['anchor']['traces_considered']} trace(s)"
    )
    echo_note = ""
    if echo:
        echo_note = (
            f"<p><b>Echo check.</b> With the verifier's own Codex review sessions counted as the anchor, validity reads "
            f"<b>{fmt(echo['current']['validity'])}</b> over {echo['anchor']['traces_considered']} trace(s). "
            f"With them excluded it reads <b>{fmt(cur['validity'])}</b> ({esc(anchor_note)}). The first number agrees with the "
            f"review signal because it <i>is</i> the review signal; only the second is an independent anchor.</p>"
        )

    next_action = decision_now["action"]
    next_color = {"none": GREEN, "revise": ORANGE, "rollback": RED}[next_action]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSI Dashboard — L5 meta-improvement</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; font: 14px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; color:#1b2433; background:#f6f7fa; }}
  header {{ background:{NAVY}; color:#fff; padding:24px 32px; }}
  header h1 {{ margin:0 0 6px; font-size:24px; }}
  header p {{ margin:0; color:#c9d5ea; }}
  main {{ max-width:1180px; margin:0 auto; padding:24px 16px 48px; }}
  section {{ background:#fff; border:1px solid #e3e7ee; border-radius:10px; padding:20px 22px; margin:0 0 20px; }}
  h2 {{ font-size:17px; margin:0 0 12px; color:{NAVY}; }}
  h2 small {{ color:{GREY}; font-weight:400; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid #edf0f5; vertical-align:top; }}
  th {{ color:{GREY}; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  tr.l5 td {{ background:#fff4ea; font-weight:600; }}
  tr.newly td {{ background:#eefaf1; }}
  .chip {{ display:inline-block; color:#fff; border-radius:999px; padding:2px 10px; font-size:12px; font-weight:700; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }}
  .card {{ border:1px solid #e3e7ee; border-radius:10px; padding:14px 16px; background:#fbfcfe; }}
  .card h3 {{ margin:0 0 8px; font-size:14px; }}
  .stat {{ font-size:28px; font-weight:800; color:{NAVY}; }}
  .status {{ display:flex; flex-wrap:wrap; gap:14px; align-items:center; margin:12px 0 0; }}
  ul {{ margin:4px 0 0 18px; padding:0; }}
  code {{ background:#eef1f6; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .scroll {{ overflow-x:auto; }}
  footer {{ color:{GREY}; font-size:12px; text-align:center; padding:0 16px 32px; }}
</style></head>
<body>
<header>
  <h1>Level 5: recursive meta-improvement</h1>
  <p>The loop that decides target-vs-mechanism fixes now revises its own decision policy from measured evidence. Rendered from real repository state at <code>{esc(head)}</code>.</p>
  <div class="status">
    {chip(f"policy v{policy['version']} · {after['policy_hash']}", ORANGE)}
    {chip(f"coverage {fmt(cur['coverage'])} (v1: {fmt(before['current']['coverage'])})", NAVY)}
    {chip(f"validity {fmt(cur['validity'])}", NAVY)}
    {chip(f"{len(revisions)} revision(s) · {len(rollbacks)} rollback(s)", GREEN if not rollbacks else RED)}
    {chip(f"next decision: {next_action}", next_color)}
  </div>
</header>
<main>

<section>
  <h2>Autonomy matrix <small>— what this repository has internalized at each level</small></h2>
  <div class="scroll"><table>
    <tr><th>Level</th><th>Human keeps</th><th>AI internalizes</th><th>Retained update</th><th>Where it lives here</th></tr>
    {"".join(rows_levels)}
  </table></div>
</section>

<section>
  <h2>The closed improvement loop <small>— live values</small></h2>
  {loop_diagram(stats)}
</section>

<section>
  <h2>The L5 trigger <small>— does the policy's signal still predict the field?</small></h2>
  {trigger_chart(before, after, versions, revise_mod.MIN_COVERAGE)}
  <p>Coverage is the share of archived findings the policy can classify at all; a blind spot never accumulates toward the mechanism-fix threshold.
  Validity is Spearman agreement between review-derived recurrence and the independent field anchor ({esc(anchor_note)}).
  Fixed acceptance rule: revise when coverage &lt; {revise_mod.MIN_COVERAGE} or validity &lt; {revise_mod.MIN_VALIDITY}; roll back when a revision's coverage falls below its parent's after {revise_mod.MIN_ROUNDS_TO_JUDGE} further rounds.</p>
  {echo_note}
  <p><b>Decision if run now:</b> {esc(next_action)} — {esc(decision_now.get("reason", ""))}</p>
</section>

<section>
  <h2>Policy lineage <small>— every version, its parent, and why</small></h2>
  <div class="scroll"><table>
    <tr><th>Version</th><th>Origin</th><th>Parent</th><th>Created</th><th>Coverage before → after</th><th>Changes</th></tr>
    {"".join(rows_versions)}
  </table></div>
</section>

<section>
  <h2>Three systemic failure modes <small>— and the guard for each</small></h2>
  <div class="grid">
    <div class="card"><h3>Safe inheritance</h3>
      <div class="stat">{len(versions)} version(s)</div>
      <p>{len(rollbacks)} rollback(s). Worst coverage change across adopted revisions: <b>{fmt(worst)}</b>. Archive rounds kept: {kept}/{len(entries)} entries.</p>
      <p>Guard: append-only history with full policy snapshots; automatic rollback proposal when a revision underperforms its parent.</p></div>
    <div class="card"><h3>Autonomy attribution</h3>
      <div class="stat">{len(policy_mod.AI_OWNED_COMPONENTS)} AI-owned · {len(policy_mod.FIXED_INFRASTRUCTURE)} fixed</div>
      <p>AI may write:</p><ul>{"".join(f"<li><code>{esc(p)}</code></li>" for p in policy_mod.AI_OWNED_COMPONENTS.values())}</ul>
      <p>Fixed infrastructure:</p><ul>{"".join(f"<li><b>{esc(k)}</b>: {esc(v)}</li>" for k, v in policy_mod.FIXED_INFRASTRUCTURE.items())}</ul>
      <p>Guard: <code>assert_ai_may_write</code> refuses any other path; the acceptance thresholds are constants, not policy fields.</p></div>
    <div class="card"><h3>Reliable verification</h3>
      <div class="stat">{esc(after["policy_hash"])}</div>
      <p>Policy hash pinned for this measurement; a revision must re-measure before it can act (hash mismatch is refused).</p>
      <p>Anchor: {esc(anchor_note)}. The verifier's own transcripts are excluded by default so the anchor cannot echo the review signal.</p>
      <p>Evidence stored: trace ids, agents, timestamps only — no transcript text.</p></div>
  </div>
</section>

<section>
  <h2>Current taxonomy <small>— v{policy["version"]}</small></h2>
  <div class="scroll"><table>
    <tr><th>Topic</th><th>Keywords</th><th>Weight</th><th>Rounds with a finding</th><th>Field traces</th><th>Origin</th></tr>
    {"".join(rows_topics)}
  </table></div>
</section>

<section>
  <h2>Every archived finding <small>— under v1 and under v{policy["version"]}; green rows were blind spots v1 could not see</small></h2>
  <div class="scroll"><table>
    <tr><th>Round</th><th>Finding</th><th>Topic under v1</th><th>Topic under v{policy["version"]}</th><th>Field traces</th></tr>
    {"".join(rows_findings)}
  </table></div>
</section>

</main>
<footer>Reproduce: {" → ".join(f"<code>{esc(c)}</code>" for c in REPRODUCE_COMMANDS)}</footer>
</body></html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("archive_path")
    parser.add_argument("--policy", default=str(policy_mod.POLICY_PATH))
    parser.add_argument("--history", default=str(policy_mod.HISTORY_PATH))
    parser.add_argument("--trace-evidence", default=None)
    parser.add_argument("--verifier-evidence", default=None)
    parser.add_argument("--head", default="working tree")
    parser.add_argument(
        "--out", default=str(policy_mod.REPO_ROOT / "docs" / "rsi" / "dashboard.html")
    )
    args = parser.parse_args(argv[1:])

    policy_mod.assert_safe_output(
        args.out,
        inputs=[
            args.archive_path,
            args.policy,
            args.history,
            args.trace_evidence,
            args.verifier_evidence,
        ],
    )
    entries = measure_mod.load_archive(args.archive_path)
    policy = policy_mod.load_policy(args.policy)
    history = policy_mod.load_history(args.history)
    page = render(
        entries,
        policy,
        history,
        load_evidence(args.trace_evidence),
        load_evidence(args.verifier_evidence),
        args.head,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
