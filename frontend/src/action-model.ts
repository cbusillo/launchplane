import type { ProductActionAvailability } from "./generated/openapi.ts";
import type { BrowserActionKind } from "./browser-operation";
import { BROWSER_WRITE_ROUTES } from "./browser-write-contract";

export type BrowserActionSupport =
  | "implemented"
  | "diagnostic"
  | "planned"
  | "unsupported";

export interface BrowserActionPresentation {
  action: ProductActionAvailability;
  browserBlockers: string[];
  browserSupport: BrowserActionSupport;
  canExecute: boolean;
  kind: BrowserActionKind;
  serverBlockers: string[];
}

interface BrowserActionAdapter {
  actionId: string;
  blocker: string;
  browserRoutePath: string;
  diagnosticRoutePath: string;
  driverId: string;
  kind: BrowserActionKind;
  method: "POST";
  safety: "mutation";
  scope: "instance";
  support: BrowserActionSupport;
}

const BROWSER_ACTION_ADAPTERS: BrowserActionAdapter[] = [
  {
    actionId: "prod_promotion",
    blocker: "The product-owned promotion dry-run is unavailable.",
    browserRoutePath: BROWSER_WRITE_ROUTES.productPromotionDryRun,
    diagnosticRoutePath: "/v1/drivers/generic-web/prod-promotion",
    driverId: "generic-web",
    kind: "dry-run",
    method: "POST",
    safety: "mutation",
    scope: "instance",
    support: "diagnostic",
  },
  {
    actionId: "prod_promotion_workflow",
    blocker: "The product-owned promotion workflow control is unavailable.",
    browserRoutePath: BROWSER_WRITE_ROUTES.productPromotionWorkflowDispatch,
    diagnosticRoutePath: "/v1/drivers/generic-web/prod-promotion-workflow",
    driverId: "generic-web",
    kind: "workflow-dispatch",
    method: "POST",
    safety: "mutation",
    scope: "instance",
    support: "diagnostic",
  },
];

export const ACTION_KIND_LABELS: Record<BrowserActionKind, string> = {
  inspect: "Inspect",
  "dry-run": "Dry run",
  apply: "Apply",
  "workflow-dispatch": "Workflow dispatch",
  destructive: "Destructive",
  unsupported: "Unsupported",
};

export function browserActionPresentation(
  driverId: string,
  action: ProductActionAvailability,
): BrowserActionPresentation {
  const adapter = BROWSER_ACTION_ADAPTERS.find(
    (candidate) =>
      candidate.driverId === driverId && candidate.actionId === action.action_id,
  );
  const serverBlockers = [...action.disabled_reasons];
  const browserBlockers: string[] = [];
  if (!action.enabled && serverBlockers.length === 0) {
    browserBlockers.push(
      "Launchplane marked this action disabled without an explanatory reason.",
    );
  }
  if (!adapter) {
    browserBlockers.push(
      "No generated browser operation is registered for this action.",
    );
    return {
      action,
      browserBlockers: uniqueStrings(browserBlockers),
      browserSupport: "unsupported",
      canExecute: false,
      kind: fallbackActionKind(action),
      serverBlockers,
    };
  }
  if (
    adapter.diagnosticRoutePath !== action.route_path ||
    adapter.method !== action.method ||
    adapter.safety !== action.safety ||
    adapter.scope !== action.scope
  ) {
    browserBlockers.push(
      "The advertised method, route, safety, or scope does not match the typed browser operation contract.",
    );
    return {
      action,
      browserBlockers: uniqueStrings(browserBlockers),
      browserSupport: "unsupported",
      canExecute: false,
      kind: "unsupported",
      serverBlockers,
    };
  }
  if (!isTrustedAction(action)) {
    browserBlockers.push(
      `Action availability evidence is ${action.trust_state}; current recorded evidence is required.`,
    );
  }
  if (!adapter.browserRoutePath) {
    browserBlockers.push("The typed browser operation has no fixed product-owned route.");
  }
  if (adapter.support === "planned" || adapter.support === "unsupported") {
    browserBlockers.push(adapter.blocker);
  }
  const canExecute =
    adapter.support === "implemented" &&
    action.enabled &&
    serverBlockers.length === 0 &&
    browserBlockers.length === 0;
  return {
    action,
    browserBlockers: uniqueStrings(browserBlockers),
    browserSupport: adapter.support,
    canExecute,
    kind: adapter.kind,
    serverBlockers,
  };
}

export function compareBrowserActionPresentations(
  left: BrowserActionPresentation,
  right: BrowserActionPresentation,
): number {
  if (left.browserSupport !== right.browserSupport) {
    const supportOrder = {
      implemented: 0,
      diagnostic: 1,
      planned: 2,
      unsupported: 3,
    } as const;
    return supportOrder[left.browserSupport] - supportOrder[right.browserSupport];
  }
  if (left.action.enabled !== right.action.enabled) {
    return left.action.enabled ? -1 : 1;
  }
  return left.action.label.localeCompare(right.action.label);
}

function fallbackActionKind(action: ProductActionAvailability): BrowserActionKind {
  if (action.safety === "destructive") {
    return "destructive";
  }
  if (action.safety === "read" || action.method === "GET") {
    return "inspect";
  }
  return "unsupported";
}

function isTrustedAction(action: ProductActionAvailability): boolean {
  return action.trust_state === "verified" || action.trust_state === "recorded";
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}
