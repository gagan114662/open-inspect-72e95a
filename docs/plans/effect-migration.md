# Effect Migration

## Status

Pilot landed in `packages/shared` (this PR). Everything after phase 0 is proposed and lands one
bounded module per PR, each reviewed by Codex and merged by a human. Nothing in this plan changes a
public function's signature; Effect surfaces are added next to the Promise/boolean ones and the old
names become thin adapters.

## Why

The product code hides its failure modes in `try {} catch {}` blocks and `Promise` rejections that
carry `unknown`. A caller cannot see from a signature which errors it must handle, so review has to
find missing handling by reading bodies. [Effect](https://effect.website) makes the error channel
part of the type: `Effect<A, E>` says what can go wrong, `catchTag` proves it was handled, and the
compiler fails when a new error is introduced without handling. That is the property the Codex
review loop keeps flagging by hand today (unchecked exit statuses, swallowed rejections, error paths
treated as success).

## Inventory (2026-09-15)

| Package       | TS source files | `async` functions | `try/catch` blocks | zod schemas |
| ------------- | --------------- | ----------------- | ------------------ | ----------- |
| control-plane | 423             | 268               | 186                | 75          |
| web           | 397             | 132               | 116                | 10          |
| shared        | 78              | 8                 | 9                  | 34          |
| slack-bot     | 62              |                   |                    |             |
| linear-bot    | 28              |                   |                    |             |
| github-bot    | 13              |                   |                    |             |

Counts are from `grep -c` over `src/**/*.ts`; they size the work, they are not a scoreboard.

## Rules for every migration PR

1. **Add, adapt, never rename.** The Effect function gets a new name (`readBody`, `parseCron`); the
   existing export keeps its signature and delegates. Callers move to the Effect surface only when
   their own module migrates.
2. **Errors are `Data.TaggedError` classes** with the fields a handler needs (`maxBytes`,
   `receivedBytes`, `expression`, `reason`). No string errors, no `unknown` in the failure channel.
3. **Adapters preserve behaviour bit for bit.** A Promise adapter rethrows the original cause of an
   unexpected failure (`Cause.squash`), and a boolean adapter maps exactly the errors the old
   `catch` swallowed. Tests pin this.
4. **Anything with a close/cancel/release step is a resource.** Readers, sockets, locks and temp
   files are acquired with `Effect.acquireUseRelease` (or a `Scope`), so the release runs on
   success, on a typed failure and when the caller's fiber is interrupted by a timeout. A test
   interrupts the fiber and asserts the release ran (Codex review of PR #75, round 1).
5. **One module per PR, tests first.** A PR that touches more than one module or changes a caller
   without migrating it is split.
6. **Codex reviews the diff; a human merges.** Same loop as every other change in this fork.
7. **No Effect in `packages/web` React components** until the server modules are done. Effect in the
   browser bundle is a size and readability question to decide separately.

## Phases

### Phase 0: pilot (this PR)

- `effect@3.22.2` added to `packages/shared`.
- `http-body.ts`: `readBody` with `BodyTooLarge` and `BodyReadFailed`; `readBodyCapped` adapts.
- `cron.ts`: `parseCron` and `validateTimeZone` with `InvalidCronExpression` and `InvalidTimeZone`;
  `isValidCron`, `cronIntervalMinutes`, `isValidTimeZone` adapt. Four `try/catch` blocks removed.
- Tests cover the typed errors, `catchTag` recovery, and adapter parity.

### Phase 1: `packages/shared` (78 files, 9 `try/catch`)

Order by blast radius, smallest first: `regex.ts`, `git.ts`, `user-id.ts`, `service-auth.ts`,
`auth.ts`, then `completion/extractor.ts` and `triggers/`. zod stays; `Schema` from Effect is a
later, separate decision because the 34 shared schemas are shared with `web`.

### Phase 2: `packages/github-bot`, `linear-bot`, `slack-bot` (13 / 28 / 62 files)

Small, webhook-shaped, mostly `fetch` plus JSON parsing: ideal for `Effect.tryPromise` and
`Effect.retry` with typed transport errors. `slack-bot/attachments.ts` already calls
`readBodyCapped`; it becomes the first caller to use `readBody` directly.

### Phase 3: `packages/control-plane` services (268 `async`, 186 `try/catch`)

Start at the leaves: `auth/service/request-authenticator.ts` and `routes/session-attachments.ts`
(both call `readBodyCapped`), then the sandbox and session services. Durable Objects and Workers
handlers keep Promise boundaries; `Effect.runPromise` runs at the handler edge. Introduce
`Layer`/`Context` for services only once two or more services share a dependency, not before.

### Phase 4: `packages/web` server code (132 `async`, 116 `try/catch`)

Route handlers and proxies (`lib/browser-auth-proxy.ts`, `lib/settings-proxy.ts`) as in phase 3.
Components stay untouched.

## Tooling

- `npx @effect-migrate/cli audit` gives a per-file inventory of migration candidates; run it at the
  start of each phase and commit the report under `docs/rsi/effect-audit-<phase>.json` so the
  dashboard can show progress. (Not run for the pilot: the CLI's own dependency tree warned about
  duplicate Effect versions, which is worth resolving before trusting its output.)
- The Effect language-service plugin catches duplicate `effect` copies at compile time; add it to
  `tsconfig` in phase 1 if a second package starts depending on `effect`.

## Not doing

- No `Effect.Schema` replacement of zod in this plan.
- No `@effect/platform` HTTP client until phase 2 shows the bots need it.
- No Effect version 4 (still a release candidate); pin 3.22.x and upgrade in one PR later.
