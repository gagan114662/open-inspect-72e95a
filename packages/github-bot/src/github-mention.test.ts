import { describe, it, expect } from "vitest";
import { containsBotMention, stripBotMention, isReReviewRequest } from "./github-mention";

describe("isReReviewRequest", () => {
  it("matches a bare 'review'", () => {
    expect(isReReviewRequest("review")).toBe(true);
  });

  it("matches 'review again'", () => {
    expect(isReReviewRequest("review again")).toBe(true);
  });

  it("matches 'please review'", () => {
    expect(isReReviewRequest("please review")).toBe(true);
  });

  it("matches a full sentence that leads with re-review", () => {
    expect(
      isReReviewRequest(
        "please re-review the current state of this PR now that the fix has been pushed."
      )
    ).toBe(true);
  });

  it("is case-insensitive", () => {
    expect(isReReviewRequest("REVIEW AGAIN")).toBe(true);
  });

  it("tolerates leading/trailing whitespace", () => {
    expect(isReReviewRequest("  review again  ")).toBe(true);
  });

  it("does not match a request that merely mentions 'review' mid-sentence", () => {
    expect(isReReviewRequest("the review comment above has a typo, please fix it")).toBe(false);
  });

  it("does not match an unrelated bug-fix request", () => {
    expect(
      isReReviewRequest("CI is failing, please fix the implementation so the tests pass")
    ).toBe(false);
  });

  it("does not match a word that merely starts with 'review'-like letters out of order", () => {
    expect(isReReviewRequest("reviewer needed for this")).toBe(false);
  });
});

describe("containsBotMention", () => {
  it("detects an exact mention", () => {
    expect(
      containsBotMention("@open-inspect-gagan[bot] please review", "open-inspect-gagan[bot]")
    ).toBe(true);
  });

  it("returns false with no botUsername configured", () => {
    expect(containsBotMention("@anything hello", undefined)).toBe(false);
  });

  it("returns false when the bot isn't mentioned", () => {
    expect(containsBotMention("hello world", "open-inspect-gagan[bot]")).toBe(false);
  });
});

describe("stripBotMention", () => {
  it("removes the mention and trims the remainder", () => {
    expect(
      stripBotMention("@open-inspect-gagan[bot]   review again", "open-inspect-gagan[bot]")
    ).toBe("review again");
  });
});
