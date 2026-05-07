import { AlertTriangle, Database } from "lucide-react";

import { KeyValue, PanelHead } from "./panel-ui";
import { SkeletonRows, StateBlock, StatusPill } from "./status-ui";
import { freshnessLabel, TrustBadge } from "./TrustBadge";

import type { DriverChoice } from "./useProductSelection";
import type { ProductSiteOverview } from "./types";

export function ProductOverviewShell({
  product,
  selected,
  loading,
}: {
  product: ProductSiteOverview | null;
  selected: DriverChoice;
  loading: boolean;
}) {
  const environments = product?.environments ?? [];
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
                <div
                  className="product-environment-pill"
                  key={`${environment.environment}:${environment.context}`}
                  data-environment={environment.environment}
                >
                  <span>{environment.environment}</span>
                  <strong>{freshnessLabel(environment.trust_state)}</strong>
                  <code>{environment.context}</code>
                </div>
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
