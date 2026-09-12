import { escapeRegExp } from "@open-inspect/shared/regex";

function botMentionPattern(botUsername: string, flags: string): RegExp {
  return new RegExp(`@${escapeRegExp(botUsername)}(?![A-Za-z0-9-])`, flags);
}

export function containsBotMention(body: string, botUsername: string | undefined): boolean {
  return botUsername ? botMentionPattern(botUsername, "i").test(body) : false;
}

export function stripBotMention(body: string, botUsername: string): string {
  return body.replace(botMentionPattern(botUsername, "gi"), "").trim();
}

// Deliberately tight: must LEAD with the review keyword (after an optional
// "please"), not merely mention the word "review" somewhere in a longer
// request. A loose substring match would let unrelated comments ("the
// review comment above is wrong, please fix the typo") accidentally trigger
// a formal GitHub review submission instead of a plain comment response.
// "re-?view" alone would match "review"/"re-view" but not "re-review"
// ("re" + "-" + "review", not "re" + "-" + "view") - the optional "(re-?)?"
// prefix handles that case separately from the base "review" word.
const RE_REVIEW_PATTERN = /^(please\s+)?(re-?)?review\b/i;

/** True for an explicit "(please) re-review" style request — see prompts.ts's buildReReviewPrompt. */
export function isReReviewRequest(strippedBody: string): boolean {
  return RE_REVIEW_PATTERN.test(strippedBody.trim());
}
