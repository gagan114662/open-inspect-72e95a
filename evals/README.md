# evals

The third thing the slide says we still own. Every archived review round is a
real change plus an independent verdict on it, so the archive is an eval set:
given this diff, does a reviewer surface the same classes of problem Codex did?

```
evals/cases/round-NNN.json    built by scripts/build-evals.py from the archive
evals/results/<grader>-<stamp>.json   written by scripts/run-evals.py
```

Two graders:

- `keywords` (default, no model): does the diff itself contain the expected
  topic's policy keywords? That is the share of archived problems a pre-push
  keyword check could have caught before review, and the baseline any smarter
  reviewer has to beat.
- `codex`: runs `codex exec` on the diff with a review prompt and asks for
  findings labelled with the policy's topics. Recall is the share of expected
  topics the reviewer named. Slow (minutes per case); use `--limit`.

```bash
python3 scripts/build-evals.py                       # rebuild cases from the archive
python3 scripts/run-evals.py                         # keyword baseline over every case
python3 scripts/run-evals.py --grader codex --limit 5
```

Compare result files across runs to see whether a change to skills, prompts
or models moved recall. Cases carry scrubbed diffs only; never edit them by hand.
