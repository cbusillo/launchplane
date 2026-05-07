import { AlertTriangle, Database, ExternalLink } from "lucide-react";

import { KeyValue, PanelHead } from "./panel-ui";
import { SkeletonRows, StateBlock, StatusPill } from "./status-ui";
import { freshnessLabel, TrustBadge } from "./TrustBadge";

import type { DriverChoice } from "./useProductSelection";
import type { ProductSiteOverview } from "./types";

export function ProductOverviewShell({
  product,
  selected,
  loading,
  selectedEnvironment,
  onSelectEnvironment,
}: {
  product: ProductSiteOverview | null;
  selected: DriverChoice;
  loading: boolean;
  selectedEnvironment: string;
  onSelectEnvironment: (environment: string) => void;
}) {
  const environments = product?.environments ?? [];
  const activeEnvironment =
    environments.find(
      (environment) => environment.environment === selectedEnvironment,
    ) ?? environments[0];
  const enabledActions = product?.available_actions.filter(
    (action) => action.enabled,
  );
  const blockedActions = product?.available_actions.filter(
    (action) => !action.enabled,
  );
  const preview = product?.preview;

  return (
    <section className="panel product-overview-shell">
      <PanelHead
        eyebrow="product workspace"
        title={product?.display_name || selected.label}
        right={
          <div className="panel-badges">
            <TrustBadge provenance={product?.provenance ?? null} />
            <StatusPill status={product?.trust_state ?? "missing"} />
          </div>
        }
      />
      {loading ? (
        <SkeletonRows />
      ) : (
        <div className="product-overview-grid">
          <div className="product-overview-identity">
            <div>
              <span className="overview-label">Product key</span>
              <code>{product?.product || selected.driverId}</code>
            </div>
            <div>
              <span className="overview-label">Repository</span>
              <code>
                {product?.repository || selected.repository || "unknown"}
              </code>
            </div>
            <div>
              <span className="overview-label">Driver</span>
              <code>
                {product?.driver_id || selected.driverLabel}
                {product?.base_driver_id ? ` / ${product.base_driver_id}` : ""}
              </code>
            </div>
          </div>
          <div className="product-environment-strip">
            {environments.length ? (
              environments.map((environment) => (
                <button
                  type="button"
                  className="product-environment-pill"
                  key={`${environment.environment}:${environment.context}`}
                  data-environment={environment.environment}
                  data-selected={
                    activeEnvironment?.environment === environment.environment
                  }
                  onClick={() => onSelectEnvironment(environment.environment)}
                >
                  <span>{environment.environment}</span>
                  <strong>{freshnessLabel(environment.trust_state)}</strong>
                  <code>{environment.context}</code>
                </button>
              ))
            ) : (
              <StateBlock
                icon={<Database size={18} />}
                title="No product environment read model"
              />
            )}
          </div>
          <div className="product-overview-sidecar">
            <KeyValue
              label="Previews"
              value={
                preview?.enabled
                  ? `${preview.active_count} active / ${preview.context || "no context"}`
                  : "not enabled"
              }
              status={preview?.enabled ? preview.trust_state : "unsupported"}
            />
            <KeyValue
              label="Enabled actions"
              value={`${enabledActions?.length ?? 0} enabled`}
              status={enabledActions?.length ? "pass" : "unknown"}
            />
            <KeyValue
              label="Blocked actions"
              value={`${blockedActions?.length ?? 0} blocked`}
              status={blockedActions?.length ? "blocked" : "pass"}
            />
          </div>
        </div>
      )}
      {!loading && activeEnvironment ? (
        <EnvironmentDetailStrip environment={activeEnvironment} />
      ) : null}
      {product?.warnings.length ? (
        <div className="overview-warning-list">
          {product.warnings.map((warning) => (
            <span key={warning}>
              <AlertTriangle size={14} aria-hidden="true" />
              {warning}
            </span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function EnvironmentDetailStrip({
  environment,
}: {
  environment: ProductSiteOverview["environments"][number];
}) {
  const enabledActions = environment.available_actions.filter(
    (action) => action.enabled,
  );
  const blockedActions = environment.available_actions.filter(
    (action) => !action.enabled,
  );
  return (
    <div className="environment-detail-strip" data-environment={environment.environment}>
      <div className="environment-detail-identity">
        <span className={`lane-chip lane-chip-${environment.environment}`}>
          {environment.environment}
        </span>
        <code>{environment.context}</code>
        {environment.base_url ? (
          <a href={environment.base_url} target="_blank" rel="noreferrer">
            <ExternalLink size={13} aria-hidden="true" />
            {environment.base_url}
          </a>
        ) : null}
      </div>
      <div className="environment-detail-facts">
        <KeyValue
          label="Health URL"
          value={environment.health_url}
          mono
          muted={!environment.health_url}
        />
        <KeyValue
          label="Enabled lane actions"
          value={`${enabledActions.length} enabled`}
          status={enabledActions.length ? "pass" : "unknown"}
        />
        <KeyValue
          label="Blocked lane actions"
          value={`${blockedActions.length} blocked`}
          status={blockedActions.length ? "blocked" : "pass"}
        />
      </div>
      {environment.available_actions.length ? (
        <div className="environment-action-strip">
          {environment.available_actions.slice(0, 4).map((action) => (
            <span
              className="environment-action-chip"
              data-enabled={action.enabled}
              key={action.action_id}
              title={action.disabled_reasons.join("; ") || action.description}
            >
              {action.label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
