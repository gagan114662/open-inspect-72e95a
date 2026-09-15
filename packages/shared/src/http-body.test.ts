import { Effect, Exit } from "effect";
import { describe, expect, it } from "vitest";

import { BodyReadFailed, BodyTooLarge, readBody, readBodyCapped } from "./http-body";

function streamOf(...chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

function failingStream(error: unknown): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      controller.error(error);
    },
  });
}

describe("readBodyCapped", () => {
  it("reads a missing body as zero bytes", async () => {
    expect(await readBodyCapped(null, 10)).toEqual(new Uint8Array());
  });

  it("concatenates chunks in order under the cap", async () => {
    const body = await readBodyCapped(
      streamOf(new Uint8Array([1, 2]), new Uint8Array([3]), new Uint8Array([4, 5])),
      5
    );
    expect(body).toEqual(new Uint8Array([1, 2, 3, 4, 5]));
  });

  it("returns null and cancels the stream once the cap is exceeded", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new Uint8Array(4));
      },
      cancel() {
        cancelled = true;
      },
    });
    expect(await readBodyCapped(stream, 7)).toBeNull();
    expect(cancelled).toBe(true);
  });

  it("rethrows the original cause when the stream fails", async () => {
    const boom = new Error("socket reset");
    await expect(readBodyCapped(failingStream(boom), 10)).rejects.toBe(boom);
  });
});

describe("readBody (Effect)", () => {
  it("succeeds with the concatenated bytes", async () => {
    const bytes = await Effect.runPromise(
      readBody(streamOf(new Uint8Array([9]), new Uint8Array([8, 7])), 3)
    );
    expect(bytes).toEqual(new Uint8Array([9, 8, 7]));
  });

  it("fails with BodyTooLarge carrying the cap and the bytes seen so far", async () => {
    const exit = await Effect.runPromiseExit(readBody(streamOf(new Uint8Array(4)), 3));
    expect(Exit.isFailure(exit)).toBe(true);
    const error = await Effect.runPromise(Effect.flip(readBody(streamOf(new Uint8Array(4)), 3)));
    expect(error).toBeInstanceOf(BodyTooLarge);
    expect(error).toMatchObject({ _tag: "BodyTooLarge", maxBytes: 3, receivedBytes: 4 });
  });

  it("fails with BodyReadFailed wrapping the reader's rejection", async () => {
    const boom = new Error("socket reset");
    const error = await Effect.runPromise(Effect.flip(readBody(failingStream(boom), 10)));
    expect(error).toBeInstanceOf(BodyReadFailed);
    expect(error).toMatchObject({ _tag: "BodyReadFailed", cause: boom });
  });

  it("lets callers recover from the typed error with catchTag", async () => {
    const result = await Effect.runPromise(
      readBody(streamOf(new Uint8Array(4)), 3).pipe(
        Effect.catchTag("BodyTooLarge", (e) => Effect.succeed(`too large: ${e.receivedBytes}`))
      )
    );
    expect(result).toBe("too large: 4");
  });
});
