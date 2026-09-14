# Recursive meta-improvement (L5)

Source: "The Last AI Built by Humans — A Structural Framework for Genuine Recursive
Self-Improvement" (15-slide deck, September 2026). This document maps that framework onto the
self-improvement loop this repository already runs, names the gap, and records how the gap was
closed.

## The framework in one table

The closed improvement loop has seven parts: AI system, improver, strategy, target, verifier,
improvement, successor. Autonomy is measured by how many of those decisions have moved from fixed
human infrastructure into the AI's own persistent state:

| Level | Human keeps                        | AI internalizes        | Retained update       | Here                                                          |
| ----- | ---------------------------------- | ---------------------- | --------------------- | ------------------------------------------------------------- |
| L1    | objective, strategy, validation    | execution              | task outcome          | Claude Code applies a round's fix                             |
| L2    | objective, task bounds, validation | search rules           | search strategy       | each round chooses what to try from the previous findings     |
| L3    | environment parameters, validation | data generation        | practice curriculum   | `analyze-traces.py` / `sync-pr-traces.py` pull session traces |
| L4    | governance rules, rollbacks        | state management       | deployed state        | `archive-round.py` + `archive-and-recommend.yml`              |
| L5    | final oversight                    | the improver mechanism | the verifier/improver | `revise-improvement-policy.py` (this change)                  |

L5's benchmark in the deck (A-Evolve-Training): the system revises its own research policy when
development scores stop predicting external gains, then uses the revised policy to direct the next
round.

Three failure modes the design must guard against:

1. **Safe inheritance** — self-modification that degrades over time. Needs transfer tests, version
   history, automatic rollback.
2. **Autonomy attribution** — better candidates mistaken for a better search process. Needs explicit
   separation of AI-controlled logic from fixed infrastructure.
3. **Reliable verification** — repeated evaluator access rewards exploitation. Needs evaluators
   frozen per epoch and an independent ground-truth anchor.

## The gap

Before this change the loop was L4. `scripts/detect-recurring-pattern.py` decided
target-vs-mechanism fixes from a keyword taxonomy and a threshold that were constants in the file:
written once by hand, never measured, never revised. Two consequences were visible in the real
archive:

- 11 of 28 archived findings (39%) matched no topic at all, including every finding from rounds 8 to
  10 (archive threshold crossings, workflow concurrency, PR-creation recovery). A blind spot never
  accumulates toward the mechanism-fix threshold, so the loop could not notice its own newest
  recurring problem.
- Nothing checked whether a topic the reviews kept crediting ever appeared in actual working
  sessions.

## What changed

- `docs/improvement-policy.json` — the taxonomy, per-topic weights and threshold as a versioned
  document (`version`, `parent`, `origin`). `detect-recurring-pattern.py` reads it; the old module
  constants remain as views of the loaded policy so every caller keeps working.
- `scripts/improvement_policy.py` — load/validate/hash the policy, version it, and the attribution
  guard: `assert_ai_may_write` refuses any write outside the two AI-owned files.
- `scripts/measure-policy-validity.py` — the L5 trigger. Coverage (classified / total findings) and
  predictive validity (Spearman agreement between review-derived recurrence and Traces evidence from
  working sessions), replayed per archive round using only what existed at that round's timestamp.
  The verifier's own Codex review transcripts are excluded from the anchor by default; an empty
  anchor is treated as no anchor, so nothing is discounted for failing to appear in a field nobody
  observed.
- `scripts/revise-improvement-policy.py` — the meta-improver. Fixed acceptance rule (constants, not
  policy fields): revise when coverage < 0.8 or validity < 0.3; roll back when an adopted revision's
  coverage falls below its parent's after two further rounds. Revisions are bounded: at most two
  mined topics, each backed by at least two previously unclassified findings, keywords chosen by
  document frequency, appended after existing topics so nothing already classified changes bucket.
  Every version is appended to `docs/improvement-policy-history.jsonl` with a full snapshot.
- `scripts/render-rsi-dashboard.py` — `docs/rsi/dashboard.html`, a self-contained page rendered from
  the archive, the policy history and the evidence files: autonomy matrix, the loop with live
  values, the trigger chart, policy lineage, the three failure-mode guards, and every finding under
  v1 and under the current policy.
- `.github/workflows/revise-improvement-policy.yml` — runs after the archive changes on main and
  proposes the result as a pull request. Never pushes to main.

## First real run

Measured against the archive as of round 10 with policy v1: coverage 0.61, anchor empty (no working
sessions for this repository are indexed in Traces yet). The rule fired on coverage and proposed v2:
one mined topic covering 8 of the 11 blind-spot findings, coverage 0.61 → 0.89 (the remaining three
are single-occurrence findings no bounded rule may claim). A second pass under v2 proposes nothing.
With the verifier's own review sessions counted as the anchor, validity reads 0.95: the number
agrees with the review signal because it _is_ the review signal, which is why the default excludes
them.

Reproduce:

```bash
python3 scripts/measure-policy-validity.py docs/self-improvement-archive.jsonl --repo-dir . --save-evidence docs/rsi/trace-evidence.json
sed -n '/^---/,$p' <(python3 scripts/measure-policy-validity.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json) | tail -n +2 > docs/rsi/measurement.json
python3 scripts/revise-improvement-policy.py docs/self-improvement-archive.jsonl --measurement docs/rsi/measurement.json --dry-run
python3 scripts/render-rsi-dashboard.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json --out docs/rsi/dashboard.html
```
