# Channel: GitHub issues

When a finding class crosses the mechanism-fix threshold on the merged archive, the agent opens one
issue titled `Recurring pattern: <topic> — mechanism-level fix recommended`, labelled
`self-improvement-recommendation`, and never a second one for a topic that already has one open.
Deciding the fix is a human's job. Workflow: `.github/workflows/archive-and-recommend.yml`.
