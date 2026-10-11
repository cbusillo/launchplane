import type { ReleaseReviewItem } from "./generated/openapi.ts";

export const NOTHING_TO_TEST = "Nothing for the Client to test";
// role-words: legacy marker that pull requests written before the role words changed carry.
const LEGACY_NOTHING_TO_TEST = "Nothing for the owner to test";

function nothingToTestMarker(notes: string): string | undefined {
  const lowered = notes.trim().toLowerCase();
  return [NOTHING_TO_TEST, LEGACY_NOTHING_TO_TEST].find(marker => lowered.startsWith(marker.toLowerCase()));
}

export type ReleaseCheck = { notes: string; items: ReleaseReviewItem[] };

export type GroupedReleaseItems = {
  checks: ReleaseCheck[];
  nothingToTest: ReleaseReviewItem[];
};

// Display grouping only: the checklist, its digest, and the blockers stay per pull request.
export function groupReleaseItems(items: readonly ReleaseReviewItem[]): GroupedReleaseItems {
  const checks: ReleaseCheck[] = [];
  const byNotes = new Map<string, ReleaseCheck>();
  const nothingToTest: ReleaseReviewItem[] = [];
  for (const item of items) {
    const notes = item.owner_test_notes.trim();
    if (nothingToTestMarker(notes)) {
      nothingToTest.push(item);
      continue;
    }
    const existing = notes ? byNotes.get(notes) : undefined;
    if (existing) {
      existing.items.push(item);
      continue;
    }
    const check = { notes, items: [item] };
    checks.push(check);
    if (notes) byNotes.set(notes, check);
  }
  return { checks, nothingToTest };
}

// The reason after the "Nothing for the Client to test" marker, such as "CI tests only."
export function untestedReason(notes: string): string {
  const marker = nothingToTestMarker(notes) ?? "";
  return notes.trim().slice(marker.length).replace(/^[\s.:;,-]+/, "");
}
