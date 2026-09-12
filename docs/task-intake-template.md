# Task Intake Template

A standardized contract for any unit of work handed to an agent session or automation on this
deployment — matches the "factory-ready task packet" described in
[murraycole.com/posts/software-factory](https://murraycole.com/posts/software-factory). If a field
is vague, the agent can only automate that ambiguity; fill in every section before starting the
session.

This same template doubles as the entry format for the production-hardening backlog agreed on
2026-09-12 (deployment/rollback verification, budget-exhaustion and retry-storm testing,
concurrency/capacity controls, audit-trail completeness, operational recovery). Every backlog item
must carry its own **Acceptance test** and **Evidence** sections below — a backlog item without both
is not ready to work.

---

## Objective and non-goals

- **Objective:** _What outcome is required, in one or two sentences._
- **Non-goals:** _What must explicitly not change. Scope creep is a failure mode, not a bonus._

## Context

_Relevant files, prior decisions, known failure modes, links to related PRs/issues. Enough that the
agent isn't reconstructing history from scratch._

## Acceptance criteria

_Observable behavior, stated as pass/fail conditions — not "should work," but the specific
input/output or state change that proves it. Include edge cases._

## Capabilities

- **Allowed:** _tools, paths, commands, network access, credentials the session may use_
- **Denied:** _explicitly out of bounds — e.g. "must not touch `terraform/`", "must not merge its
  own PR"_

## Checks

_The exact tests, linters, builds, migrations, security scans, and scenario evaluations that must
run and pass. Name them, don't describe them — "npm run check in open-inspect-sandbox", not "run the
tests."_

## Terminal states

_What "done" looks like, and what isn't done: complete / retry / no-op / escalate-to-human. State
the condition for each._

## Acceptance test

_The independent, externally-controlled test that proves the objective was met — run by something
other than the agent that did the work. A build succeeding is not this. Name the actual test,
who/what runs it, and what a pass looks like. This is the field the article calls the difference
between "reported working" and "proven working."_

## Evidence

_What gets attached to the completed task as proof: check results (pass/fail per check, not just
overall), the diff, session ID, cost, duration, and — for backlog items specifically — the artifact
that shows the test above was actually run (log excerpt, screenshot, linked CI run)._

## Rollback

_The smallest safe way to undo this change if it turns out to be wrong post-merge/post-deploy._

---

## Worked example (this session's PR #8)

| Field               | Value                                                                                                                        |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Objective           | Prove the platform's own `create-pull-request` tool works end-to-end, not just manual git/gh                                 |
| Non-goals           | No application code changes, no CI/config changes                                                                            |
| Context             | `open-inspect-sandbox` repo, validation harness already live there                                                           |
| Acceptance criteria | A PR exists, authored by the platform session, with the new file present                                                     |
| Capabilities        | Allowed: write to `docs/`, use `create-pull-request` tool. Denied: merge its own PR                                          |
| Checks              | `npm run check` (lint + typecheck + test) in `open-inspect-sandbox`                                                          |
| Terminal states     | Complete = PR opened and left open; escalate if `create-pull-request` tool errors                                            |
| Acceptance test     | A human (or a second session) merges the PR only after CI + review pass — not the session that made it                       |
| Evidence            | PR #8, `session_pull_requests` row (`session_id: 22faf90b...`, `lifecycle_state: merged`), cost $0.2437, 117s created→merged |
| Rollback            | `git revert` the squash-merge commit                                                                                         |

This is the level of specificity every backlog item needs before it's picked up — including this
session's remaining open items (deployment/rollback, budget exhaustion, capacity controls, audit
trail, operational recovery).
