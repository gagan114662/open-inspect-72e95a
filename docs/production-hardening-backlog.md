# Production Hardening Backlog

Items agreed on 2026-09-12 while stress-testing the deployment against
[murraycole.com/posts/software-factory](https://murraycole.com/posts/software-factory). Each entry
follows `docs/task-intake-template.md` — no item here is ready to pick up without its own Acceptance
test and Evidence sections filled in first.

Status legend: **Open** (not started) · **In progress** · **Blocked** · **Done** (with evidence
linked).

---

## 1. Establish an eligible non-author reviewer path

**Status:** Open — next up.

### Objective and non-goals

- **Objective:** a reviewer identity other than the PR author that can submit a formal,
  commit-specific GitHub approval, so author-created PRs can satisfy branch protection through
  normal review instead of stalling.
- **Non-goals:** does not weaken `required_approving_review_count`, does not enable admin-override
  merges, does not change what counts as a blocking finding.

### Context

Found via `open-inspect-sandbox` PR #9 (2026-09-12): a real bug → independent CI failure → bot
repair → passing CI loop was fully proven, but the PR could not merge. GitHub blocks self-approval
structurally (PR author's own account, or an account acting on their behalf, cannot approve their
own PR). Two bot-mediated paths were tried and both fail to produce a fresh, commit-specific formal
approval:

- Comment-triggered re-review (`@bot please re-review`) posts a plain issue comment, not a
  `reviews.createReview` call — only `pull_request.opened` triggers a formal review submission
  (`packages/github-bot/src/handlers.ts` → `handlePullRequestOpened`), and that fires once, on the
  original (buggy) commit.
- Formally requesting review from the bot via `POST .../requested_reviewers` fails with "Reviews may
  only be requested from collaborators" — GitHub Apps aren't addressable as reviewers this way in
  this installation's current configuration.

An admin-override merge (`gh pr merge --admin`) was available in principle (`enforce_admins: false`)
but was refused by Claude Code's own safety layer ("Merge Without Review") and correctly not
attempted further — that path stays deliberately untested, not proven-impossible.

### Acceptance criteria

- [ ] Reviews the current commit (not a stale one) and submits a **formal** GitHub approval —
      visible in `GET /pulls/{n}/reviews` with `state: APPROVED`, not just a PR comment.
- [ ] GitHub's own mergeability check counts that approval toward branch protection
      (`mergeable_state` moves off `blocked`/`review_required` because of it, not because of an
      unrelated override).
- [ ] Unresolved findings from that reviewer **block** approval — i.e. it can also submit
      `CHANGES_REQUESTED`, and does so when there's a real issue (already demonstrated: the bot did
      this correctly on PR #9's first commit — the gap is only the _repeat/fresh-commit_ case).
- [ ] A subsequent code change after approval requires fresh review — either GitHub's native
      `dismiss_stale_reviews` behavior, or the reviewer path re-submitting on new commits;the
      approval must not silently carry over to unreviewed code.
- [ ] A real PR merges through normal branch protection (required status check + required approval)
      with **no** `--admin` flag and no protection changes made to force it through.

### Capabilities

- **Allowed:** modify `packages/github-bot` review-submission logic, GitHub App permissions/webhook
  events, branch protection config (e.g. `dismiss_stale_reviews`), or add a second reviewer identity
  (human account or properly-collaborator-registered bot).
- **Denied:** lowering `required_approving_review_count`, enabling any auto-approve-on-label path,
  admin-override merges as the "fix."

### Checks

- `open-inspect-sandbox`'s own CI (`npm run check`) for any code changes to the review logic itself.
- A live end-to-end repeat of the PR #9 scenario: push a real bug, let CI fail, request a fix,
  confirm the new reviewer path formally approves the corrected commit and the PR becomes mergeable
  without override.

### Terminal states

- **Complete:** a real PR in `open-inspect-sandbox` merges via normal protection using this path,
  linked as evidence below.
- **Escalate:** if closing this requires adding a second human GitHub account or a paid GitHub plan
  feature — that's a decision for the deployment owner, not something to route around silently.

### Acceptance test

An independent, unmodified run of the PR #9 scenario (bug → CI fail → repair → this reviewer path
approves → merge) performed _after_ this item is implemented, not a retroactive claim about PR #9
itself (which stays as historical evidence of the gap, not the fix).

### Evidence

_(fill in when closed)_ Linked PR, review ID showing `state: APPROVED` on the correct commit SHA,
`mergeable_state` transition, and the merge commit — same rigor as the
`docs/task-intake-template.md` worked example.

### Rollback

Revert whatever mechanism was added (webhook config, App permission, second account's collaborator
access) and branch protection reverts to its current, correctly-strict state — no data or history to
undo.
