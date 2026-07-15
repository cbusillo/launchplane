import type { ProductSiteOverview } from "./generated/openapi.ts";
import {
  AppLink,
  productActivityPath,
  productEnvironmentPath,
  productPath,
  type AppRoute,
  type EnvironmentView,
} from "./router";

export function ProductWorkspaceNav({
  product,
  route,
}: {
  product: ProductSiteOverview;
  route: AppRoute;
}) {
  return (
    <nav className="workspace-nav" aria-label={`${product.display_name} workspace`}>
      <AppLink
        aria-current={route.kind === "product-workspace" ? "page" : undefined}
        data-active={route.kind === "product-workspace"}
        to={productPath(product.product)}
      >
        Overview
      </AppLink>
      {product.environments.map((environment) => {
        const active =
          route.kind === "product-environment" &&
          route.environment === environment.environment;
        return (
          <AppLink
            aria-current={active ? "page" : undefined}
            data-active={active}
            data-lane={environment.environment}
            key={environment.environment}
            to={productEnvironmentPath(product.product, environment.environment)}
          >
            {environmentLabel(environment.environment)}
          </AppLink>
        );
      })}
      <AppLink
        aria-current={route.kind === "product-activity" ? "page" : undefined}
        data-active={route.kind === "product-activity"}
        to={productActivityPath(product.product)}
      >
        Activity
      </AppLink>
    </nav>
  );
}

export function EnvironmentViewNav({
  environment,
  product,
  view,
}: {
  environment: string;
  product: string;
  view: EnvironmentView;
}) {
  const views: Array<{ label: string; view: EnvironmentView }> = [
    { label: "Environment", view: "overview" },
    { label: "Actions", view: "actions" },
    { label: "Runtime settings", view: "runtime-settings" },
    { label: "Managed secrets", view: "managed-secrets" },
    { label: "Diagnostics", view: "diagnostics" },
  ];
  return (
    <nav className="environment-view-nav" aria-label="Environment views">
      {views.map((item) => (
        <AppLink
          aria-current={view === item.view ? "page" : undefined}
          data-active={view === item.view}
          key={item.view}
          to={productEnvironmentPath(product, environment, item.view)}
        >
          {item.label}
        </AppLink>
      ))}
    </nav>
  );
}

export function environmentLabel(environment: string): string {
  if (environment === "prod") {
    return "Production";
  }
  const normalized = environment.replaceAll("_", " ").replaceAll("-", " ").trim();
  return normalized
    ? `${normalized.slice(0, 1).toUpperCase()}${normalized.slice(1)}`
    : "Environment";
}

export function environmentViewLabel(view: EnvironmentView): string {
  if (view === "actions") {
    return "Actions";
  }
  if (view === "runtime-settings") {
    return "Runtime settings";
  }
  if (view === "managed-secrets") {
    return "Managed secrets";
  }
  if (view === "diagnostics") {
    return "Diagnostics";
  }
  return "Overview";
}
