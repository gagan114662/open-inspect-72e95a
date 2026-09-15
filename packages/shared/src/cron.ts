/**
 * Cron expression utilities for the automation engine.
 *
 * Thin wrappers around `cron-parser` enforcing 5-field expressions only,
 * with timezone support via `Intl`.
 */

import { CronExpressionParser } from "cron-parser";
import { Data, Effect } from "effect";

type CronParseOptions = NonNullable<Parameters<typeof CronExpressionParser.parse>[1]>;

/** A cron expression `cron-parser` refused, with its reason. */
export class InvalidCronExpression extends Data.TaggedError("InvalidCronExpression")<{
  readonly expression: string;
  readonly reason: string;
}> {}

/** A time-zone name `Intl` does not know. */
export class InvalidTimeZone extends Data.TaggedError("InvalidTimeZone")<{
  readonly timeZone: string;
}> {}

function reasonOf(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

/**
 * Parse a cron expression as an Effect. Fails with `InvalidCronExpression`
 * instead of throwing; the sync wrappers below are built on it.
 */
export function parseCron(
  expression: string,
  options?: CronParseOptions
): Effect.Effect<ReturnType<typeof CronExpressionParser.parse>, InvalidCronExpression> {
  return Effect.try({
    try: () => CronExpressionParser.parse(expression, options),
    catch: (cause) => new InvalidCronExpression({ expression, reason: reasonOf(cause) }),
  });
}

/** Validate an IANA time-zone name as an Effect; succeeds with the name. */
export function validateTimeZone(timeZone: string): Effect.Effect<string, InvalidTimeZone> {
  return Effect.try({
    try: () => {
      Intl.DateTimeFormat(undefined, { timeZone });
      return timeZone;
    },
    catch: () => new InvalidTimeZone({ timeZone }),
  });
}

/** Fastest schedule cadence supported by automation persistence and execution. */
export const MIN_AUTOMATION_CRON_INTERVAL_MINUTES = 15;

/**
 * Return the next occurrence after `after` (defaults to now).
 */
export function nextCronOccurrence(expression: string, timezone: string, after?: Date): Date {
  const cron = CronExpressionParser.parse(expression, {
    tz: timezone,
    currentDate: after,
  });
  return cron.next().toDate();
}

/**
 * Validate a cron expression without throwing.
 * Only 5-field expressions are accepted.
 */
export function isValidCron(expression: string): boolean {
  // Reject 6-field (seconds) or 7-field expressions
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5) return false;

  return Effect.runSync(
    parseCron(expression).pipe(
      Effect.as(true),
      Effect.orElseSucceed(() => false)
    )
  );
}

/**
 * Return the interval in minutes between consecutive occurrences,
 * or `null` if intervals are not constant across the sample window.
 *
 * Constant-interval expressions (e.g., every 15 min, hourly, weekly)
 * return their exact interval. Variable-interval expressions (e.g., monthly)
 * return `null`, which bypasses the minimum-interval check — this is safe
 * because variable-interval crons cannot fire at sub-15-minute frequency.
 *
 * Used to enforce the 15-minute minimum interval.
 */
export function cronIntervalMinutes(expression: string): number | null {
  const sampled = Effect.gen(function* () {
    // Fixed reference point (a Wednesday) for deterministic interval sampling.
    const cron = yield* parseCron(expression, {
      currentDate: new Date("2025-01-01T00:00:00Z"),
      tz: "UTC",
    });

    // Sample the first 5 intervals. `next()` can throw past the parser's
    // horizon, so it stays inside the typed boundary too.
    const times = yield* Effect.try({
      try: () => Array.from({ length: 6 }, () => cron.next().toDate().getTime()),
      catch: (cause) => new InvalidCronExpression({ expression, reason: reasonOf(cause) }),
    });

    const intervals = new Set<number>();
    for (let i = 1; i < times.length; i++) {
      intervals.add((times[i] - times[i - 1]) / 60_000);
    }

    // Return the minimum observed interval (catches multi-value expressions
    // like "0,1 * * * *" whose shortest gap is 1 minute).
    return Math.min(...intervals);
  });
  return Effect.runSync(sampled.pipe(Effect.orElseSucceed((): number | null => null)));
}

