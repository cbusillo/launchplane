export type AppRoute =
  | { kind: "product-index" }
  | { kind: "product-workspace"; product: string }
  | { kind: "product-activity"; product: string }
  | {
      kind: "product-environment";
      product: string;
      environment: string;
      view: EnvironmentView;
    }
  | { kind: "engineering"; view: EngineeringView }
  | { kind: "not-found"; path: string };

export type EnvironmentView =
  | "overview"
  | "actions"
  | "runtime-settings"
  | "managed-secrets"
  | "diagnostics";

export type EngineeringView =
  | "hub"
  | "work-graph"
  | "issue-inbox"
  | "every-code"
  | "merge-train"
  | "tenant-admission"
  | "governance-projection"
  | "owner-acceptance"
  | "privileged-operations";

export function productIndexPath(): string {
  return "/ui/products";
}

export function productPath(product: string): string {
  return `${productIndexPath()}/${encodeURIComponent(product)}`;
}

export function productActivityPath(product: string): string {
  return `${productPath(product)}/activity`;
}

export function productEnvironmentPath(
  product: string,
  environment: string,
  view: EnvironmentView = "overview",
): string {
  const basePath = `${productPath(product)}/environments/${encodeURIComponent(environment)}`;
  return view === "overview" ? basePath : `${basePath}/${view}`;
}

export function engineeringPath(view: EngineeringView = "hub"): string {
  const basePath = "/ui/engineering";
  return view === "hub" ? basePath : `${basePath}/${view}`;
}

export function engineeringViewLabel(view: EngineeringView): string {
  if (view === "work-graph") {
    return "Work graph";
  }
  if (view === "issue-inbox") {
    return "Issue inbox";
  }
  if (view === "every-code") {
    return "Every Code";
  }
  if (view === "merge-train") {
    return "Merge train";
  }
  if (view === "tenant-admission") {
    return "Tenant admission";
  }
  if (view === "governance-projection") {
    return "Governance evidence";
  }
  if (view === "owner-acceptance") {
    return "Owner product review";
  }
  if (view === "privileged-operations") {
    return "Privileged operation plans";
  }
  return "Engineering Ops";
}

export function parseAppRoute(pathname: string): AppRoute {
  const normalizedPath = pathname.replace(/\/+$/, "") || "/";
  if (
    normalizedPath === "/" ||
    normalizedPath === "/ui" ||
    normalizedPath === productIndexPath()
  ) {
    return { kind: "product-index" };
  }
  if (normalizedPath === engineeringPath()) {
    return { kind: "engineering", view: "hub" };
  }
  const engineeringPrefix = `${engineeringPath()}/`;
  if (normalizedPath.startsWith(engineeringPrefix)) {
    const routeSegments = normalizedPath.slice(engineeringPrefix.length).split("/");
    const view = routeSegments[0] as EngineeringView;
    if (
      routeSegments.length === 1 &&
      [
        "work-graph",
        "issue-inbox",
        "every-code",
        "merge-train",
        "tenant-admission",
        "governance-projection",
        "owner-acceptance",
        "privileged-operations",
      ].includes(view)
    ) {
      return { kind: "engineering", view };
    }
    return { kind: "not-found", path: pathname };
  }
  const productPrefix = `${productIndexPath()}/`;
  if (normalizedPath.startsWith(productPrefix)) {
    const routeSegments = normalizedPath.slice(productPrefix.length).split("/");
    const product = decodeRouteSegment(routeSegments[0]);
    if (!product) {
      return { kind: "not-found", path: pathname };
    }
    if (routeSegments.length === 1) {
      return { kind: "product-workspace", product };
    }
    if (routeSegments.length === 2 && routeSegments[1] === "activity") {
      return { kind: "product-activity", product };
    }
    if (routeSegments[1] === "environments") {
      const environment = decodeRouteSegment(routeSegments[2] ?? "");
      if (!environment) {
        return { kind: "not-found", path: pathname };
      }
      if (routeSegments.length === 3) {
        return {
          kind: "product-environment",
          product,
          environment,
          view: "overview",
        };
      }
      const view = routeSegments[3] as EnvironmentView;
      if (
        routeSegments.length === 4 &&
        [
          "overview",
          "actions",
          "runtime-settings",
          "managed-secrets",
          "diagnostics",
        ].includes(view)
      ) {
        return { kind: "product-environment", product, environment, view };
      }
    }
  }
  return { kind: "not-found", path: pathname };
}

export function ownerAcceptanceLookupFromSearch(search: string): {
  repository: string;
  pullRequest: string;
  requested: boolean;
  valid: boolean;
} {
  const searchParams = new URLSearchParams(search);
  const repository = searchParams.get("repository")?.trim() ?? "";
  const pullRequest = searchParams.get("pull_request")?.trim() ?? "";
  return {
    repository,
    pullRequest,
    requested: Boolean(repository || pullRequest),
    valid:
      /^[^/\s]+\/[^/\s]+$/.test(repository) && /^[1-9][0-9]*$/.test(pullRequest),
  };
}

export function routeProductKey(route: AppRoute): string {
  return route.kind === "product-workspace" ||
    route.kind === "product-activity" ||
    route.kind === "product-environment"
    ? route.product
    : "";
}

function decodeRouteSegment(value: string): string {
  if (!value) {
    return "";
  }
  try {
    const decoded = decodeURIComponent(value).trim();
    return decoded && !decoded.includes("/") ? decoded : "";
  } catch {
    return "";
  }
}
