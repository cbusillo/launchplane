import type { ReleaseReviewItem } from "./generated/openapi.ts";

// role-words: legacy marker that product pull requests write and Launchplane parses.
export const NOTHING_TO_TEST = "Nothing for the owner to test";

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
    if (notes.toLowerCase().startsWith(NOTHING_TO_TEST.toLowerCase())) {
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

// The reason after the "Nothing for the owner to test" marker, such as "CI tests only."
export function untestedReason(notes: string): string {
  return notes.trim().slice(NOTHING_TO_TEST.length).replace(/^[\s.:;,-]+/, "");
}
