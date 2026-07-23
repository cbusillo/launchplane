import {
  Ban,
  CircleEllipsis,
  Eye,
  GitBranch,
  PlayCircle,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

import {
  ACTION_KIND_LABELS,
  browserActionPresentation,
  compareBrowserActionPresentations,
  type BrowserActionPresentation,
} from "./action-model";
import type { BrowserActionKind } from "./browser-operation";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { ProductPromotionFlow } from "./ProductPromotionFlow";
import { EvidenceBadge, MissingEvidenceState, humanize } from "./ProductOps";
import { EnvironmentReadinessPanel } from "./EnvironmentReadiness";
import type { ResourceState } from "./resource";

import type {
  ProductEnvironmentDetail,
  ProductPromotionStatus,
} from "./generated/openapi.ts";

const ACTION_KINDS: BrowserActionKind[] = [
  "inspect",
  "dry-run",
  "apply",
  "workflow-dispatch",
  "destructive",
  "unsupported",
];

export function EnvironmentActionsView({
  detail,
  fixtureMode,
  onRefresh,
  promotionResource,
}: {
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
  onRefresh: () => void;
  promotionResource: ResourceState<ProductPromotionStatus>;
}) {
  const actions = detail.available_actions
    .map((action) => browserActionPresentation(detail.driver_id, action))
    .sort(compareBrowserActionPresentations);
  const serverEnabled = actions.filter(({ action }) => action.enabled).length;
  const planned = actions.filter(
    ({ browserSupport }) => browserSupport === "planned",
  ).length;
  const executable = actions.filter(({ canExecute }) => canExecute).length;

  return (
    <section className="environment-actions-view">
      {detail.environment === "prod" ? (
        <ProductPromotionFlow
          detail={detail}
          fixtureMode={fixtureMode}
          onRefresh={onRefresh}
          statusResource={promotionResource}
        />
      ) : null}
      <header className="environment-actions-header">
        <span aria-hidden="true">
          <PlayCircle />
        </span>
        <div>
          <p className="eyebrow">Operator actions</p>
          <h2>Supported behavior, blockers, and browser boundary</h2>
          <p>
            Launchplane lists descriptor actions as evidence, but the browser only
            executes explicit generated operations. No advertised route is invoked
            dynamically.
          </p>
        </div>
      </header>

      <div className="action-boundary-note">
        <ShieldCheck size={18} aria-hidden="true" />
        <div>
          <strong>Fail-closed browser controls</strong>
          <p>
            A server-enabled action remains unavailable until a typed control owns
            its input, confirmation, idempotency, replay, and result states.
          </p>
        </div>
      </div>

      <EnvironmentReadinessPanel
        actions={detail.available_actions}
        detail={detail}
        fixtureMode={fixtureMode}
        key={`${detail.product}:${detail.context}:${detail.environment}`}
      />

      <div className="action-summary-strip" aria-label="Operator action summary">
        <div>
          <strong>{actions.length}</strong>
          <span>advertised</span>
        </div>
        <div>
          <strong>{serverEnabled}</strong>
          <span>server enabled</span>
        </div>
        <div>
          <strong>{planned}</strong>
          <span>typed flows planned</span>
        </div>
        <div>
          <strong>{executable}</strong>
          <span>enabled here</span>
        </div>
      </div>

      <ActionKindLegend />

      {actions.length ? (
        <ul className="environment-action-list" aria-label="Environment actions">
          {actions.map((presentation) => (
            <EnvironmentActionCard
              key={`${presentation.action.action_id}:${presentation.action.route_path}`}
              presentation={presentation}
            />
          ))}
        </ul>
      ) : (
        <MissingEvidenceState
          detail="The product environment read model did not advertise any operator-visible actions for this lane. Launchplane does not infer controls from driver names or provider state."
          title="No operator actions advertised"
        />
      )}
    </section>
  );
}

function ActionKindLegend() {
  return (
    <section className="action-kind-legend" aria-labelledby="action-kind-title">
      <div>
        <p className="eyebrow">Action taxonomy</p>
        <h2 id="action-kind-title">Every control declares its behavior</h2>
      </div>
      <ul>
        {ACTION_KINDS.map((kind) => (
          <li data-kind={kind} key={kind}>
            <ActionKindIcon kind={kind} />
            <span>{ACTION_KIND_LABELS[kind]}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EnvironmentActionCard({
  presentation,
}: {
  presentation: BrowserActionPresentation;
}) {
  const {
    action,
    browserBlockers,
    browserSupport,
    canExecute,
    kind,
    serverBlockers,
  } = presentation;
  return (
    <li className="environment-action-card" data-kind={kind}>
      <div className="environment-action-icon" aria-hidden="true">
        <ActionKindIcon kind={kind} />
      </div>
      <div className="environment-action-body">
        <div className="environment-action-title">
          <span className="action-kind-badge" data-kind={kind}>
            {ACTION_KIND_LABELS[kind]}
          </span>
          <EvidenceBadge compact state={action.trust_state} />
        </div>
        <h3>{action.label}</h3>
        {action.description ? <p>{action.description}</p> : null}
        <dl className="environment-action-facts">
          <div>
            <dt>Server safety</dt>
            <dd>{humanize(action.safety || "unknown")}</dd>
          </div>
          <div>
            <dt>Scope</dt>
            <dd>{humanize(action.scope || "unknown")}</dd>
          </div>
          <div>
            <dt>Server availability</dt>
            <dd>{action.enabled ? "Enabled" : "Blocked"}</dd>
          </div>
          <div>
            <dt>Browser support</dt>
            <dd>{browserSupportLabel(browserSupport)}</dd>
          </div>
        </dl>
        {serverBlockers.length ? (
          <ActionBlockers
            blockers={serverBlockers}
            label="Launchplane blockers"
          />
        ) : null}
        {browserBlockers.length ? (
          <ActionBlockers blockers={browserBlockers} label="Browser blockers" />
        ) : null}
      </div>
      <span className="environment-action-state">
        {canExecute ? (
          <ShieldCheck size={14} aria-hidden="true" />
        ) : (
          <Ban size={14} aria-hidden="true" />
        )}
        {browserSupport === "diagnostic"
          ? "Diagnostics only"
          : canExecute
            ? "Executable"
            : "Not executable"}
      </span>
    </li>
  );
}

function ActionBlockers({
  blockers,
  label,
}: {
  blockers: string[];
  label: string;
}) {
  return (
    <div className="environment-action-blockers">
      <strong>
        <ShieldAlert size={14} aria-hidden="true" />
        {label}
      </strong>
      <ul>
        {blockers.map((blocker, index) => (
          <li key={`${index}:${blocker}`}>{blocker}</li>
        ))}
      </ul>
    </div>
  );
}

function browserSupportLabel(
  support: BrowserActionPresentation["browserSupport"],
): string {
  if (support === "implemented") {
    return "Typed flow installed";
  }
  if (support === "diagnostic") {
    return "Product-owned flow above";
  }
  if (support === "planned") {
    return "Typed flow planned";
  }
  return "Unsupported";
}

function ActionKindIcon({ kind }: { kind: BrowserActionKind }) {
  if (kind === "inspect") {
    return <Eye size={16} aria-hidden="true" />;
  }
  if (kind === "dry-run") {
    return <PlayCircle size={16} aria-hidden="true" />;
  }
  if (kind === "workflow-dispatch") {
    return <GitBranch size={16} aria-hidden="true" />;
  }
  if (kind === "destructive") {
    return <TriangleAlert size={16} aria-hidden="true" />;
  }
  if (kind === "apply") {
    return <ShieldCheck size={16} aria-hidden="true" />;
  }
  return <CircleEllipsis size={16} aria-hidden="true" />;
}
