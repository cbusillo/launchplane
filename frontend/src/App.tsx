import { AlertTriangle, KeyRound, LoaderCircle, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AppShell } from "./AppShell";
import { ProductActivityRoute } from "./ActivityRoute";
import {
  loadDevFixtures,
  readDevFixtureMode,
} from "./dev-fixture-loader";
import { EngineeringOpsRoute } from "./EngineeringOps";
import { ProductEnvironmentRoute } from "./EnvironmentRoute";
import {
  LaunchplaneApiError,
  listProducts,
  logout,
  readAuthSession,
} from "./api";
import {
  ProductIndexRoute,
  ProductWorkspaceRoute,
} from "./ProductOps";
import {
  emptyResource,
  type ResourceState,
} from "./resource";
import {
  AppLink,
  navigateTo,
  productIndexPath,
  routeProductKey,
  useAppRoute,
} from "./router";

import type {
  GitHubHumanIdentityResponse,
  ProductSiteOverview,
} from "./generated/openapi.ts";

type Theme = "dark" | "light";
type AuthState =
  | { status: "checking"; identity: null; error: ""; traceId: "" }
  | { status: "signed_out"; identity: null; error: ""; traceId: "" }
  | {
      status: "signed_in";
      identity: GitHubHumanIdentityResponse;
      error: "";
      traceId: "";
    }
  | {
      status: "error";
      identity: null;
      error: string;
      traceId: string;
    };
const THEME_STORAGE_KEY = "launchplane.theme";

