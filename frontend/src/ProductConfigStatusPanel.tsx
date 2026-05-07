import { KeyRound, Settings2 } from "lucide-react";

import { formatTime, labelForStatus } from "./format";
import type { ReactNode } from "react";
import { PanelHead } from "./panel-ui";
import { StatusPill, StateBlock } from "./status-ui";
import { freshnessLabel } from "./TrustBadge";
import type {
  ProductConfigItemStatus,
  ProductEnvironmentConfigStatus,
  ProductManagedSecretConfigStatusItem,
  ProductRuntimeConfigStatusItem,
} from "./types";

export function ProductConfigStatusPanel({
  statuses,
  loading,
  error,
}: {
  statuses: ProductEnvironmentConfigStatus[];
  loading: boolean;
  error: string;
}) {
  const totals = summarizeConfigStatuses(statuses);
  return (
    <section className="panel config-status-panel" aria-busy={loading}>
      <PanelHead
        eyebrow="settings readiness"
        title="Expected config"
        right={
          <div className="panel-badges">
            <StatusPill status={statusToUiStatus(totals.worst)} />
            <span className="count-chip">{totals.configured}/{totals.total}</span>
          </div>
        }
      />
      {error ? (
        <div className="config-status-alert">
          <span>Config status unavailable</span>
          <code>{error}</code>
        </div>
      ) : null}
      {loading && !statuses.length ? <ConfigStatusSkeleton /> : null}
      {!loading && !statuses.length && !error ? (
        <StateBlock icon={<Settings2 size={18} />} title="No expected config status" />
      ) : null}
      {statuses.map((status) => (
        <EnvironmentConfigStatus key={`${status.product}:${status.environment}`} status={status} />
      ))}
    </section>
  );
}

function EnvironmentConfigStatus({
  status,
}: {
  status: ProductEnvironmentConfigStatus;
}) {
  const runtimeItems = status.runtime_settings;
  const secretItems = status.managed_secrets;
  return (
    <div className="config-status-environment" data-environment={status.environment}>
      <div className="config-status-environment-head">
        <div>
          <strong>{status.environment}</strong>
          <code>{status.context}</code>
        </div>
        <span className="trust-badge trust-compact" data-freshness={status.trust_state}>
          <span>{freshnessLabel(status.trust_state)}</span>
        </span>
      </div>
      <ConfigStatusGroup
        title="Runtime settings"
        emptyTitle="No expected runtime keys"
        icon={<Settings2 size={16} aria-hidden="true" />}
        items={runtimeItems.map((item) => ({
          id: `${item.context}:${item.instance}:${item.key}`,
          name: item.key,
          status: item.status,
          route: configRouteLabel(item.context, item.instance),
          detail: item.source_label || freshnessLabel(item.trust_state),
          updatedAt: item.updated_at,
        }))}
      />
      <ConfigStatusGroup
        title="Secrets"
        emptyTitle="No expected secret bindings"
        icon={<KeyRound size={16} aria-hidden="true" />}
        items={secretItems.map((item) => secretStatusRow(item))}
      />
    </div>
  );
}

function ConfigStatusGroup({
  title,
  emptyTitle,
  icon,
  items,
}: {
  title: string;
  emptyTitle: string;
  icon: ReactNode;
  items: ConfigStatusRowItem[];
}) {
  return (
    <div className="config-status-group">
      <div className="config-status-group-title">
        {icon}
        <span>{title}</span>
      </div>
      <div className="config-status-list">
        {items.length ? (
          items.map((item) => <ConfigStatusRow item={item} key={item.id} />)
        ) : (
          <StateBlock icon={icon} title={emptyTitle} />
        )}
      </div>
    </div>
  );
}

type ConfigStatusRowItem = {
  id: string;
  name: string;
  status: ProductConfigItemStatus;
  route: string;
  detail: string;
  updatedAt: string;
};

function ConfigStatusRow({ item }: { item: ConfigStatusRowItem }) {
  return (
    <div className="config-status-row" data-status={statusToUiStatus(item.status)}>
      <StatusPill status={statusToUiStatus(item.status)} />
      <div>
        <strong>{item.name}</strong>
        <span>{labelForStatus(item.status)}</span>
      </div>
      <code>{item.route}</code>
      <span>{item.updatedAt ? formatTime(item.updatedAt) : item.detail}</span>
    </div>
  );
}

function secretStatusRow(
  item: ProductManagedSecretConfigStatusItem,
): ConfigStatusRowItem {
  return {
    id: `${item.integration}:${item.context}:${item.instance}:${item.binding_key}`,
    name: item.binding_key,
    status: item.status,
    route: configRouteLabel(item.context, item.instance),
    detail: item.integration,
    updatedAt: item.updated_at,
  };
}

function summarizeConfigStatuses(statuses: ProductEnvironmentConfigStatus[]) {
  const items = statuses.flatMap((status) => [
    ...status.runtime_settings.map((item) => item.status),
    ...status.managed_secrets.map((item) => item.status),
  ]);
  const configured = items.filter((status) => status === "configured").length;
  return {
    configured,
    total: items.length,
    worst: worstConfigStatus(items),
  };
}

function worstConfigStatus(
  statuses: ProductConfigItemStatus[],
): ProductConfigItemStatus {
  if (statuses.some((status) => status === "missing" || status === "disabled")) {
    return "missing";
  }
  if (statuses.some((status) => status === "stale" || status === "unvalidated")) {
    return "stale";
  }
  if (statuses.some((status) => status === "unsupported")) {
    return "unsupported";
  }
  if (statuses.some((status) => status === "configured")) {
    return "configured";
  }
  return "unsupported";
}

function statusToUiStatus(status: ProductConfigItemStatus): string {
  if (status === "configured") {
    return "pass";
  }
  if (status === "stale" || status === "unvalidated") {
    return "pending";
  }
  if (status === "unsupported") {
    return "unknown";
  }
  return "blocked";
}

function configRouteLabel(context: string, instance: string): string {
  if (context && instance) {
    return `${context}/${instance}`;
  }
  return context || "global";
}

function ConfigStatusSkeleton() {
  return (
    <div className="config-status-skeleton" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}
