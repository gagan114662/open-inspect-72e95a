# Production Hardening Backlog

Items agreed on 2026-09-12 while stress-testing the deployment against
[murraycole.com/posts/software-factory](https://murraycole.com/posts/software-factory). Each entry
follows `docs/task-intake-template.md` — no item here is ready to pick up without its own Acceptance
test and Evidence sections filled in first.

Status legend: **Open** (not started) · **In progress** · **Blocked** · **Done** (with evidence
linked).

---

## 1. Establish an eligible non-author reviewer path

**Status:** Done — 2026-09-12. See Evidence below.

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

- [x] Reviews the current commit (not a stale one) and submits a **formal** GitHub approval —
      visible in `GET /pulls/{n}/reviews` with `state: APPROVED`, not just a PR comment.
- [x] GitHub's own mergeability check counts that approval toward branch protection
      (`mergeable_state` moves off `blocked`/`review_required` because of it, not because of an
      unrelated override).
- [x] Unresolved findings from that reviewer **block** approval — i.e. it can also submit
      `CHANGES_REQUESTED`, and does so when there's a real issue (already demonstrated on PR #9's
      first commit; re-confirmed on PR #10, where the reviewer caught a genuine, unplanned bug — a
      missing import — and requested changes on it before approving the fix).
- [x] A subsequent code change after approval requires fresh review — proved on PR #10 with an
      isolated test: approved → pushed a new commit → review auto-`DISMISSED` by GitHub's
      `dismiss_stale_reviews` → `mergeable_state` reverted to `blocked` → fresh `review again` → new
      formal review bound to the new SHA.
- [x] A real PR merges through normal branch protection (required status check + required approval)
      with **no** `--admin` flag and no protection changes made to force it through. Both PR #9 and
      PR #10 merged this way.

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

**Audit findings** (read-only, done before any code change, per instruction not to create a new
identity or expand permissions until the audit established what was actually needed):

- The bot's identity and `pull_requests: write` permission were already sufficient — proven by PR
  #8's real `APPROVED` review and PR #9's real `CHANGES_REQUESTED` review, both formal and
  GitHub-counted, both submitted before this fix existed.
- The sandbox's `gh` CLI authenticates via the GitHub App's installation token
  (`packages/modal-infra/src/sandbox/vcs_env.py:29-38`), a genuinely separate identity from the
  human PR author.
- The actual gap was pure code: `buildCodeReviewPrompt` (`packages/github-bot/src/prompts.ts`, used
  only by the one-time `pull_request.opened` auto-review) included formal
  `gh api .../pulls/{n}/reviews` instructions; `buildCommentActionPrompt` (used by every `@mention`
  comment trigger) never did — it only posted plain issue comments.
- No new identity or permission expansion was used. Fix was entirely in `packages/github-bot`.

**Implementation** (commit `719fb205`, deployed via targeted `terraform apply`):

- `isReReviewRequest` (`github-mention.ts`) — deliberately tight trigger, must _lead_ with "(please)
  re-review", not merely mention the word.
- `buildReReviewPrompt` (`prompts.ts`) — formal review submission bound to the head SHA via
  `commit_id`, with an explicit re-check of the head immediately before submitting in case a new
  commit landed mid-review. Verdict must come from actually inspecting the diff, not CI status.
- `fetchPullRequestSummary` (`github-auth.ts`) — `issue_comment` webhooks carry no PR head info;
  fetches it fresh at request time.
- `dismiss_stale_reviews: true` added to branch protection (all other settings unchanged).
- 22 new/changed tests (`github-mention.test.ts` + `handlers.test.ts`), including a real bug the
  test-writing process itself caught: the first regex (`re-?view\b`) matched "review"/"re-view" but
  not "re-review" ("re" + "-" + "review", not "re" + "-" + "view") — found by the deliberately
  literal test case, fixed before commit.

**Live proof, PR #9** (https://github.com/gagan114662/open-inspect-sandbox/pull/9): real bug →
independent CI failure → bot repair → CI passes → `@bot review again` → formal `APPROVED` review (id
`5187416625`) bound via `commit_id` to the exact fixed commit
(`9ae4e2d9eb1c74e7424057f98e272b07723cf747`) → `mergeable_state` cleared → merged via
`gh pr merge --squash`, no `--admin`.

**Live proof, PR #10** (https://github.com/gagan114662/open-inspect-sandbox/pull/10) — isolated test
of criterion 4 plus an unplanned real bug catch:

1. Formal `APPROVED` on the initial commit (`58fe982...`).
2. Pushed a new commit → review auto-`DISMISSED`, `mergeable_state` → `blocked`.
3. `review again` → bot found a genuine bug (missing `ratioOf` import in the test file, not a staged
   scenario) → formal `CHANGES_REQUESTED`, with the exact fix in the inline comment.
4. Fixed the import, pushed, `review again` → formal `APPROVED` bound to the final commit
   (`3aa72daff1992dd22a0b29ee288d139c48fa20b0`) → `mergeable_state: clean` → merged, no `--admin`.

### Rollback

Revert whatever mechanism was added (webhook config, App permission, second account's collaborator
access) and branch protection reverts to its current, correctly-strict state — no data or history to
undo.
