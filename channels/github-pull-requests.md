# Channel: GitHub pull requests

The only way this agent changes the repository.

- **Policy proposals** land on the standing branch `improvement-policy-proposal` (one at a time; a
  newer archive round supersedes the open one).
- **Evidence refreshes** with no policy change land on `improvement-evidence-refresh`, updated in
  place, never one PR per hour.
- **Archived rounds** land on `archive-rounds-pr-<N>`, one standing branch per reviewed pull
  request, rebuilt from the default branch on every run.

A human merges every one of them. Workflow: `.github/workflows/revise-improvement-policy.yml`,
`.github/workflows/archive-and-recommend.yml`.
