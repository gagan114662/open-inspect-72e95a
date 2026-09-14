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

## Invariants the meta-improver must hold

Twenty-three rounds of independent Codex review on PR #10 converged on these. Every one is enforced
in code and covered by a regression test in `scripts/*_test.py`; a future change that breaks one
should fail the suite, not wait for a reviewer.

1. **One evidence window.** Every validity comparison in a decision (candidate acceptance, weight
   repair, rollback, the reported figure) uses the same rounds: those no later than the evidence
   snapshot's `collected_at`. Rounds newer than the snapshot never mark a topic as "credited by
   reviews, never seen in the field".
2. **Evidence is bound to its definition.** A count is valid only for the topic name AND the keyword
   list it was searched with. Renamed or re-mined topics, truncated searches, unsearched topics and
   undated traces in historical epochs are _unknown_, never zero.
3. **Evidence outlives the topic.** Refreshes keep searching every topic any recorded policy version
   ever had, and candidates are judged against the evidence-wide counts, so a rolled-back topic
   keeps the adverse evidence that stops it being re-mined on the same archive and snapshot. A name
   reused with different keywords keeps every definition (older ones under `name@tag` keys), and
   each policy version is judged on the evidence searched with its own keywords.
4. **Measurements are pinned.** A decision refuses a measurement whose policy hash or archive digest
   differs from what it is deciding on; topic order is part of the hash.
5. **Rounds are stamped.** Each archived round records the policy version and hash that decided it;
   a revision is judged only on rounds stamped with its own version and hash, and no further
   revision is layered on one that has not yet run for `MIN_ROUNDS_TO_JUDGE` rounds. Clean reviews
   are archived as rounds with no findings, so a policy that eliminates findings still accumulates
   the rounds needed to judge it.
6. **Ancestry is followed through rollbacks.** Rollback compares the current policy with every
   unjudged ancestor, following a rollback to the ancestry of the version it restored, and rolls
   back to the best-scoring ancestor; the recorded coverage is the restored policy's own.
7. **No candidate regresses.** A revision is refused if it lowers coverage or validity against the
   policy it replaces, or turns a defined validity into an undefined one; a rejected configuration
   is not retried until the archive or the evidence has changed.
8. **Bounded, unique mining.** At most two mined topics per revision, each backed by at least two
   findings no other topic claims, keywords by document frequency, names never colliding with
   existing topics, appended after existing topics so nothing already classified changes bucket.
9. **Writes are role-specific and guarded.** The meta-improver writes only the policy and its
   history, validates both destinations before writing either, refuses identical paths, and every
   report/JSON side output refuses protected files, canonical evidence snapshots, and the run's own
   inputs.
10. **Rendered output is escaped.** Every string from the archive, history or evidence is
    HTML-escaped at the point it enters the dashboard.
11. **The workflow proposes, humans merge.** One superseding proposal branch, same-repository PRs
    only, checkout pinned to the default branch, labelled with the commit actually measured,
    machine-readable JSON written apart from the human report, re-measured after a decision.

## What the field anchor is made of

The first anchor searched transcript text for the taxonomy's keywords and every hit was narration:
the assistant summarising review findings. Counting it made the field echo the reviews. The anchor
is now built by `scripts/mine-trace-failures.py`, which walks every event of each working session
through `traces show --json` and keeps only executions that went wrong: tool results Traces marked
as errors, and command tools that reported a non-zero exit. Output that merely contains
failure-shaped text (a file displayed with `cat`, a quoted finding) never counts. Each failure is
paired with the command that produced it, deduplicated per session by tool, command and excerpt,
matched independently against every topic's keywords, and written as evidence with the keyword
definitions it was searched under. Failures no topic claims are the field's blind spots; when at
least `MIN_FIELD_BLIND_SPOTS` of them exist, `revise-improvement-policy.py --field-failures` mines
topics from their output the same way it mines unclassified review findings.

First strict run over the working sessions in this folder: 96 distinct failures across 3 sessions,
validity 0.55 against the review signal, 69 blind spots dominated by "permission denied by the
auto-mode classifier" (28), tool input errors, and missing tools.

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
python3 scripts/mine-trace-failures.py --repo-dir . --save-evidence docs/rsi/trace-evidence.json
sed -n '/^---/,$p' <(python3 scripts/measure-policy-validity.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json) | tail -n +2 > docs/rsi/measurement.json
python3 scripts/revise-improvement-policy.py docs/self-improvement-archive.jsonl --measurement docs/rsi/measurement.json --dry-run
python3 scripts/render-rsi-dashboard.py docs/self-improvement-archive.jsonl --trace-evidence docs/rsi/trace-evidence.json --out docs/rsi/dashboard.html
```
