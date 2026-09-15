# Schedule: hourly revise

`cron: "17 * * * *"` in `.github/workflows/revise-improvement-policy.yml`.

Each run: refresh the field anchor from the dedicated Traces namespace (`TRACES_NAMESPACE`,
`TRACES_NAMESPACE_IS_REPOSITORY=true`), measure coverage and validity, apply the acceptance rule,
re-render the dashboard, distill skills, and propose the result. A refresh that observed no session
keeps the committed snapshot instead of replacing it.
