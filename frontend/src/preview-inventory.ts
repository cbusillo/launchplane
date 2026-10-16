import type { ProductPreviewSummary } from "./generated/openapi.ts";

export function previewInventoryPresentation(preview: ProductPreviewSummary) {
  if (!preview.enabled) {
    return { headline: "Not applicable", detail: "This product profile does not expose preview lifecycle capability." };
  }
  if (preview.inventory) {
    const inventory = preview.inventory;
    const recorded = `${preview.active_count} recorded preview identities.`;
    const disagreements = [
      preview.unobserved_record_slugs?.length ? `${preview.unobserved_record_slugs.length} recorded preview(s) absent from scan.` : "",
      preview.untracked_observed_slugs?.length ? `${preview.untracked_observed_slugs.length} observed preview(s) without a current record.` : "",
    ].filter(Boolean).join(" ");
    if (preview.trust_state === "verified" && (inventory.status === "empty" || inventory.status === "nonempty")) {
      return { headline: inventory.observed_count ? `${inventory.observed_count} observed` : "No previews observed",
        detail: `Provider inventory verified. ${recorded} ${disagreements}`.trim() };
    }
    const headlines = { stale: "Inventory stale", failed: "Inventory scan failed", unsupported: "Inventory unsupported", unavailable: "Inventory unavailable", missing: "Inventory unknown" };
    return { headline: preview.trust_state === "stale" ? "Inventory stale" : headlines[inventory.status as keyof typeof headlines] ?? "Inventory unknown", detail: `${recorded} ${inventory.provenance.detail}` };
  }
  const recorded = `Recorded count: ${preview.active_count}.`;
  switch (preview.trust_state) {
    case "verified":
      return { headline: preview.active_count ? `${preview.active_count} active` : "No active previews",
        detail: "The preview inventory is verified." };
    case "recorded":
      return { headline: `${preview.active_count} recorded`,
        detail: "Current preview presence has not been verified." };
    case "stale":
      return { headline: "Inventory stale", detail: `${recorded} Current preview presence is unknown because the inventory evidence is stale.` };
    case "unsupported":
      return { headline: "Inventory unavailable", detail: `${recorded} Preview inventory evidence is unsupported.` };
    case "missing":
      return { headline: "Inventory unknown", detail: `Launchplane cannot determine whether previews exist without inventory evidence. ${recorded}` };
  }
}
