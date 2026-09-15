/**
 * Buffer a request/response body stream under a hard byte cap.
 *
 * `readBody` is the Effect-native surface: it succeeds with the concatenated
 * bytes (an empty array for a missing body) and fails with a typed error the
 * caller has to handle, either `BodyTooLarge` once the cap is exceeded or
 * `BodyReadFailed` when the underlying reader rejects. The stream is
 * cancelled on either failure and when the caller is interrupted.
 *
 * `readBodyCapped` is the Promise adapter that existing callers use: it turns
 * `BodyTooLarge` into `null` and rethrows the original cause of a read failure,
 * so its behaviour is unchanged by the migration.
 */

import { Cause, Data, Effect, Exit, Option } from "effect";

export class BodyTooLarge extends Data.TaggedError("BodyTooLarge")<{
  readonly maxBytes: number;
  readonly receivedBytes: number;
}> {}

export class BodyReadFailed extends Data.TaggedError("BodyReadFailed")<{
  readonly cause: unknown;
}> {}

export type ReadBodyError = BodyTooLarge | BodyReadFailed;

export function readBody(
  body: ReadableStream<Uint8Array> | null,
  maxBytes: number
): Effect.Effect<Uint8Array<ArrayBuffer>, ReadBodyError> {
  if (body === null) return Effect.succeed(new Uint8Array());

  // The reader is a resource: whatever way the read ends (success, a typed
  // failure, or the caller's fiber being interrupted by a timeout), the
  // release step runs. A stalled body is therefore cancelled and unlocked
  // instead of staying open after the caller stopped waiting (Codex review
  // of PR #75, round 1).
  return Effect.acquireUseRelease(
    // A stream that is already locked makes getReader() throw; that is a
    // typed BodyReadFailed the caller can recover from, not a defect
    // (Codex review of PR #75, round 3).
    Effect.try({
      try: () => body.getReader(),
      catch: (cause) => new BodyReadFailed({ cause }),
    }),
    (reader) =>
      Effect.gen(function* () {
        const chunks: Uint8Array[] = [];
        let totalBytes = 0;
        while (true) {
          const { done, value } = yield* Effect.tryPromise({
            try: () => reader.read(),
            catch: (cause) => new BodyReadFailed({ cause }),
          });
          if (done) break;
          totalBytes += value.byteLength;
          if (totalBytes > maxBytes) {
            return yield* new BodyTooLarge({ maxBytes, receivedBytes: totalBytes });
          }
          chunks.push(value);
        }

        const bytes = new Uint8Array(totalBytes);
        let offset = 0;
        for (const chunk of chunks) {
          bytes.set(chunk, offset);
          offset += chunk.byteLength;
        }
        return bytes;
      }),
    (reader, exit) =>
      Effect.sync(() => {
        // Cancellation is started, never awaited: `cancel()` runs the
        // source's cancel step synchronously and returns a promise that a
        // stalled source may never settle, and a finalizer that waits on
        // it would keep the timeout or interruption pending forever (Codex
        // review of PR #75, round 2). The lock is released right after, so
        // the stream is usable again whatever the source does.
        if (!Exit.isSuccess(exit)) void reader.cancel().catch(() => undefined);
        reader.releaseLock();
      })
  );
}

/**
 * Promise adapter: the concatenated bytes, an empty array for a missing body,
 * or null once the cap is exceeded. A failed read rejects with its original
 * cause, exactly as the pre-Effect implementation did.
 */
export async function readBodyCapped(
  body: ReadableStream<Uint8Array> | null,
  maxBytes: number
): Promise<Uint8Array<ArrayBuffer> | null> {
  const exit = await Effect.runPromiseExit(readBody(body, maxBytes));
  if (Exit.isSuccess(exit)) return exit.value;

  const failure = Cause.failureOption(exit.cause);
  if (Option.isSome(failure)) {
    if (failure.value._tag === "BodyTooLarge") return null;
    throw failure.value.cause;
  }
  throw Cause.squash(exit.cause);
}
