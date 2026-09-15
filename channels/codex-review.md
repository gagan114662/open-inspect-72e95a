# Channel: Codex review comments

Inbound. Every pull request gets an independent review from Codex, posted as a comment that starts
`### Codex independent review` and ends with two footers the workflow itself writes:
`<!-- codex-review-status: completed|failed|not-run -->` and `<!-- codex-review-sha: <commit> -->`.
The agent archives a comment only if it was posted by the Actions bot, is stamped completed, and its
last line binds it to the reviewed commit. Review text cannot forge either footer. Workflow:
`.github/workflows/codex-review.yml`.