export function App() {
  const route = useAppRoute();
  const fixtureMode = readDevFixtureMode();
  const [theme, setTheme] = useState<Theme>(() =>
    window.sessionStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark",
  );
  const [authState, setAuthState] = useState<AuthState>({
    status: "checking",
    identity: null,
    error: "",
    traceId: "",
  });
  const [authRefreshToken, setAuthRefreshToken] = useState(0);
  const [productRefreshToken, setProductRefreshToken] = useState(0);
  const [productsResource, setProductsResource] = useState<
    ResourceState<ProductSiteOverview[]>
  >(emptyResource());
  const [signingOut, setSigningOut] = useState(false);
  const [sessionNotice, setSessionNotice] = useState("");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.sessionStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    const normalizedPath = window.location.pathname.replace(/\/+$/, "") || "/";
    if (normalizedPath === "/" || normalizedPath === "/ui") {
      navigateTo(productIndexPath(), true);
    }
  }, [route.kind]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setAuthState({
      status: "checking",
      identity: null,
      error: "",
      traceId: "",
    });
    setSessionNotice("");

    async function loadSession() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          if (active) {
            setAuthState({
              status: "signed_in",
              identity: fixtures.fixtureIdentity,
              error: "",
              traceId: "",
            });
          }
          return;
        }
        const payload = await readAuthSession(controller.signal);
        if (active) {
          setAuthState({
            status: "signed_in",
            identity: payload.identity,
            error: "",
            traceId: "",
          });
        }
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        if (error instanceof LaunchplaneApiError && error.statusCode === 401) {
          setAuthState({
            status: "signed_out",
            identity: null,
            error: "",
            traceId: "",
          });
          return;
        }
        setAuthState({
          status: "error",
          identity: null,
          error:
            error instanceof Error
              ? error.message
              : "Launchplane could not verify this browser session.",
          traceId: error instanceof LaunchplaneApiError ? error.traceId : "",
        });
      }
    }

    void loadSession();
    return () => {
      active = false;
      controller.abort();
    };
  }, [authRefreshToken, fixtureMode]);

  useEffect(() => {
    if (authState.status !== "signed_in" || route.kind === "engineering") {
      setProductsResource(emptyResource());
      return;
    }
    let active = true;
    const controller = new AbortController();
    setProductsResource((current) => ({
      ...current,
      status: "loading",
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadProducts() {
      try {
        const products = fixtureMode
          ? await loadDevFixtures().then((fixtures) =>
              fixtures.productsForFixture(fixtureMode),
            )
          : await listProducts(controller.signal).then((payload) => payload.products);
        if (active) {
          setProductsResource({
            status: "ready",
            data: [...products].sort((left, right) =>
              left.display_name.localeCompare(right.display_name),
            ),
            error: "",
            traceId: "",
            statusCode: 200,
          });
        }
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setProductsResource((current) => ({
          status: "error",
          data: current.data,
          error:
            error instanceof Error
              ? error.message
              : "Launchplane could not load the product inventory.",
          traceId: error instanceof LaunchplaneApiError ? error.traceId : "",
          statusCode: error instanceof LaunchplaneApiError ? error.statusCode : 503,
        }));
      }
    }

    void loadProducts();
    return () => {
      active = false;
      controller.abort();
    };
  }, [authState.status, fixtureMode, productRefreshToken, route.kind]);

  const refreshProducts = useCallback(() => {
    setProductRefreshToken((current) => current + 1);
  }, []);

  const signOut = useCallback(async () => {
    setSigningOut(true);
    setSessionNotice("");
    try {
      if (!fixtureMode) {
        await logout();
      }
      setAuthState({
        status: "signed_out",
        identity: null,
        error: "",
        traceId: "",
      });
      setProductsResource(emptyResource());
      navigateTo(productIndexPath(), true);
    } catch (error) {
      setSessionNotice(
        error instanceof Error
          ? `Sign out failed: ${error.message}`
          : "Sign out failed. The browser session may still be active.",
      );
    } finally {
      setSigningOut(false);
    }
  }, [fixtureMode]);

  const selectedProduct = useMemo(() => {
    const productKey = routeProductKey(route);
    if (!productKey || !productsResource.data) {
      return null;
    }
    return (
      productsResource.data.find((product) => product.product === productKey) ?? null
    );
  }, [productsResource.data, route]);

  if (authState.status !== "signed_in") {
    return (
      <SessionGate
        state={authState}
        onRetry={() => setAuthRefreshToken((current) => current + 1)}
      />
    );
  }

  return (
    <AppShell
      identity={authState.identity}
      notice={sessionNotice}
      onDismissNotice={() => setSessionNotice("")}
      onLogout={() => void signOut()}
      onRefresh={refreshProducts}
      onThemeChange={setTheme}
      productsResource={productsResource}
      route={route}
      selectedProduct={selectedProduct}
      signingOut={signingOut}
      theme={theme}
    >
      {route.kind === "product-index" ? (
        <ProductIndexRoute resource={productsResource} onRetry={refreshProducts} />
      ) : null}
      {route.kind === "product-workspace" ? (
        <ProductWorkspaceRoute
          fixtureResource={fixtureMode ? productsResource : null}
          key={`workspace:${route.product}:${fixtureMode}`}
          productKey={route.product}
          refreshToken={productRefreshToken}
        />
      ) : null}
      {route.kind === "product-environment" ? (
        <ProductEnvironmentRoute
          environmentKey={route.environment}
          fixtureMode={fixtureMode}
          key={`environment:${route.product}:${route.environment}:${fixtureMode}`}
          productKey={route.product}
          productOverview={selectedProduct}
          refreshToken={productRefreshToken}
          view={route.view}
        />
      ) : null}
      {route.kind === "product-activity" ? (
        <ProductActivityRoute
          fixtureMode={fixtureMode}
          key={`activity:${route.product}:${fixtureMode}`}
          productKey={route.product}
          productOverview={selectedProduct}
          refreshToken={productRefreshToken}
        />
      ) : null}
      {route.kind === "engineering" ? (
        <EngineeringOpsRoute
          fixtureMode={fixtureMode}
          key={`engineering:${route.view}:${fixtureMode}`}
          view={route.view}
        />
      ) : null}
      {route.kind === "not-found" ? <NotFoundRoute /> : null}
    </AppShell>
  );
}

function SessionGate({
  state,
  onRetry,
}: {
  state: Exclude<AuthState, { status: "signed_in" }>;
  onRetry: () => void;
}) {
  const returnTo = `${window.location.pathname}${window.location.search}`;
  const safeReturnTo = returnTo.startsWith("/ui") ? returnTo : productIndexPath();
  const loginHref = `/auth/github/login?return_to=${encodeURIComponent(safeReturnTo)}`;
  const checking = state.status === "checking";
  const failed = state.status === "error";

  useEffect(() => {
    if (state.status === "checking") {
      return;
    }
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
  }, [state.status]);

  return (
    <main className="session-screen">
      <section className="session-card" aria-live="polite">
        <div className="session-brand" aria-label="Launchplane">
          <img
            alt=""
            src={`${import.meta.env.BASE_URL}assets/brand/launchplane-icon.svg`}
          />
          <span>Launchplane</span>
        </div>
        <div className="session-icon" data-state={failed ? "error" : "secure"}>
          {checking ? (
            <LoaderCircle className="spin" aria-hidden="true" />
          ) : failed ? (
            <AlertTriangle aria-hidden="true" />
          ) : (
            <ShieldCheck aria-hidden="true" />
          )}
        </div>
        <p className="eyebrow">Operator access</p>
        <h1 data-route-heading tabIndex={-1}>
          {checking
            ? "Verifying your session"
            : failed
              ? "Session verification failed"
              : "Sign in to operate products"}
        </h1>
        <p className="session-copy">
          {checking
            ? "Launchplane is checking the browser session before loading operational evidence."
            : failed
              ? state.error
              : "GitHub authentication protects product evidence and operator actions."}
        </p>
        {failed && state.traceId ? <code className="trace-id">{state.traceId}</code> : null}
        {failed ? (
          <button className="button button-primary" type="button" onClick={onRetry}>
            Retry session check
          </button>
        ) : checking ? null : (
          <a className="button button-primary" href={loginHref}>
            <KeyRound size={16} aria-hidden="true" />
            Sign in with GitHub
          </a>
        )}
        <p className="session-footnote">
          Operational data is loaded only after the service confirms this session.
        </p>
      </section>
    </main>
  );
}

function NotFoundRoute() {
  return (
    <section className="state-page">
      <p className="eyebrow">Navigation</p>
      <h1 data-route-heading tabIndex={-1}>
        This Launchplane view does not exist
      </h1>
      <p>
        The URL is not part of the product or engineering operations workspace.
      </p>
      <AppLink className="button button-primary" to={productIndexPath()}>
        Return to Product Ops
      </AppLink>
    </section>
  );
}
