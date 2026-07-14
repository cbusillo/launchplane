import {
  Activity,
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Clock3,
  GitCommitHorizontal,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { LaunchplaneApiError, readProductActivity } from "./api";
import {
  loadDevFixtures,
  type DevFixtureMode,
} from "./dev-fixture-loader";
import {
  EvidenceBadge,
  InlineError,
  RouteError,
  humanize,
} from "./ProductOps";
import { ProductWorkspaceNav, environmentLabel } from "./ProductWorkspaceNav";
import {
  emptyResource,
  type ResourceState,
} from "./resource";
import {
  AppLink,
  productPath,
  type AppRoute,
} from "./router";

import type {
  ProductActivityEvent,
  ProductActivityReadModel,
  ProductSiteOverview,
} from "./generated/openapi.ts";

export function ProductActivityRoute({
  fixtureMode,
  productKey,
  productOverview,
  refreshToken,
}: {
  fixtureMode: DevFixtureMode;
  productKey: string;
  productOverview: ProductSiteOverview | null;
  refreshToken: number;
}) {
  const [retryToken, setRetryToken] = useState(0);
  const [resource, setResource] = useState<ResourceState<ProductActivityReadModel>>(
    emptyResource(),
  );
  const loadedProduct = useRef("");

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const sameProduct = loadedProduct.current === productKey;
    setResource((current) => ({
      ...current,
      status: "loading",
      data: sameProduct ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadActivity() {
      try {
        const activity = fixtureMode
          ? await loadDevFixtures().then((fixtures) =>
              fixtures.activityForFixture(fixtureMode, productKey),
            )
          : await readProductActivity(productKey, controller.signal).then(
              (payload) => payload.activity,
            );
        if (!active || controller.signal.aborted) {
          return;
        }
        if (!activity) {
          setResource({
            status: "error",
            data: null,
            error: "Product activity was not found.",
            traceId: "",
            statusCode: 404,
          });
          return;
        }
        loadedProduct.current = productKey;
        setResource({
          status: "ready",
          data: activity,
          error: "",
          traceId: "",
          statusCode: 200,
        });
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setResource((current) => ({
          status: "error",
          data: current.data,
          error:
            error instanceof Error
              ? error.message
              : "Launchplane could not load recent product activity.",
          traceId: error instanceof LaunchplaneApiError ? error.traceId : "",
          statusCode: error instanceof LaunchplaneApiError ? error.statusCode : 503,
        }));
      }
    }

    void loadActivity();
    return () => {
      active = false;
      controller.abort();
    };
  }, [fixtureMode, productKey, refreshToken, retryToken]);

  if ((resource.status === "idle" || resource.status === "loading") && !resource.data) {
    return <ActivitySkeleton />;
  }
  if (resource.status === "error" && !resource.data) {
    const accessDenied = resource.statusCode === 401 || resource.statusCode === 403;
    const notFound = resource.statusCode === 404;
    return (
      <RouteError
        eyebrow="Product activity"
        message={resource.error}
        onRetry={
          accessDenied || notFound
            ? undefined
            : () => setRetryToken((current) => current + 1)
        }
        returnTo={productPath(productKey)}
        title={
          notFound
            ? "Product activity not found"
            : accessDenied
              ? "Product activity access denied"
              : "Product activity unavailable"
        }
        traceId={resource.traceId}
      />
    );
  }
  if (!resource.data) {
    return null;
  }

  const route: AppRoute = { kind: "product-activity", product: productKey };
  return (
    <ActivityPage
      activity={resource.data}
      productOverview={productOverview}
      refreshError={resource.status === "error" ? resource.error : ""}
      route={route}
      traceId={resource.status === "error" ? resource.traceId : ""}
      updating={resource.status === "loading"}
    />
  );
}

function ActivityPage({
  activity,
  productOverview,
  refreshError,
  route,
  traceId,
  updating,
}: {
  activity: ProductActivityReadModel;
  productOverview: ProductSiteOverview | null;
  refreshError: string;
  route: AppRoute;
  traceId: string;
  updating: boolean;
}) {
  const events = [...activity.events].sort(
    (left, right) => Date.parse(right.occurred_at) - Date.parse(left.occurred_at),
  );
  useEffect(() => {
    document.title = `${activity.display_name} · Recent activity · Launchplane`;
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
  }, [activity.display_name]);

  return (
    <article className="activity-workspace">
      {refreshError ? (
        <InlineError
          message={`${refreshError} Showing the last activity timeline received by this browser.`}
          traceId={traceId}
        />
      ) : null}
      <header className="activity-hero">
        <AppLink className="environment-back-link" to={productPath(activity.product)}>
          <ArrowLeft size={14} aria-hidden="true" />
          {activity.display_name}
        </AppLink>
        <div>
          <p className="eyebrow">Operator timeline</p>
          <h1 data-route-heading tabIndex={-1}>
            Recent activity
          </h1>
          <p>
            The latest deployments, promotions, preview lifecycle, cleanup, policy,
            and evidence events returned by Launchplane. This is a recent window,
            not a complete audit export.
          </p>
        </div>
        {updating ? (
          <span className="activity-refresh-state">
            <Clock3 size={14} aria-hidden="true" />
            Refreshing timeline
          </span>
        ) : null}
      </header>

      {productOverview ? (
        <ProductWorkspaceNav product={productOverview} route={route} />
      ) : (
        <div className="workspace-nav-fallback">
          <AppLink to={productPath(activity.product)}>Product overview</AppLink>
        </div>
      )}

      {events.length ? (
        <ol className="activity-timeline">
          {events.map((event) => (
            <ActivityEventRow event={event} key={event.event_id} />
          ))}
        </ol>
      ) : (
        <section className="activity-empty">
          <Activity size={24} aria-hidden="true" />
          <div>
            <h2>No recent activity records</h2>
            <p>
              Launchplane returned an empty recent activity window for this product.
              It does not infer events from provider logs.
            </p>
          </div>
        </section>
      )}
    </article>
  );
}

function ActivityEventRow({ event }: { event: ProductActivityEvent }) {
  const tone = eventTone(event.status);
  const scopeLabel = event.environment.trim()
    ? environmentLabel(event.environment)
    : "Product-wide";
  return (
    <li className="activity-event" data-tone={tone}>
      <span className="activity-event-marker" aria-hidden="true">
        <EventIcon tone={tone} />
      </span>
      <article>
        <header>
          <div>
            <span className="activity-lane">{scopeLabel}</span>
            <h2>{event.title}</h2>
          </div>
          <time dateTime={event.occurred_at}>{formatActivityTime(event.occurred_at)}</time>
        </header>
        <p>{event.summary}</p>
        <div className="activity-event-meta">
          <span className="activity-status" data-tone={tone}>
            {humanize(event.status)}
          </span>
          <EvidenceBadge compact state={event.trust_state} />
        </div>
        <details className="activity-event-diagnostics">
          <summary>Technical event details</summary>
          <dl>
            <div>
              <dt>Event type</dt>
              <dd>{event.event_type}</dd>
            </div>
            <div>
              <dt>Action ID</dt>
              <dd>{event.action_id || "missing"}</dd>
            </div>
            <div>
              <dt>Context</dt>
              <dd>{event.context || "missing"}</dd>
            </div>
            <div>
              <dt>Driver</dt>
              <dd>{event.driver_id || "missing"}</dd>
            </div>
            <div>
              <dt>Event ID</dt>
              <dd>{event.event_id}</dd>
            </div>
          </dl>
          {event.records.length ? (
            <ul>
              {event.records.map((record) => (
                <li key={`${record.record_type}:${record.record_id}`}>
                  <span>{record.record_type}</span>
                  <code>{record.record_id}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p>No linked records were returned.</p>
          )}
        </details>
      </article>
    </li>
  );
}

function EventIcon({ tone }: { tone: "pass" | "warning" | "danger" | "unknown" }) {
  if (tone === "pass") {
    return <CheckCircle2 />;
  }
  if (tone === "danger") {
    return <AlertCircle />;
  }
  if (tone === "warning") {
    return <ShieldCheck />;
  }
  return <GitCommitHorizontal />;
}

function ActivitySkeleton() {
  return (
    <section className="workspace-skeleton" aria-busy="true" aria-live="polite">
      <p className="eyebrow">Product activity</p>
      <h1 data-route-heading tabIndex={-1}>
        Loading recent activity
      </h1>
      <div className="skeleton-directory">
        <span />
        <span />
        <span />
      </div>
    </section>
  );
}

function eventTone(status: string): "pass" | "warning" | "danger" | "unknown" {
  const normalized = status.toLowerCase();
  if (["pass", "passed", "success", "healthy", "done", "completed"].includes(normalized)) {
    return "pass";
  }
  if (["fail", "failed", "error", "blocked", "rejected"].includes(normalized)) {
    return "danger";
  }
  if (["pending", "running", "queued", "stale", "warning"].includes(normalized)) {
    return "warning";
  }
  return "unknown";
}

function formatActivityTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "No timestamp";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}
