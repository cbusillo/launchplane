import type { ProductPreviewSummary } from "./generated/openapi.ts";

export function previewInventoryPresentation(preview: ProductPreviewSummary) {
  if (!preview.enabled) {
    return { headline: "Not enabled", detail: "This product profile does not expose preview lifecycle capability." };
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
