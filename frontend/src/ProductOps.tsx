import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  ExternalLink,
  PackageOpen,
  Radar,
  RefreshCw,
  Search,
  ShieldQuestion,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { LaunchplaneApiError, readProduct } from "./api";
import { formatTime } from "./format";
import {
  emptyResource,
  type ResourceState,
} from "./resource";
import { AppLink, productPath } from "./router";

import type {
  DataProvenance,
  ProductEnvironmentSummary,
  ProductSiteOverview,
  ProductTopologyWarning,
} from "./generated/openapi.ts";

type TrustState = ProductSiteOverview["trust_state"];
type SignalTone = TrustState | "warning" | "danger";

interface WarningItem {
  id: string;
  scope: string;
  detail: string;
  severity: "warning" | "error";
}

interface InspectionStep {
  title: string;
  detail: string;
  targetId: string;
}

export function ProductIndexRoute({
  resource,
  onRetry,
}: {
  resource: ResourceState<ProductSiteOverview[]>;
  onRetry: () => void;
}) {
  const products = resource.data ?? [];
  const loading = resource.status === "idle" || resource.status === "loading";

  if (loading && !products.length) {
    return <ProductIndexSkeleton />;
  }
  if (resource.status === "error" && !products.length) {
    const accessDenied = resource.statusCode === 401 || resource.statusCode === 403;
    return (
      <RouteError
        eyebrow="Product Ops"
        message={resource.error}
        onRetry={accessDenied ? undefined : onRetry}
        returnTo={accessDenied ? null : undefined}
        title={accessDenied ? "Product inventory access denied" : "Product inventory unavailable"}
        traceId={resource.traceId}
      />
    );
  }
  if (resource.status === "ready" && !products.length) {
    return <NoProducts onRetry={onRetry} />;
  }

  return (
    <section className="product-index" aria-busy={resource.status === "loading"}>
      <div className="route-heading route-heading-compact">
        <div>
          <p className="eyebrow">Product Ops</p>
          <h1 data-route-heading tabIndex={-1}>
            Products
          </h1>
          <p>
            Choose a product workspace to compare stable lanes, preview inventory,
            warnings, and the next safe inspection.
          </p>
        </div>
        <div className="inventory-count" aria-label={`${products.length} products`}>
          <strong>{products.length}</strong>
          <span>{products.length === 1 ? "product" : "products"}</span>
        </div>
      </div>

      {resource.status === "error" ? (
        <InlineError
          message={`${resource.error} Showing the last product inventory received by this browser.`}
          traceId={resource.traceId}
        />
      ) : null}

      <div className="directory-head" aria-hidden="true">
        <span>Product</span>
        <span>Testing</span>
        <span>Production</span>
        <span>Previews</span>
        <span>Warnings</span>
        <span>Next safe action</span>
      </div>
      <ul className="product-directory" aria-label="Launchplane products">
        {products.map((product) => (
          <ProductDirectoryRow key={product.product} product={product} />
        ))}
      </ul>
    </section>
  );
}

