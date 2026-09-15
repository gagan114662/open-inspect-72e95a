# Schedule: on every independent review

`workflow_run` of "Codex Review" in `.github/workflows/archive-and-recommend.yml`.

When a review completes, the agent archives the round and checks whether any topic newly crossed the
threshold. When an archive-round PR merges into the default branch, it re-evaluates the whole merged
archive.
