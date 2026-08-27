import {
  AlertTriangle,
  Ban,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import { useLayoutEffect, useRef, type ReactNode } from "react";

import { formatTime } from "./format";
import {
  AppLink,
  engineeringPath,
  engineeringViewLabel,
  type EngineeringView,
} from "./router";
import type { EngineeringResourceState } from "./engineering-resource";

const ENGINEERING_VIEWS: Exclude<EngineeringView, "hub">[] = [
  "work-graph",
  "issue-inbox",
  "every-code",
  "merge-train",
  "tenant-admission",
  "governance-projection",
  "owner-acceptance",
  "authorization-administration",
  "privileged-operations",
];

export function EngineeringRouteFrame({
  actions,
  children,
  description,
  icon: Icon,
  title,
  view,
}: {
  actions?: ReactNode;
  children: ReactNode;
  description: string;
  icon: LucideIcon;
  title: string;
  view: EngineeringView;
}) {
  const navigationRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const navigation = navigationRef.current;
    const activeLink = navigation?.querySelector<HTMLElement>(
      '[aria-current="page"]',
    );
    if (!navigation || !activeLink) {
      return;
    }
    navigation.scrollLeft = Math.max(
      0,
      activeLink.offsetLeft -
        (navigation.clientWidth - activeLink.offsetWidth) / 2,
    );
  }, [view]);

  return (
    <section className="engineering-route">
      <nav
        className="engineering-route-nav"
        aria-label="Engineering Ops views"
        ref={navigationRef}
      >
        <AppLink
          aria-current={view === "hub" ? "page" : undefined}
          data-active={view === "hub"}
          to={engineeringPath()}
        >
          Overview
        </AppLink>
        {ENGINEERING_VIEWS.map((routeView) => (
          <AppLink
            aria-current={view === routeView ? "page" : undefined}
            data-active={view === routeView}
            key={routeView}
            to={engineeringPath(routeView)}
          >
            {engineeringViewLabel(routeView)}
          </AppLink>
        ))}
      </nav>
      <header className="engineering-route-hero">
        <div className="engineering-route-copy">
          <p className="eyebrow">Engineering Ops</p>
          <h1 data-route-heading tabIndex={-1}>
            {title}
          </h1>
          <p>{description}</p>
        </div>
        <div className="engineering-route-actions">
          {actions}
          <span className="engineering-route-icon" aria-hidden="true">
            <Icon size={24} />
          </span>
        </div>
      </header>
      {children}
    </section>
  );
}

export function EngineeringResourceControls<T>({
  cancel,
  refresh,
  refreshLabel,
  state,
}: {
  cancel: () => void;
  refresh: () => void;
  refreshLabel: string;
  state: EngineeringResourceState<T>;
}) {
  return (
    <div className="engineering-resource-controls" aria-live="polite">
      <span data-state={resourceStateLabel(state)}>
        {state.refreshing
          ? "Loading evidence"
          : state.stale
            ? "Cached evidence"
            : state.phase === "ready"
              ? "Latest response"
              : "No response"}
      </span>
      {state.lastSuccessfulAt ? (
        <time dateTime={state.lastSuccessfulAt}>
          {formatTime(state.lastSuccessfulAt)}
        </time>
      ) : null}
      {state.refreshing ? (
        <button className="button" type="button" onClick={cancel}>
          <X size={15} aria-hidden="true" />
          Cancel
        </button>
      ) : (
        <button className="button" type="button" onClick={refresh}>
          <RefreshCw size={15} aria-hidden="true" />
          {refreshLabel}
        </button>
      )}
    </div>
  );
}

export function EngineeringResourceGate<T>({
  children,
  noun,
  refresh,
  state,
}: {
  children: (data: T) => ReactNode;
  noun: string;
  refresh: () => void;
  state: EngineeringResourceState<T>;
}) {
  if (!state.data && state.phase === "loading") {
    return <EngineeringLoading noun={noun} />;
  }
  if (!state.data && state.phase === "denied") {
    return (
      <EngineeringState
        action="Retry"
        detail={state.error || `This browser session cannot read ${noun}.`}
        icon={ShieldAlert}
        onAction={refresh}
        title="Access denied"
        traceId={state.traceId}
      />
    );
  }
  if (!state.data && state.phase === "cancelled") {
    return (
      <EngineeringState
        action="Load again"
        detail={`The ${noun} request was cancelled before a response was accepted.`}
        icon={Ban}
        onAction={refresh}
        title="Load cancelled"
      />
    );
  }
  if (!state.data && state.phase === "error") {
    return (
      <EngineeringState
        action="Retry"
        detail={state.error || `Launchplane could not load ${noun}.`}
        icon={AlertTriangle}
        onAction={refresh}
        title={`${capitalize(noun)} unavailable`}
        traceId={state.traceId}
      />
    );
  }
  if (!state.data) {
    return <EngineeringLoading noun={noun} />;
  }
  return (
    <>
      <EngineeringRefreshNotice noun={noun} state={state} />
      {children(state.data)}
    </>
  );
}

export function EngineeringEmpty({
  detail,
  icon: Icon,
  title,
}: {
  detail: string;
  icon: LucideIcon;
  title: string;
}) {
  return (
    <div className="engineering-empty">
      <Icon size={21} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

export function EngineeringBoundaryNote({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <aside className="engineering-boundary-note">
      <ShieldAlert size={18} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </aside>
  );
}

function EngineeringLoading({ noun }: { noun: string }) {
  return (
    <div className="engineering-loading" role="status">
      <LoaderCircle className="spin" size={20} aria-hidden="true" />
      <span>Loading {noun}…</span>
      <div className="engineering-skeleton" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
    </div>
  );
}

function EngineeringState({
  action,
  detail,
  icon: Icon,
  onAction,
  title,
  traceId = "",
}: {
  action: string;
  detail: string;
  icon: LucideIcon;
  onAction: () => void;
  title: string;
  traceId?: string;
}) {
  return (
    <div className="engineering-state" role="alert">
      <Icon size={22} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
        {traceId ? <code>{traceId}</code> : null}
      </div>
      <button className="button" type="button" onClick={onAction}>
        {action}
      </button>
    </div>
  );
}

function EngineeringRefreshNotice<T>({
  noun,
  state,
}: {
  noun: string;
  state: EngineeringResourceState<T>;
}) {
  if (!state.stale) {
    return null;
  }
  return (
    <div className="engineering-refresh-notice" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>
          {state.cancelled ? "Refresh cancelled" : "Refresh failed"}
        </strong>
        <p>
          {state.cancelled
            ? `Showing the last accepted ${noun} response.`
            : `${state.error} Showing the last accepted response instead.`}
        </p>
        {state.traceId ? <code>{state.traceId}</code> : null}
      </div>
    </div>
  );
}

function resourceStateLabel<T>(state: EngineeringResourceState<T>): string {
  if (state.refreshing) {
    return "loading";
  }
  if (state.stale) {
    return "stale";
  }
  return state.phase;
}

function capitalize(value: string): string {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : value;
}