export function ProductWorkspaceRoute({
  fixtureResource,
  productKey,
  refreshToken,
}: {
  fixtureResource: ResourceState<ProductSiteOverview[]> | null;
  productKey: string;
  refreshToken: number;
}) {
  const [retryToken, setRetryToken] = useState(0);
  const [resource, setResource] = useState<ResourceState<ProductSiteOverview>>(
    emptyResource(),
  );
  const loadedProductKey = useRef("");

  useEffect(() => {
    if (fixtureResource) {
      const product =
        fixtureResource.data?.find((candidate) => candidate.product === productKey) ?? null;
      const missingProduct = fixtureResource.status === "ready" && !product;
      setResource({
        status: missingProduct ? "error" : fixtureResource.status,
        data: product,
        error: missingProduct
          ? "Launchplane has no product with this route key."
          : fixtureResource.error,
        traceId: fixtureResource.traceId,
        statusCode: missingProduct ? 404 : fixtureResource.statusCode,
      });
      return;
    }

    let active = true;
    const controller = new AbortController();
    const sameProduct = loadedProductKey.current === productKey;
    setResource((current) => ({
      ...current,
      status: "loading",
      data: sameProduct ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));
    readProduct(productKey, controller.signal)
      .then((payload) => {
        if (active) {
          loadedProductKey.current = productKey;
          setResource({
            status: "ready",
            data: payload.product,
            error: "",
            traceId: payload.trace_id,
            statusCode: 200,
          });
        }
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) {
          return;
        }
        setResource((current) => ({
          status: "error",
          data: current.data,
          error:
            error instanceof Error
              ? error.message
              : "Launchplane could not load this product workspace.",
          traceId: error instanceof LaunchplaneApiError ? error.traceId : "",
          statusCode: error instanceof LaunchplaneApiError ? error.statusCode : 503,
        }));
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    fixtureResource,
    productKey,
    refreshToken,
    retryToken,
  ]);

  if ((resource.status === "idle" || resource.status === "loading") && !resource.data) {
    return <ProductWorkspaceSkeleton />;
  }
  if (resource.status === "error" && !resource.data) {
    const notFound = resource.statusCode === 404;
    const accessDenied = resource.statusCode === 401 || resource.statusCode === 403;
    return (
      <RouteError
        eyebrow="Product Ops"
        message={resource.error}
        onRetry={
          notFound || accessDenied
            ? undefined
            : () => setRetryToken((current) => current + 1)
        }
        title={
          notFound
            ? "Product workspace not found"
            : accessDenied
              ? "Product workspace access denied"
              : "Product workspace unavailable"
        }
        traceId={resource.traceId}
      />
    );
  }
  if (!resource.data) {
    return null;
  }

  return (
    <ProductWorkspace
      product={resource.data}
      refreshError={resource.status === "error" ? resource.error : ""}
      traceId={resource.status === "error" ? resource.traceId : ""}
      updating={resource.status === "loading"}
    />
  );
}

function ProductDirectoryRow({ product }: { product: ProductSiteOverview }) {
  const testing = environmentByName(product, "testing");
  const production = environmentByName(product, "prod");
  const warnings = warningsForProduct(product);

  return (
    <li>
      <AppLink className="product-directory-row" to={productPath(product.product)}>
        <span className="directory-product">
          <span className="product-glyph" aria-hidden="true">
            {product.display_name.slice(0, 1).toUpperCase()}
          </span>
          <span>
            <strong>{product.display_name}</strong>
            <small>{product.repository || "Owning repository not recorded"}</small>
            <span className="directory-mobile-action">Inspect workspace · read-only</span>
          </span>
          <EvidenceBadge
            state={product.trust_state}
            timestamp={evidenceTimestamp(product.provenance)}
          />
        </span>
        <DirectoryLane environment={testing} lane="testing" />
        <DirectoryLane environment={production} lane="prod" />
        <span className="directory-preview">
          <span className="lane-name">Previews</span>
          <strong>
            {product.preview.enabled
              ? `${product.preview.active_count} active`
              : "Not enabled"}
          </strong>
          <EvidenceBadge compact state={product.preview.trust_state} />
        </span>
        <span className="directory-warnings" data-has-warnings={warnings.length > 0}>
          {warnings.length ? <AlertTriangle size={15} aria-hidden="true" /> : null}
          <strong>{warnings.length}</strong>
          <small>{warnings.length === 1 ? "warning" : "warnings"}</small>
        </span>
        <span className="directory-action">
          <span>
            <strong>Inspect workspace</strong>
            <small>Read-only</small>
          </span>
          <ArrowRight size={16} aria-hidden="true" />
        </span>
      </AppLink>
    </li>
  );
}

function DirectoryLane({
  environment,
  lane,
}: {
  environment: ProductEnvironmentSummary | null;
  lane: "testing" | "prod";
}) {
  const tone = environmentOperationalTone(environment);
  const headline = environment ? ingressHeadline(environment) : "Not recorded";
  return (
    <span className="directory-lane" data-lane={lane} data-operational={tone}>
      <span className="lane-name">{lane === "prod" ? "Production" : "Testing"}</span>
      <strong>
        {tone === "danger" ? "Issue · " : tone === "warning" ? "Warning · " : ""}
        {headline}
      </strong>
      <EvidenceBadge compact state={environment?.trust_state ?? "missing"} />
    </span>
  );
}

function ProductWorkspace({
  product,
  refreshError,
  traceId,
  updating,
}: {
  product: ProductSiteOverview;
  refreshError: string;
  traceId: string;
  updating: boolean;
}) {
  const environments = useMemo(
    () => [...product.environments].sort(compareEnvironments),
    [product.environments],
  );
  const warnings = useMemo(() => warningsForProduct(product), [product]);
  const inspection = useMemo(() => nextSafeInspection(product, warnings), [product, warnings]);
  const testing = environmentByName(product, "testing");
  const production = environmentByName(product, "prod");
  const incompleteEvidence =
    product.trust_state === "missing" ||
    product.trust_state === "stale" ||
    environments.length === 0 ||
    environments.some(
      (environment) =>
        environment.trust_state === "missing" || environment.trust_state === "stale",
    );

  useEffect(() => {
    document.title = `${product.display_name} · Launchplane`;
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
  }, [product.display_name]);

  return (
    <article className="product-workspace" aria-busy={updating}>
      {refreshError ? (
        <InlineError
          message={`${refreshError} Showing the last product evidence received by this browser.`}
          traceId={traceId}
        />
      ) : null}

      <header className="workspace-hero">
        <div className="workspace-title">
          <p className="eyebrow">Product workspace</p>
          <div className="workspace-title-line">
            <h1 data-route-heading tabIndex={-1}>
              {product.display_name}
            </h1>
            <EvidenceBadge
              state={product.trust_state}
              timestamp={evidenceTimestamp(product.provenance)}
            />
          </div>
          <p>
            Stable environments, preview inventory, operational warnings, and the
            next safe inspection for this product.
          </p>
          <span className="workspace-repository">
            Owning repository · {product.repository || "not recorded"}
          </span>
        </div>
        {updating ? (
          <span className="refresh-state">
            <RefreshCw className="spin" size={14} aria-hidden="true" />
            Refreshing evidence
          </span>
        ) : null}
      </header>

      <a className="mobile-next-inspection" href={`#${inspection.targetId}`}>
        <span>
          <small>Next safe action · Inspect</small>
          <strong>{inspection.title}</strong>
        </span>
        <ArrowRight size={17} aria-hidden="true" />
      </a>

      {incompleteEvidence ? (
        <div className="evidence-warning" role="status">
          <ShieldQuestion size={19} aria-hidden="true" />
          <div>
            <strong>Operational evidence is incomplete</strong>
            <p>
              Launchplane shows only recorded product state. Missing or stale lane
              evidence is not replaced with inferred health.
            </p>
          </div>
        </div>
      ) : null}

      <section className="workspace-signal-strip" aria-label="Product summary">
        <SignalTile
          tone={environmentOperationalTone(testing)}
          label="Testing"
          state={testing?.trust_state ?? "missing"}
          value={signalHeadline(testing)}
        />
        <SignalTile
          tone={environmentOperationalTone(production)}
          label="Production"
          state={production?.trust_state ?? "missing"}
          value={signalHeadline(production)}
        />
        <SignalTile
          label="Previews"
          state={product.preview.trust_state}
          value={
            product.preview.enabled
              ? `${product.preview.active_count} active`
              : "Not enabled"
          }
        />
        <SignalTile
          label="Warnings"
          state={product.trust_state}
          tone={warnings.length ? "warning" : product.trust_state}
          value={`${warnings.length} recorded`}
          detail={warnings.length ? "Needs review" : trustLabel(product.trust_state)}
        />
      </section>

      <div className="workspace-primary-grid">
        <section className="lane-board" id="stable-lanes" aria-labelledby="lane-board-title">
          <div className="section-title-row">
            <div>
              <p className="eyebrow">Stable environments</p>
              <h2 id="lane-board-title">Testing and production</h2>
            </div>
            <span>{environments.length} recorded</span>
          </div>
          {environments.length ? (
            <div className="lane-list">
              {environments.map((environment) => (
                <EnvironmentRow key={environment.environment} environment={environment} />
              ))}
            </div>
          ) : (
            <MissingEvidenceState
              detail="The product profile is present, but no stable environment summaries were returned."
              title="No stable environment evidence"
            />
          )}
        </section>

        <aside className="next-inspection" aria-labelledby="next-inspection-title">
          <div className="inspection-icon" aria-hidden="true">
            <Search />
          </div>
          <p className="eyebrow">Next safe action</p>
          <h2 id="next-inspection-title">{inspection.title}</h2>
          <p>{inspection.detail}</p>
          <span className="inspection-classification">Inspect · read-only</span>
          <a className="button button-primary" href={`#${inspection.targetId}`}>
            Review evidence
            <ArrowRight size={15} aria-hidden="true" />
          </a>
          <small>
            Launchplane does not expose a mutation control here until the operation
            has a supported execution path and explicit blockers.
          </small>
        </aside>
      </div>

      <div className="workspace-secondary-grid">
        <PreviewSummary product={product} />
        <WarningSummary product={product} warnings={warnings} />
      </div>

      <ProductDiagnostics product={product} />
    </article>
  );
}

function EnvironmentRow({ environment }: { environment: ProductEnvironmentSummary }) {
  const domain = domainForEnvironment(environment);
  const externalUrl = safeExternalUrl(environment.base_url);
  const tls = environment.topology.observed.tls_domains.find(
    (candidate) => candidate.role === "primary",
  ) ?? environment.topology.observed.tls_domains[0];
  const topologyWarnings = environment.topology.warnings;

  return (
    <article
      className="environment-row"
      data-lane={environment.environment}
      id={`lane-${safeAnchor(environment.environment)}`}
    >
      <div className="environment-identity">
        <span className="lane-marker" aria-hidden="true" />
        <div>
          <span className="lane-overline">Stable lane</span>
          <h3>{environmentLabel(environment.environment)}</h3>
        </div>
        <EvidenceBadge
          compact
          state={environment.trust_state}
          timestamp={evidenceTimestamp(environment.provenance)}
        />
      </div>
      <dl className="environment-facts">
        <div>
          <dt>Public endpoint</dt>
          <dd>
            {externalUrl ? (
              <a href={externalUrl.href} rel="noreferrer" target="_blank">
                {domain}
                <ExternalLink size={13} aria-hidden="true" />
              </a>
            ) : (
              domain
            )}
          </dd>
          <small>{ingressHeadline(environment)}</small>
        </div>
        <div>
          <dt>TLS evidence</dt>
          <dd>{tls ? humanize(tls.status) : "Not recorded"}</dd>
          <small>
            {tls?.summary || tls?.likely_failure_cause || "No TLS observation returned"}
          </small>
        </div>
        <div>
          <dt>Topology warnings</dt>
          <dd>{topologyWarnings.length}</dd>
          <small>
            {topologyWarnings.some((warning) => warning.severity === "error")
              ? "Action required"
              : topologyWarnings.length
                ? "Review recommended"
                : "None recorded"}
          </small>
        </div>
        <div>
          <dt>Evidence updated</dt>
          <dd>{formatEvidenceTime(environment.provenance)}</dd>
          <small>{trustLabel(environment.trust_state)}</small>
        </div>
      </dl>
    </article>
  );
}

function PreviewSummary({ product }: { product: ProductSiteOverview }) {
  const preview = product.preview;
  const title = !preview.enabled
    ? "Preview environments are not enabled"
    : preview.trust_state === "missing"
      ? "Preview inventory evidence is missing"
      : preview.active_count
        ? `${preview.active_count} active preview${preview.active_count === 1 ? "" : "s"}`
        : "No active previews";
  const detail = !preview.enabled
    ? "This product profile does not expose preview lifecycle capability."
    : preview.trust_state === "missing"
      ? "Launchplane cannot determine whether previews exist without inventory evidence."
      : preview.active_count
        ? "Active preview inventory is recorded under this product workspace."
        : "The inventory read completed without an active preview environment.";

  return (
    <section className="summary-panel preview-summary" id="preview-summary">
      <div className="summary-panel-icon" aria-hidden="true">
        <Radar />
      </div>
      <div>
        <p className="eyebrow">Preview inventory</p>
        <h2>{title}</h2>
        <p>{detail}</p>
      </div>
      <EvidenceBadge
        state={preview.trust_state}
        timestamp={evidenceTimestamp(preview.provenance)}
      />
    </section>
  );
}

function WarningSummary({
  product,
  warnings,
}: {
  product: ProductSiteOverview;
  warnings: WarningItem[];
}) {
  const trustworthyEmpty =
    (product.trust_state === "verified" || product.trust_state === "recorded") &&
    product.environments.every(
      (environment) =>
        environment.trust_state === "verified" || environment.trust_state === "recorded",
    );

  return (
    <section className="summary-panel warning-summary" id="warning-summary">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Warnings</p>
          <h2>{warnings.length ? `${warnings.length} need review` : "No warnings listed"}</h2>
        </div>
        <EvidenceBadge compact state={product.trust_state} />
      </div>
      {warnings.length ? (
        <ul className="warning-list">
          {warnings.slice(0, 6).map((warning) => (
            <li data-severity={warning.severity} key={warning.id}>
              {warning.severity === "error" ? (
                <AlertCircle size={16} aria-hidden="true" />
              ) : (
                <AlertTriangle size={16} aria-hidden="true" />
              )}
              <span>
                <strong>{warning.scope}</strong>
                <small>{warning.detail}</small>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="warning-empty">
          {trustworthyEmpty ? (
            <CheckCircle2 size={18} aria-hidden="true" />
          ) : (
            <ShieldQuestion size={18} aria-hidden="true" />
          )}
          <p>
            {trustworthyEmpty
              ? "Launchplane evidence currently contains no product or topology warnings."
              : "Warning evidence is incomplete, so an empty list is not proof of healthy operation."}
          </p>
        </div>
      )}
    </section>
  );
}

function ProductDiagnostics({ product }: { product: ProductSiteOverview }) {
  const actions = [
    ...product.available_actions.map((action) => ({ ...action, environment: "product" })),
    ...product.environments.flatMap((environment) =>
      environment.available_actions.map((action) => ({
        ...action,
        environment: environment.environment,
      })),
    ),
  ];

  return (
    <details className="diagnostics-panel" id="product-diagnostics">
      <summary>
        <span>
          <strong>Diagnostics</strong>
          <small>Raw routing, driver, provider, and action metadata</small>
        </span>
        <span>{product.environments.length} lanes</span>
      </summary>
      <div className="diagnostics-content">
        <dl>
          <div>
            <dt>Product key</dt>
            <dd>{product.product}</dd>
          </div>
          <div>
            <dt>Driver</dt>
            <dd>{product.driver_id}</dd>
          </div>
          <div>
            <dt>Base driver</dt>
            <dd>{product.base_driver_id || "none"}</dd>
          </div>
          <div>
            <dt>Repository</dt>
            <dd>{product.repository || "missing"}</dd>
          </div>
        </dl>
        {product.environments.map((environment) => (
          <section key={environment.environment}>
            <h3>{environmentLabel(environment.environment)}</h3>
            <dl>
              <div>
                <dt>Context</dt>
                <dd>{environment.context || "missing"}</dd>
              </div>
              <div>
                <dt>Provider</dt>
                <dd>{environment.topology.provider_recorded.placement.provider || "missing"}</dd>
              </div>
              <div>
                <dt>Target</dt>
                <dd>{environment.topology.provider_recorded.placement.target_name || "missing"}</dd>
              </div>
              <div>
                <dt>Ingress path</dt>
                <dd>{environment.topology.provider_recorded.ingress.path}</dd>
              </div>
            </dl>
          </section>
        ))}
        {actions.length ? (
          <section>
            <h3>Advertised action routes</h3>
            <ul className="diagnostic-action-list">
              {actions.map((action) => (
                <li key={`${action.environment}:${action.action_id}:${action.route_path}`}>
                  <span>{action.environment}</span>
                  <strong>{action.label}</strong>
                  <code>{action.route_path}</code>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </details>
  );
}

function SignalTile({
  detail,
  label,
  state,
  tone = state,
  value,
}: {
  detail?: string;
  label: string;
  state: TrustState;
  tone?: SignalTone;
  value: string;
}) {
  return (
    <div className="signal-tile" data-state={state} data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail ?? trustLabel(state)}</small>
    </div>
  );
}

function EvidenceBadge({
  compact = false,
  state,
  timestamp = "",
}: {
  compact?: boolean;
  state: TrustState;
  timestamp?: string;
}) {
  return (
    <span
      className="evidence-badge"
      data-compact={compact}
      data-state={state}
      title={timestamp ? `${trustLabel(state)} · ${formatTime(timestamp)}` : trustLabel(state)}
    >
      <span aria-hidden="true" />
      <strong>{trustLabel(state)}</strong>
      {!compact && timestamp ? <small>{formatTime(timestamp)}</small> : null}
    </span>
  );
}

function ProductIndexSkeleton() {
  return (
    <section className="product-index" aria-busy="true" aria-live="polite">
      <div className="route-heading route-heading-compact">
        <div>
          <p className="eyebrow">Product Ops</p>
          <h1 data-route-heading tabIndex={-1}>
            Loading products
          </h1>
          <p>Launchplane is reading product and stable environment summaries.</p>
        </div>
      </div>
      <div className="skeleton-directory">
        {Array.from({ length: 4 }, (_, index) => (
          <span key={index} />
        ))}
      </div>
    </section>
  );
}

function ProductWorkspaceSkeleton() {
  return (
    <section className="workspace-skeleton" aria-busy="true" aria-live="polite">
      <p className="eyebrow">Product workspace</p>
      <h1 data-route-heading tabIndex={-1}>
        Loading product evidence
      </h1>
      <div className="skeleton-hero" />
      <div className="skeleton-lanes">
        <span />
        <span />
      </div>
    </section>
  );
}

function NoProducts({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="state-page state-page-onboarding">
      <span className="state-page-icon" aria-hidden="true">
        <PackageOpen />
      </span>
      <p className="eyebrow">First run</p>
      <h1 data-route-heading tabIndex={-1}>
        No products are owned by Launchplane yet
      </h1>
      <p>
        Use the supported Product Onboarding workflow to record a product profile,
        stable lanes, runtime target authority, and required grants. This browser
        does not invent a sample product or infer provider state.
      </p>
      <ol className="onboarding-checklist">
        <li>Record the product profile and display name.</li>
        <li>Record testing and production lane authority.</li>
        <li>Verify runtime, ingress, and evidence records.</li>
      </ol>
      <button className="button button-primary" type="button" onClick={onRetry}>
        <RefreshCw size={15} aria-hidden="true" />
        Refresh product inventory
      </button>
    </section>
  );
}

function RouteError({
  eyebrow,
  message,
  onRetry,
  returnTo = "/ui/products",
  title,
  traceId,
}: {
  eyebrow: string;
  message: string;
  onRetry?: () => void;
  returnTo?: string | null;
  title: string;
  traceId: string;
}) {
  return (
    <section className="state-page state-page-error" role="alert">
      <span className="state-page-icon" aria-hidden="true">
        <AlertTriangle />
      </span>
      <p className="eyebrow">{eyebrow}</p>
      <h1 data-route-heading tabIndex={-1}>
        {title}
      </h1>
      <p>{message}</p>
      {traceId ? <code className="trace-id">{traceId}</code> : null}
      {onRetry ? (
        <button className="button button-primary" type="button" onClick={onRetry}>
          <RefreshCw size={15} aria-hidden="true" />
          Retry
        </button>
      ) : returnTo ? (
        <AppLink className="button" to={returnTo}>
          Return to products
        </AppLink>
      ) : null}
    </section>
  );
}

function InlineError({ message, traceId }: { message: string; traceId: string }) {
  return (
    <div className="inline-error" role="alert">
      <AlertTriangle size={17} aria-hidden="true" />
      <span>{message}</span>
      {traceId ? <code>{traceId}</code> : null}
    </div>
  );
}

function MissingEvidenceState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="missing-evidence-state">
      <ShieldQuestion size={20} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

function environmentByName(
  product: ProductSiteOverview,
  environmentName: string,
): ProductEnvironmentSummary | null {
  return (
    product.environments.find(
      (environment) => environment.environment === environmentName,
    ) ?? null
  );
}

function warningsForProduct(product: ProductSiteOverview): WarningItem[] {
  const warningItems: WarningItem[] = product.warnings.map((detail, index) => ({
    id: `product:${index}:${detail}`,
    scope: "Product",
    detail,
    severity: "warning",
  }));
  for (const environment of product.environments) {
    for (const [index, detail] of environment.warnings.entries()) {
      warningItems.push({
        id: `${environment.environment}:summary:${index}:${detail}`,
        scope: environmentLabel(environment.environment),
        detail,
        severity: "warning",
      });
    }
    for (const warning of environment.topology.warnings) {
      warningItems.push(topologyWarningItem(environment, warning));
    }
  }
  const seen = new Set<string>();
  return warningItems.filter((warning) => {
    const key = `${warning.scope}:${warning.detail}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function topologyWarningItem(
  environment: ProductEnvironmentSummary,
  warning: ProductTopologyWarning,
): WarningItem {
  return {
    id: `${environment.environment}:topology:${warning.code}:${warning.domain_name}`,
    scope: `${environmentLabel(environment.environment)} · ${humanize(warning.scope)}`,
    detail: warning.detail,
    severity: warning.severity,
  };
}

function nextSafeInspection(
  product: ProductSiteOverview,
  warnings: WarningItem[],
): InspectionStep {
  for (const environment of [...product.environments].sort(compareEnvironments)) {
    if (environment.topology.warnings.some((warning) => warning.severity === "error")) {
      return {
        title: `Inspect ${environmentLabel(environment.environment)} topology`,
        detail:
          "Launchplane recorded an error-severity topology warning. Review the lane evidence before any mutation.",
        targetId: `lane-${safeAnchor(environment.environment)}`,
      };
    }
  }
  const incomplete = [...product.environments]
    .sort(compareEnvironments)
    .find(
      (environment) =>
        environment.trust_state === "missing" || environment.trust_state === "stale",
    );
  if (incomplete) {
    return {
      title: `Inspect ${environmentLabel(incomplete.environment)} evidence`,
      detail: `The lane is ${trustLabel(incomplete.trust_state).toLowerCase()}. Confirm freshness and missing records before relying on its state.`,
      targetId: `lane-${safeAnchor(incomplete.environment)}`,
    };
  }
  if (warnings.length) {
    return {
      title: "Review recorded warnings",
      detail:
        "Read the product and topology warnings before choosing an operational workflow.",
      targetId: "warning-summary",
    };
  }
  if (!product.environments.length) {
    return {
      title: "Complete stable lane evidence",
      detail:
        "The product profile exists without stable environment summaries. Use Product Onboarding to record lane authority.",
      targetId: "product-diagnostics",
    };
  }
  return {
    title: "Review stable lane evidence",
    detail:
      "Compare testing and production trust, public ingress, TLS, and timestamps before choosing a supported operation.",
    targetId: "stable-lanes",
  };
}

function compareEnvironments(
  left: ProductEnvironmentSummary,
  right: ProductEnvironmentSummary,
): number {
  const rank: Record<string, number> = { testing: 0, prod: 1 };
  return (
    (rank[left.environment] ?? 10) - (rank[right.environment] ?? 10) ||
    left.environment.localeCompare(right.environment)
  );
}

function ingressHeadline(environment: ProductEnvironmentSummary): string {
  const summary = environment.public_ingress.summary.trim();
  if (summary) {
    return summary;
  }
  const status = environment.public_ingress.status.trim();
  if (status) {
    return humanize(status);
  }
  return environment.trust_state === "missing"
    ? "Evidence missing"
    : `${trustLabel(environment.trust_state)} evidence`;
}

function signalHeadline(environment: ProductEnvironmentSummary | null): string {
  const tone = environmentOperationalTone(environment);
  if (tone === "danger") {
    return "Needs attention";
  }
  if (tone === "warning") {
    return "Review warning";
  }
  if (!environment || environment.trust_state === "missing") {
    return "Evidence missing";
  }
  if (environment.trust_state === "unsupported") {
    return "Unsupported";
  }
  const status = environment.public_ingress.status.toLowerCase();
  if (["pass", "passed", "healthy", "ok", "success"].includes(status)) {
    return "Public evidence passed";
  }
  return environment.trust_state === "verified" ? "Evidence current" : "Evidence recorded";
}

function environmentOperationalTone(
  environment: ProductEnvironmentSummary | null,
): SignalTone {
  if (!environment) {
    return "missing";
  }
  const negativeTlsStates = new Set([
    "expired",
    "hostname_mismatch",
    "untrusted",
    "self_signed",
    "unreachable",
  ]);
  if (
    environment.topology.warnings.some((warning) => warning.severity === "error") ||
    environment.topology.observed.tls_domains.some((domain) =>
      negativeTlsStates.has(domain.status),
    )
  ) {
    return "danger";
  }
  if (
    environment.warnings.length ||
    environment.topology.warnings.length ||
    environment.trust_state === "stale"
  ) {
    return "warning";
  }
  return environment.trust_state;
}

function domainForEnvironment(environment: ProductEnvironmentSummary): string {
  const primaryDomain = environment.topology.desired.domains.find(
    (domain) => domain.role === "primary",
  )?.domain_name;
  if (primaryDomain) {
    return primaryDomain;
  }
  const url = safeExternalUrl(environment.base_url);
  return url?.hostname || "No domain evidence";
}

function safeExternalUrl(value: string): URL | null {
  if (!value.trim()) {
    return null;
  }
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

function evidenceTimestamp(provenance: DataProvenance): string {
  return provenance.refreshed_at || provenance.recorded_at;
}

function formatEvidenceTime(provenance: DataProvenance): string {
  const timestamp = evidenceTimestamp(provenance);
  return timestamp ? formatTime(timestamp) : "No timestamp";
}

function environmentLabel(environment: string): string {
  return environment === "prod" ? "Production" : humanize(environment);
}

function trustLabel(state: TrustState): string {
  return state === "verified"
    ? "Verified"
    : state === "recorded"
      ? "Recorded"
      : state === "stale"
        ? "Stale"
        : state === "unsupported"
          ? "Unsupported"
          : "Missing";
}

function humanize(value: string): string {
  const normalized = value.replaceAll("_", " ").trim();
  return normalized
    ? `${normalized.slice(0, 1).toUpperCase()}${normalized.slice(1)}`
    : "Unknown";
}

function safeAnchor(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "lane";
}