/**
 * Validate the cron-specific automation contract shared by the API and form.
 * Returns the API-facing error message, or null when the expression is valid.
 */
export function validateAutomationCron(expression: string): string | null {
  if (!isValidCron(expression)) return "scheduleCron must be a valid 5-field cron expression";
  const interval = cronIntervalMinutes(expression);
  return interval !== null && interval < MIN_AUTOMATION_CRON_INTERVAL_MINUTES
    ? `Schedule interval must be at least ${MIN_AUTOMATION_CRON_INTERVAL_MINUTES} minutes`
    : null;
}

/** Validate an IANA time-zone name without throwing. */
export function isValidTimeZone(timeZone: string): boolean {
  return Effect.runSync(
    validateTimeZone(timeZone).pipe(
      Effect.as(true),
      Effect.orElseSucceed(() => false)
    )
  );
}

// ─── Preset descriptions ────────────────────────────────────────────────────

interface CronPreset {
  pattern: RegExp;
  describe: (match: RegExpMatchArray, options: CronDescriptionOptions) => string;
}

interface CronDescriptionOptions {
  timezone: string;
  compact: boolean;
}

function withTimezone(description: string, { timezone, compact }: CronDescriptionOptions): string {
  if (!compact) return `${description} (${timezone})`;
  if (timezone === "UTC") return `${description} (UTC)`;

  try {
    const timeZoneName = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      timeZoneName: "shortGeneric",
    })
      .formatToParts(new Date("2025-01-01T00:00:00Z"))
      .find((part) => part.type === "timeZoneName")?.value;
    return `${description} (${timeZoneName ?? timezone})`;
  } catch {
    return `${description} (${timezone})`;
  }
}

const PRESETS: CronPreset[] = [
  {
    // Every N minutes: */N * * * *
    pattern: /^\*\/(\d+) \* \* \* \*$/,
    describe: (m, options) => withTimezone(`Every ${m[1]} minutes`, options),
  },
  {
    // Every hour at minute M: M * * * *
    pattern: /^(\d+) \* \* \* \*$/,
    describe: (m, options) =>
      withTimezone(
        `${options.compact ? "Hourly" : "Every hour"} at :${m[1].padStart(2, "0")}`,
        options
      ),
  },
  {
    // Every day at H:M: M H * * *
    pattern: /^(\d+) (\d+) \* \* \*$/,
    describe: (m, options) =>
      withTimezone(
        `${options.compact ? "Daily" : "Every day"} at ${formatTime(
          parseInt(m[2]),
          parseInt(m[1]),
          options.compact
        )}`,
        options
      ),
  },
  {
    // Every weekday at H:M: M H * * 1-5
    pattern: /^(\d+) (\d+) \* \* 1-5$/,
    describe: (m, options) =>
      withTimezone(
        `${options.compact ? "Weekdays" : "Every weekday"} at ${formatTime(
          parseInt(m[2]),
          parseInt(m[1]),
          options.compact
        )}`,
        options
      ),
  },
  {
    // Every specific day at H:M: M H * * D
    pattern: /^(\d+) (\d+) \* \* (\d)$/,
    describe: (m, options) => {
      const day = DAY_NAMES[parseInt(m[3])];
      const frequency = options.compact ? `${day}s` : `Every ${day}`;
      return withTimezone(
        `${frequency} at ${formatTime(parseInt(m[2]), parseInt(m[1]), options.compact)}`,
        options
      );
    },
  },
];

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function formatTime(hour: number, minute: number, compact = false): string {
  const suffix = hour >= 12 ? "PM" : "AM";
  const h = hour % 12 || 12;
  if (compact && minute === 0) return `${h} ${suffix}`;
  return `${h}:${minute.toString().padStart(2, "0")} ${suffix}`;
}

/**
 * Produce a human-readable description of a cron expression.
 * Uses preset detection with fallback to the raw expression.
 */
export function describeCron(
  expression: string,
  timezone: string,
  options: { compact?: boolean } = {}
): string {
  const trimmed = expression.trim();
  const descriptionOptions = { timezone, compact: options.compact ?? false };
  for (const preset of PRESETS) {
    const match = trimmed.match(preset.pattern);
    if (match) return preset.describe(match, descriptionOptions);
  }
  return withTimezone(trimmed, descriptionOptions);
}
