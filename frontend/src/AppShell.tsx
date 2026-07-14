import {
  Boxes,
  ChevronRight,
  CircleUserRound,
  LogOut,
  Moon,
  RefreshCw,
  Sun,
  Wrench,
} from "lucide-react";
import { useEffect, type ReactNode } from "react";

import type {
  GitHubHumanIdentityResponse,
  ProductSiteOverview,
} from "./generated/openapi.ts";
import type { ResourceState } from "./resource";
import {
  AppLink,
  engineeringPath,
  navigateTo,
  productIndexPath,
  productPath,
  routeHref,
  type AppRoute,
} from "./router";

type Theme = "dark" | "light";

export function AppShell({
  children,
  identity,
  notice,
  onDismissNotice,
  onLogout,
  onRefresh,
  onThemeChange,
  productsResource,
  route,
  selectedProduct,
  signingOut,
  theme,
}: {
  children: ReactNode;
  identity: GitHubHumanIdentityResponse;
  notice: string;
  onDismissNotice: () => void;
  onLogout: () => void;
  onRefresh: () => void;
  onThemeChange: (theme: Theme) => void;
  productsResource: ResourceState<ProductSiteOverview[]>;
  route: AppRoute;
  selectedProduct: ProductSiteOverview | null;
  signingOut: boolean;
  theme: Theme;
}) {
  const products = productsResource.data ?? [];
  const productArea =
    route.kind === "product-index" || route.kind === "product-workspace";
  const routeLabel =
    route.kind === "engineering"
      ? "Engineering Ops"
      : route.kind === "product-workspace"
        ? selectedProduct?.display_name ?? "Product workspace"
        : route.kind === "product-index"
          ? "Products"
          : "Page not found";

  useEffect(() => {
    document.title = `${routeLabel} · Launchplane`;
    const heading = document.querySelector<HTMLElement>("[data-route-heading]");
    heading?.focus({ preventScroll: true });
  }, [routeLabel, route.kind]);

  return (
    <div className="control-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <aside className="control-rail">
        <AppLink className="rail-brand" to={productIndexPath()} aria-label="Launchplane home">
          <span className="rail-brand-mark">
            <img
              alt=""
              src={`${import.meta.env.BASE_URL}assets/brand/launchplane-icon.svg`}
            />
          </span>
          <span>
            <strong>Launchplane</strong>
            <small>Operator control plane</small>
          </span>
        </AppLink>

        <nav className="ops-nav" aria-label="Operations workspaces">
          <p className="rail-label">Workspaces</p>
          <AppLink
            aria-label="Product Ops"
            aria-current={productArea ? "page" : undefined}
            className="ops-nav-link"
            data-active={productArea}
            to={productIndexPath()}
          >
            <Boxes size={17} aria-hidden="true" />
            <span>
              <strong>Product Ops</strong>
              <small>Products and environments</small>
            </span>
          </AppLink>
          <AppLink
            aria-label="Engineering Ops"
            aria-current={route.kind === "engineering" ? "page" : undefined}
            className="ops-nav-link"
            data-active={route.kind === "engineering"}
            to={engineeringPath()}
          >
            <Wrench size={17} aria-hidden="true" />
            <span>
              <strong>Engineering Ops</strong>
              <small>Platform delivery systems</small>
            </span>
          </AppLink>
        </nav>

        <section className="rail-products" aria-labelledby="rail-products-heading">
          <div className="rail-section-head">
            <p className="rail-label" id="rail-products-heading">
              Products
            </p>
            {products.length ? <span>{products.length}</span> : null}
          </div>
          {productsResource.status === "loading" && !products.length ? (
            <p className="rail-state">Loading product inventory…</p>
          ) : null}
          {productsResource.status === "error" && !products.length ? (
            <p className="rail-state rail-state-error">Product index unavailable</p>
          ) : null}
          {productsResource.status === "ready" && !products.length ? (
            <p className="rail-state">No products recorded</p>
          ) : null}
          {products.length ? (
            <ul className="rail-product-list">
              {products.map((product) => {
                const active =
                  route.kind === "product-workspace" && route.product === product.product;
                return (
                  <li key={product.product}>
                    <AppLink
                      aria-current={active ? "page" : undefined}
                      className="rail-product-link"
                      data-active={active}
                      to={productPath(product.product)}
                    >
                      <span>{product.display_name}</span>
                      <ProductTrustDots product={product} />
                    </AppLink>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </section>

        <div className="rail-identity">
          <span className="identity-avatar" aria-hidden="true">
            {identity.login.slice(0, 1).toUpperCase()}
          </span>
          <span>
            <strong>{identity.name || identity.login}</strong>
            <small>{identity.role === "admin" ? "Administrator" : "Read-only operator"}</small>
          </span>
        </div>
      </aside>

      <section className="control-stage">
        <header className="control-topbar">
          <div className="breadcrumb" aria-label="Breadcrumb">
            <span>{route.kind === "engineering" ? "Engineering Ops" : "Product Ops"}</span>
            {route.kind === "product-workspace" ? (
              <>
                <ChevronRight size={13} aria-hidden="true" />
                <span>{selectedProduct?.display_name ?? "Product workspace"}</span>
              </>
            ) : null}
          </div>

          <label className="mobile-product-picker">
            <span>Product</span>
            <select
              value={route.kind === "product-workspace" ? route.product : ""}
              onChange={(event) =>
                navigateTo(
                  event.target.value
                    ? productPath(event.target.value)
                    : productIndexPath(),
                )
              }
            >
              <option value="">All products</option>
              {products.map((product) => (
                <option key={product.product} value={product.product}>
                  {product.display_name}
                </option>
              ))}
            </select>
          </label>

          <div className="topbar-actions">
            <button
              aria-label="Refresh current evidence"
              className="icon-button"
              disabled={productsResource.status === "loading"}
              onClick={onRefresh}
              type="button"
            >
              <RefreshCw
                className={productsResource.status === "loading" ? "spin" : ""}
                size={16}
                aria-hidden="true"
              />
            </button>
            <button
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
              className="icon-button"
              onClick={() => onThemeChange(theme === "dark" ? "light" : "dark")}
              type="button"
            >
              {theme === "dark" ? (
                <Sun size={16} aria-hidden="true" />
              ) : (
                <Moon size={16} aria-hidden="true" />
              )}
            </button>
            <details className="account-menu">
              <summary aria-label="Open operator account menu">
                <CircleUserRound size={18} aria-hidden="true" />
                <span>{identity.login}</span>
              </summary>
              <div className="account-popover">
                <strong>{identity.name || identity.login}</strong>
                <span>{identity.email || "GitHub-authenticated operator"}</span>
                <span>{identity.role === "admin" ? "Administrator" : "Read-only operator"}</span>
                <button disabled={signingOut} onClick={onLogout} type="button">
                  <LogOut size={15} aria-hidden="true" />
                  {signingOut ? "Signing out…" : "Sign out"}
                </button>
              </div>
            </details>
          </div>
        </header>

        {notice ? (
          <div className="shell-notice" role="alert">
            <span>{notice}</span>
            <button onClick={onDismissNotice} type="button">
              Dismiss
            </button>
          </div>
        ) : null}

        <div className="route-announcer" aria-live="polite" aria-atomic="true">
          {routeLabel}
        </div>
        <main className="control-main" id="main-content">
          {children}
        </main>
      </section>
    </div>
  );
}

function ProductTrustDots({ product }: { product: ProductSiteOverview }) {
  const testing = product.environments.find(
    (environment) => environment.environment === "testing",
  );
  const production = product.environments.find(
    (environment) => environment.environment === "prod",
  );
  return (
    <span className="product-trust-dots" aria-label="Lane evidence states">
      <span
        data-lane="testing"
        data-trust={testing?.trust_state ?? "missing"}
        title={`Testing: ${testing?.trust_state ?? "missing"}`}
      />
      <span
        data-lane="prod"
        data-trust={production?.trust_state ?? "missing"}
        title={`Production: ${production?.trust_state ?? "missing"}`}
      />
    </span>
  );
}
