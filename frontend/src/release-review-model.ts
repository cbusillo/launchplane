import type { ReleaseReviewItem } from "./generated/openapi.ts";

export const NOTHING_TO_TEST = "Nothing for the Client to test";
// role-words: legacy marker that pull requests written before the role words changed carry.
const LEGACY_NOTHING_TO_TEST = "Nothing for the owner to test";

function nothingToTestMarker(notes: string): string | undefined {
  const lowered = notes.trim().toLowerCase();
  return [NOTHING_TO_TEST, LEGACY_NOTHING_TO_TEST].find(marker => lowered.startsWith(marker.toLowerCase()));
}

export type GroupedReleaseItems = {
  checks: ReleaseReviewItem[];
  nothingToTest: ReleaseReviewItem[];
};

// Engineering-only changes stay collapsed; every Client check stays per pull request.
export function groupReleaseItems(items: readonly ReleaseReviewItem[]): GroupedReleaseItems {
  const checks: ReleaseReviewItem[] = [];
  const nothingToTest: ReleaseReviewItem[] = [];
  for (const item of items) {
    const notes = item.owner_test_notes.trim();
    if (!item.client_test_notes_error && nothingToTestMarker(notes)) {
      nothingToTest.push(item);
      continue;
    }
    checks.push(item);
  }
  return { checks, nothingToTest };
}

// The reason after the "Nothing for the Client to test" marker, such as "CI tests only."
export function untestedReason(notes: string): string {
  const marker = nothingToTestMarker(notes) ?? "";
  return notes.trim().slice(marker.length).replace(/^[\s.:;,-]+/, "");
}
