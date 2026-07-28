import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  ExternalLink,
  Globe2,
  Network,
  RefreshCw,
  ServerCog,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  LaunchplaneApiError,
  readProductEnvironment,
  readProductEnvironmentConfigStatus,
  readProductPromotionStatus,
} from "./api";
import {
  loadDevFixtures,
  type DevFixtureMode,
} from "./dev-fixture-loader";
import { EnvironmentActionsView } from "./EnvironmentActions";
import { EnvironmentDiagnostics } from "./EnvironmentDiagnostics";
import { EnvironmentIncidents } from "./EnvironmentIncidents";
import {
  ManagedSecretsView,
  RuntimeSettingsView,
} from "./EnvironmentConfigViews";
import {
  EvidenceBadge,
  InlineError,
  MissingEvidenceState,
  RouteError,
  evidenceTimestamp,
  humanize,
  trustLabel,
  type TrustState,
} from "./ProductOps";
import {
  EnvironmentViewNav,
  ProductWorkspaceNav,
  environmentLabel,
  environmentViewLabel,
} from "./ProductWorkspaceNav";
import {
  emptyResource,
  type ResourceState,
} from "./resource";
import {
  AppLink,
  productPath,
  type AppRoute,
  type EnvironmentView,
} from "./router";
import { safeExternalUrl } from "./url";

import type {
  ProductEnvironmentConfigStatus,
  ProductEnvironmentDetail,
  ProductObservedPlacement,
  ProductObservedTlsDomain,
  ProductPromotionStatus,
  ProductSiteOverview,
  ProductTopologyWarning,
} from "./generated/openapi.ts";

type ConditionTone = "pass" | "warning" | "danger" | "unknown";

export function ProductEnvironmentRoute({
  environmentKey,
  fixtureMode,
  productKey,
  productOverview,
  refreshToken,
  view,
}: {
  environmentKey: string;
  fixtureMode: DevFixtureMode;
  productKey: string;
  productOverview: ProductSiteOverview | null;
  refreshToken: number;
  view: EnvironmentView;
}) {
  const [retryToken, setRetryToken] = useState(0);
  const [detailResource, setDetailResource] = useState<
    ResourceState<ProductEnvironmentDetail>
  >(emptyResource());
  const [configResource, setConfigResource] = useState<
    ResourceState<ProductEnvironmentConfigStatus>
  >(emptyResource());
  const [promotionResource, setPromotionResource] = useState<
    ResourceState<ProductPromotionStatus>
  >(emptyResource());
  const detailLoadedKey = useRef("");
  const configLoadedKey = useRef("");
  const promotionLoadedKey = useRef("");
  const resourceKey = `${productKey}:${environmentKey}`;
  const needsConfig =
    view === "runtime-settings" ||
    view === "managed-secrets" ||
    view === "diagnostics";
  const needsPromotion = view === "actions" && environmentKey === "prod";

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const sameEnvironment = detailLoadedKey.current === resourceKey;
    setDetailResource((current) => ({
      ...current,
      status: "loading",
      data: sameEnvironment ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadDetail() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          const detail = fixtures.environmentForFixture(
            fixtureMode,
            productKey,
            environmentKey,
          );
          if (!active) {
            return;
          }
          if (!detail) {
            setDetailResource(notFoundResource("Environment"));
            return;
          }
          detailLoadedKey.current = resourceKey;
          setDetailResource(readyResource(detail));
          return;
        }

        const response = await readProductEnvironment(
          productKey,
          environmentKey,
          controller.signal,
        );
        if (!active || controller.signal.aborted) {
          return;
        }
        detailLoadedKey.current = resourceKey;
        setDetailResource(readyResource(response.environment));
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setDetailResource((current) =>
          errorResource(sameEnvironment ? current.data : null, error),
        );
      }
    }

    void loadDetail();
    return () => {
      active = false;
      controller.abort();
    };
  }, [environmentKey, fixtureMode, productKey, refreshToken, retryToken, resourceKey]);

  useEffect(() => {
    if (!needsConfig) {
      return;
    }
    let active = true;
    const controller = new AbortController();
    const sameEnvironment = configLoadedKey.current === resourceKey;
    setConfigResource((current) => ({
      ...current,
      status: "loading",
      data: sameEnvironment ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadConfig() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          const config = fixtures.configStatusForFixture(
            fixtureMode,
            productKey,
            environmentKey,
          );
          if (!active) {
            return;
          }
          if (!config) {
            setConfigResource(notFoundResource("Environment config status"));
            return;
          }
          configLoadedKey.current = resourceKey;
          setConfigResource(readyResource(config));
          return;
        }

        const response = await readProductEnvironmentConfigStatus(
          productKey,
          environmentKey,
          controller.signal,
        );
        if (!active || controller.signal.aborted) {
          return;
        }
        configLoadedKey.current = resourceKey;
        setConfigResource(readyResource(response.config_status));
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setConfigResource((current) =>
          errorResource(sameEnvironment ? current.data : null, error),
        );
      }
    }

    void loadConfig();
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    environmentKey,
    fixtureMode,
    needsConfig,
    productKey,
    refreshToken,
    resourceKey,
    retryToken,
  ]);

  useEffect(() => {
    if (!needsPromotion) {
      return;
    }
    let active = true;
    const controller = new AbortController();
    const sameEnvironment = promotionLoadedKey.current === resourceKey;
    setPromotionResource((current) => ({
      ...current,
      status: "loading",
      data: sameEnvironment ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadPromotion() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          const status = fixtures.promotionStatusForFixture(
            fixtureMode,
            productKey,
            environmentKey,
          );
          if (!active) {
            return;
          }
          if (!status) {
            setPromotionResource(notFoundResource("Promotion status"));
            return;
          }
          promotionLoadedKey.current = resourceKey;
          setPromotionResource(readyResource(status));
          return;
        }
        const response = await readProductPromotionStatus(
          productKey,
          environmentKey,
          controller.signal,
        );
        if (!active || controller.signal.aborted) {
          return;
        }
        promotionLoadedKey.current = resourceKey;
        setPromotionResource(readyResource(response.promotion_status));
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setPromotionResource((current) =>
          errorResource(sameEnvironment ? current.data : null, error),
        );
      }
    }

    void loadPromotion();
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    environmentKey,
    fixtureMode,
    needsPromotion,
    productKey,
    refreshToken,
    resourceKey,
    retryToken,
  ]);

  if (
    (detailResource.status === "idle" || detailResource.status === "loading") &&
    !detailResource.data
  ) {
    return <EnvironmentSkeleton environment={environmentKey} />;
  }
  if (detailResource.status === "error" && !detailResource.data) {
    const accessDenied =
      detailResource.statusCode === 401 || detailResource.statusCode === 403;
    const notFound = detailResource.statusCode === 404;
    return (
      <RouteError
        eyebrow="Product environment"
        message={detailResource.error}
        onRetry={
          accessDenied || notFound
            ? undefined
            : () => setRetryToken((current) => current + 1)
        }
        returnTo={productPath(productKey)}
        title={
          notFound
            ? "Environment not found"
            : accessDenied
              ? "Environment access denied"
              : "Environment unavailable"
        }
        traceId={detailResource.traceId}
      />
    );
  }
  if (!detailResource.data) {
    return null;
  }

  const route: AppRoute = {
    kind: "product-environment",
    product: productKey,
    environment: environmentKey,
    view,
  };
  return (
    <EnvironmentPage
      configResource={configResource}
      detail={detailResource.data}
      productOverview={productOverview}
      refreshError={detailResource.status === "error" ? detailResource.error : ""}
      route={route}
      traceId={detailResource.status === "error" ? detailResource.traceId : ""}
      updating={detailResource.status === "loading"}
      fixtureMode={fixtureMode}
      onConfigApplied={() => setRetryToken((current) => current + 1)}
      promotionResource={promotionResource}
      view={view}
    />
  );
}

function EnvironmentPage({
  configResource,
  detail,
  productOverview,
  refreshError,
  route,
  traceId,
  updating,
  fixtureMode,
  onConfigApplied,
  promotionResource,
  view,
}: {
  configResource: ResourceState<ProductEnvironmentConfigStatus>;
  detail: ProductEnvironmentDetail;
  productOverview: ProductSiteOverview | null;
  refreshError: string;
  route: AppRoute;
  traceId: string;
  updating: boolean;
  fixtureMode: DevFixtureMode;
  onConfigApplied: () => void;
  promotionResource: ResourceState<ProductPromotionStatus>;
  view: EnvironmentView;
}) {
  const externalUrl = safeExternalUrl(detail.base_url);
  const laneLabel = environmentLabel(detail.environment);
  const heading = view === "overview" ? laneLabel : environmentViewLabel(view);
  useEffect(() => {
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
  }, [detail.environment, detail.product, view]);

  return (
    <article className="environment-workspace" data-lane={detail.environment}>
      {refreshError ? (
        <InlineError
          message={`${refreshError} Showing the last environment evidence received by this browser.`}
          traceId={traceId}
        />
      ) : null}
      <header className="environment-hero">
        <div className="environment-hero-main">
          <AppLink className="environment-back-link" to={productPath(detail.product)}>
            <ArrowLeft size={14} aria-hidden="true" />
            {detail.display_name}
          </AppLink>
          <div className="environment-heading-line">
            <span className="environment-lane-mark" aria-hidden="true" />
            <div>
              <p className="eyebrow">Stable environment</p>
              <h1 data-route-heading tabIndex={-1}>
                {heading}
              </h1>
            </div>
            <EvidenceBadge
              state={detail.trust_state}
              timestamp={evidenceTimestamp(detail.provenance)}
            />
          </div>
          <p>
            {view === "actions"
              ? "Server-advertised behavior, exact blockers, and the typed browser execution boundary for this lane."
              : "Placement, public ingress, TLS, runtime identity, configuration, and evidence for this lane."}
          </p>
          <div className="environment-hero-meta">
            {externalUrl ? (
              <a href={externalUrl.href} rel="noreferrer" target="_blank">
                <Globe2 size={14} aria-hidden="true" />
                {externalUrl.hostname}
                <ExternalLink size={12} aria-hidden="true" />
              </a>
            ) : (
              <span>No public endpoint recorded</span>
            )}
            <span>{detail.repository || "Owning repository not recorded"}</span>
          </div>
        </div>
        {updating ? (
          <span className="refresh-state">
            <RefreshCw className="spin" size={14} aria-hidden="true" />
            Refreshing evidence
          </span>
        ) : null}
      </header>

      {productOverview ? (
        <ProductWorkspaceNav product={productOverview} route={route} />
      ) : (
        <div className="workspace-nav-fallback">
          <AppLink to={productPath(detail.product)}>Product overview</AppLink>
        </div>
      )}
      <EnvironmentViewNav
        environment={detail.environment}
        product={detail.product}
        view={view}
      />

      {view === "overview" ? (
        <EnvironmentOverview detail={detail} fixtureMode={fixtureMode} />
      ) : null}
      {view === "actions" ? (
        <EnvironmentActionsView
          detail={detail}
          fixtureMode={fixtureMode}
          onRefresh={onConfigApplied}
          promotionResource={promotionResource}
        />
      ) : null}
      {view === "runtime-settings" ? (
        <RuntimeSettingsView
          key={`${detail.product}:${detail.environment}:runtime-settings`}
          configResource={configResource}
          detail={detail}
          fixtureMode={fixtureMode}
          onApplied={onConfigApplied}
        />
      ) : null}
      {view === "managed-secrets" ? (
        <ManagedSecretsView
          key={`${detail.product}:${detail.environment}:managed-secrets`}
          configResource={configResource}
          detail={detail}
          fixtureMode={fixtureMode}
          onApplied={onConfigApplied}
        />
      ) : null}
      {view === "diagnostics" ? (
        <EnvironmentDiagnostics configResource={configResource} detail={detail} />
      ) : null}
    </article>
  );
}

function EnvironmentOverview({
  detail,
  fixtureMode,
}: {
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
}) {
  const tlsDomains = detail.topology.observed.tls_domains;
  const observedPlacement = detail.topology.observed.placement;
  const effectiveChecks = detail.health_monitoring.checks.filter(
    (check) => check.probe_effective,
  );
  const openIncidentCheck = effectiveChecks.find(
    (check) => check.incident_eligible && check.incident_status === "open",
  );
  const openIncidentSeverity =
    openIncidentCheck?.incident_severity || detail.public_ingress.incident_severity;
  const currentIncidentId =
    openIncidentCheck?.incident_id || detail.public_ingress.incident_id;
  const actionableMonitoringFailure = effectiveChecks.some(
    (check) => check.incident_eligible && check.status === "fail",
  );
  const readinessMonitoringFailure = effectiveChecks.some(
    (check) => check.status === "fail",
  );
  const primaryTls =
    tlsDomains.find((domain) => domain.role === "primary") ?? tlsDomains[0] ?? null;
  const warningItems = collectWarnings(detail);
  const diagnosis = diagnosisFor(detail, primaryTls, warningItems);

  return (
    <div className="environment-overview">
      <section className="environment-condition-strip" aria-label="Environment condition">
        <ConditionTile
          detail={
            detail.public_ingress.summary ||
            `${effectiveChecks.length} effective health check${effectiveChecks.length === 1 ? "" : "s"}`
          }
          label="Monitoring"
          timestamp={evidenceTimestamp(detail.public_ingress.provenance)}
          tone={
            openIncidentSeverity === "critical"
              ? "danger"
              : openIncidentSeverity === "warning"
                ? "warning"
                : actionableMonitoringFailure
                  ? "danger"
              : readinessMonitoringFailure
                ? "warning"
                : conditionTone(
                    statusTone(detail.public_ingress.status),
                    detail.health_monitoring.trust_state,
                  )
          }
          trustState={detail.health_monitoring.trust_state}
          value={
            openIncidentSeverity
              ? `Active incident · ${humanize(openIncidentSeverity)}`
              : humanize(detail.health_monitoring.monitoring_intent)
          }
        />
        <ConditionTile
          detail={primaryTls?.summary || trustLabel(primaryTls?.trust_state ?? "missing")}
          label="TLS"
          timestamp={primaryTls ? evidenceTimestamp(primaryTls.provenance) : ""}
          tone={conditionTone(tlsTone(primaryTls), primaryTls?.trust_state ?? "missing")}
          trustState={primaryTls?.trust_state ?? "missing"}
          value={primaryTls ? humanize(primaryTls.status) : "No observation"}
        />
        <ConditionTile
          detail={
            observedPlacement.runtime_identity_detail ||
            trustLabel(observedPlacement.trust_state)
          }
          label="Runtime identity"
          timestamp={evidenceTimestamp(observedPlacement.provenance)}
          tone={conditionTone(
            runtimeIdentityTone(observedPlacement.runtime_identity_status),
            observedPlacement.trust_state,
          )}
          trustState={observedPlacement.trust_state}
          value={humanize(observedPlacement.runtime_identity_status)}
        />
        <ConditionTile
          detail={warningItems.length ? "Review recorded evidence" : trustLabel(detail.trust_state)}
          label="Warnings"
          timestamp={evidenceTimestamp(detail.provenance)}
          tone={conditionTone(
            warningItems.some((warning) => warning.severity === "error")
              ? "danger"
              : warningItems.length
                ? "warning"
                : "unknown",
            detail.trust_state,
          )}
          trustState={detail.trust_state}
          value={`${warningItems.length} recorded`}
        />
      </section>

      {diagnosis ? <DiagnosisCallout diagnosis={diagnosis} /> : null}

      <div className="environment-evidence-grid">
        <IngressEvidence detail={detail} />
        <TlsEvidence detail={detail} tlsDomains={tlsDomains} />
        <PlacementEvidence detail={detail} />
      </div>

      <EnvironmentIncidents
        currentIncidentId={currentIncidentId}
        environment={detail.environment}
        fixtureMode={fixtureMode}
        monitoringTrustState={detail.health_monitoring.trust_state}
        product={detail.product}
      />

      <TopologyWarnings detail={detail} warnings={warningItems} />
      {detail.driver_extensions.odoo ? <OdooDataSafety detail={detail} /> : null}
    </div>
  );
}

function ConditionTile({
  detail,
  label,
  timestamp,
  tone,
  trustState,
  value,
}: {
  detail: string;
  label: string;
  timestamp: string;
  tone: ConditionTone;
  trustState: TrustState;
  value: string;
}) {
  return (
    <div className="condition-tile" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
      <div className="condition-tile-trust">
        <EvidenceBadge state={trustState} timestamp={timestamp} />
      </div>
    </div>
  );
}

function DiagnosisCallout({ diagnosis }: { diagnosis: Diagnosis }) {
  return (
    <section className="diagnosis-callout" data-severity={diagnosis.severity}>
      {diagnosis.severity === "error" ? (
        <AlertCircle size={20} aria-hidden="true" />
      ) : (
        <AlertTriangle size={20} aria-hidden="true" />
      )}
      <div>
        <p className="eyebrow">Recorded diagnosis</p>
        <h2>{diagnosis.title}</h2>
        <p>{diagnosis.detail}</p>
        <a href={`#${diagnosis.targetId}`}>Inspect supporting evidence</a>
      </div>
      <span>Inspect · read-only</span>
    </section>
  );
}

function IngressEvidence({ detail }: { detail: ProductEnvironmentDetail }) {
  const ingress = detail.topology.provider_recorded.ingress;
  const effectiveChecks = detail.health_monitoring.checks.filter(
    (check) => check.probe_effective,
  );
  const checkSummary = effectiveChecks
    .map((check) => `${humanize(check.name)} · ${humanize(check.status)}`)
    .join(", ");
  return (
    <section className="environment-evidence-panel" id="ingress-evidence">
      <div className="evidence-panel-head">
        <span className="evidence-panel-icon" aria-hidden="true">
          <Network />
        </span>
        <div>
          <p className="eyebrow">Ingress and health</p>
          <h2>
            {detail.health_monitoring.monitoring_intent === "private"
              ? "Private monitoring"
              : detail.health_monitoring.monitoring_intent === "prelaunch"
                ? "Prelaunch readiness"
                : "Public path"}
          </h2>
        </div>
        <EvidenceBadge
          state={detail.health_monitoring.trust_state}
          timestamp={evidenceTimestamp(detail.health_monitoring.provenance)}
        />
      </div>
      <dl className="evidence-fact-list">
        <EvidenceFact
          label="Monitoring intent"
          value={humanize(detail.health_monitoring.monitoring_intent)}
        />
        <EvidenceFact
          label="Public incident eligibility"
          value={detail.health_monitoring.public_incident_eligible ? "Eligible" : "Not eligible"}
        />
        <EvidenceFact label="Effective checks" value={checkSummary || "No effective checks"} />
        <EvidenceFact label="Current observation" value={detail.public_ingress.status ? humanize(detail.public_ingress.status) : "No observation"} />
        <EvidenceFact label="Summary" value={detail.public_ingress.summary || "No public ingress summary was returned."} />
        <EvidenceFact label="Desired endpoint" value={detail.topology.desired.base_url || "Not recorded"} />
        <EvidenceFact label="Recorded path" value={humanize(ingress.path)} />
        <EvidenceFact label="Termination" value={humanize(ingress.termination_kind)} />
        <EvidenceFact label="Incident" value={detail.public_ingress.incident_status ? humanize(detail.public_ingress.incident_status) : "No incident recorded"} />
      </dl>
    </section>
  );
}

function TlsEvidence({
  detail,
  tlsDomains,
}: {
  detail: ProductEnvironmentDetail;
  tlsDomains: ProductObservedTlsDomain[];
}) {
  const recordedTls = detail.topology.provider_recorded.tls;
  return (
    <section className="environment-evidence-panel" id="tls-evidence">
      <div className="evidence-panel-head">
        <span className="evidence-panel-icon" aria-hidden="true">
          <ShieldCheck />
        </span>
        <div>
          <p className="eyebrow">Domains and TLS</p>
          <h2>Certificate evidence</h2>
        </div>
        <EvidenceBadge state={detail.topology.observed.trust_state} />
      </div>
      <dl className="evidence-fact-list evidence-fact-list-compact">
        <EvidenceFact
          label="Desired domains"
          value={
            detail.topology.desired.domains.map((domain) => domain.domain_name).join(", ") ||
            "Not recorded"
          }
        />
        <EvidenceFact label="TLS owner" value={humanize(recordedTls.owner)} />
        <EvidenceFact label="Terminator" value={humanize(recordedTls.terminator)} />
      </dl>
      {tlsDomains.length ? (
        <ul className="tls-domain-list">
          {tlsDomains.map((domain) => (
            <li data-tone={tlsTone(domain)} key={`${domain.role}:${domain.domain_name}`}>
              <div>
                <strong>{domain.domain_name}</strong>
                <span>{domain.role}</span>
              </div>
              <div>
                <strong>{humanize(domain.status)}</strong>
                <small>{domain.summary || domain.likely_failure_cause}</small>
              </div>
              <div>
                <strong>{domain.days_remaining === null ? "Unknown" : `${domain.days_remaining} days`}</strong>
                <small>{domain.issuer || "Issuer not recorded"}</small>
              </div>
              <EvidenceBadge
                state={domain.trust_state}
                timestamp={domain.observed_at}
              />
            </li>
          ))}
        </ul>
      ) : (
        <MissingEvidenceState
          detail="No TLS domain observation was returned for this environment."
          title="TLS evidence missing"
        />
      )}
    </section>
  );
}

function PlacementEvidence({ detail }: { detail: ProductEnvironmentDetail }) {
  const observedPlacement = detail.topology.observed.placement;
  const expected = observedPlacement.expected_runtime_identity;
  const observed = observedPlacement.observed_runtime_identity;
  return (
    <section className="environment-evidence-panel" id="runtime-identity">
      <div className="evidence-panel-head">
        <span className="evidence-panel-icon" aria-hidden="true">
          <ServerCog />
        </span>
        <div>
          <p className="eyebrow">Placement and runtime</p>
          <h2>Runtime identity</h2>
        </div>
        <EvidenceBadge
          state={observedPlacement.trust_state}
          timestamp={evidenceTimestamp(observedPlacement.provenance)}
        />
      </div>
      <dl className="evidence-fact-list">
        <EvidenceFact label="Target" value={detail.target.target_name || "Not recorded"} />
        <EvidenceFact label="Target type" value={detail.target.target_type || "Not recorded"} />
        <EvidenceFact label="Identity status" value={humanize(observedPlacement.runtime_identity_status)} />
        <EvidenceFact label="Detail" value={observedPlacement.runtime_identity_detail || "No runtime identity detail was returned."} />
        <EvidenceFact label="Expected artifact" value={expected?.artifact_id || "Not recorded"} mono />
        <EvidenceFact label="Observed artifact" value={observed?.artifact_id || "Not recorded"} mono />
      </dl>
    </section>
  );
}

function EvidenceFact({
  label,
  mono = false,
  value,
}: {
  label: string;
  mono?: boolean;
  value: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{value}</dd>
    </div>
  );
}

function TopologyWarnings({
  detail,
  warnings,
}: {
  detail: ProductEnvironmentDetail;
  warnings: WarningItem[];
}) {
  return (
    <section className="environment-warning-panel" id="topology-warnings">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Topology warnings</p>
          <h2>{warnings.length ? `${warnings.length} recorded` : "No warnings listed"}</h2>
        </div>
        <EvidenceBadge state={detail.topology.trust_state} />
      </div>
      {warnings.length ? (
        <ul>
          {warnings.map((warning) => (
            <li data-severity={warning.severity} key={warning.id}>
              <span>{warning.severity}</span>
              <div>
                <strong>{warning.scope}</strong>
                <p>{warning.detail}</p>
              </div>
              {warning.domain ? <code>{warning.domain}</code> : null}
            </li>
          ))}
        </ul>
      ) : detail.topology.trust_state === "verified" ||
        detail.topology.trust_state === "recorded" ? (
        <p className="environment-warning-empty">
          Launchplane topology evidence currently contains no warning records.
        </p>
      ) : (
        <p className="environment-warning-empty">
          Warning evidence is {trustLabel(detail.topology.trust_state).toLowerCase()}; an
          empty list is not proof of healthy operation.
        </p>
      )}
    </section>
  );
}

function OdooDataSafety({ detail }: { detail: ProductEnvironmentDetail }) {
  const policy = detail.driver_extensions.odoo;
  if (!policy) {
    return null;
  }
  return (
    <section className="odoo-data-safety">
      <p className="eyebrow">Driver data safety</p>
      <h2>{humanize(policy.data_authority)} data authority</h2>
      <dl className="evidence-fact-list evidence-fact-list-compact">
        <EvidenceFact label="Backup before destroy" value={policy.requires_backup_before_destroy ? "Required" : "Not required"} />
        <EvidenceFact label="Restore proof" value={policy.requires_restore_proof ? "Required" : "Not required"} />
        <EvidenceFact label="Runtime identity" value={policy.requires_runtime_identity ? "Required" : "Not required"} />
        <EvidenceFact label="Upstream source" value={policy.upstream_source || "Not recorded"} />
      </dl>
    </section>
  );
}

function EnvironmentSkeleton({ environment }: { environment: string }) {
  return (
    <section className="workspace-skeleton" aria-busy="true" aria-live="polite">
      <p className="eyebrow">Product environment</p>
      <h1 data-route-heading tabIndex={-1}>
        Loading {environmentLabel(environment)} evidence
      </h1>
      <div className="skeleton-hero" />
      <div className="skeleton-lanes">
        <span />
        <span />
      </div>
    </section>
  );
}

interface WarningItem {
  id: string;
  scope: string;
  detail: string;
  domain: string;
  severity: "warning" | "error";
}

interface Diagnosis {
  title: string;
  detail: string;
  targetId: string;
  severity: "warning" | "error";
}

function collectWarnings(detail: ProductEnvironmentDetail): WarningItem[] {
  const topologyItems = detail.topology.warnings.map(topologyWarning);
  const topologyDetails = new Set(topologyItems.map((warning) => warning.detail));
  const summaryItems: WarningItem[] = detail.warnings
    .filter((warning) => !topologyDetails.has(warning))
    .map((warning, index) => ({
      id: `summary:${index}:${warning}`,
      scope: environmentLabel(detail.environment),
      detail: warning,
      domain: "",
      severity: "warning",
    }));
  const items = [...topologyItems, ...summaryItems].sort(
    (left, right) => Number(right.severity === "error") - Number(left.severity === "error"),
  );
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = `${item.scope}:${item.detail}:${item.domain}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function topologyWarning(warning: ProductTopologyWarning): WarningItem {
  return {
    id: `${warning.code}:${warning.scope}:${warning.domain_name}`,
    scope: `${humanize(warning.scope)} · ${humanize(warning.code)}`,
    detail: warning.detail,
    domain: warning.domain_name,
    severity: warning.severity,
  };
}

function diagnosisFor(
  detail: ProductEnvironmentDetail,
  primaryTls: ProductObservedTlsDomain | null,
  warnings: WarningItem[],
): Diagnosis | null {
  const errorWarning = warnings.find((warning) => warning.severity === "error");
  const openIncident = detail.health_monitoring.checks.find(
    (check) => check.incident_eligible && check.incident_status === "open",
  );
  if (openIncident) {
    return {
      title: openIncident.summary || `${humanize(openIncident.name)} incident open`,
      detail: openIncident.failure_code
        ? `Failure code: ${humanize(openIncident.failure_code)}.`
        : "Launchplane recorded an open material incident for this health check.",
      targetId: "incident-history",
      severity: openIncident.incident_severity === "warning" ? "warning" : "error",
    };
  }
  if (primaryTls && tlsTone(primaryTls) === "danger") {
    return {
      title: primaryTls.summary || `${primaryTls.domain_name} TLS needs attention`,
      detail:
        primaryTls.likely_failure_cause ||
        errorWarning?.detail ||
        "Launchplane recorded a failing TLS observation for the public domain.",
      targetId: "tls-evidence",
      severity: "error",
    };
  }
  if (statusTone(detail.public_ingress.status) === "danger") {
    return {
      title: detail.public_ingress.summary || "Public ingress needs attention",
      detail:
        errorWarning?.detail ||
        "Launchplane recorded a failing public ingress observation.",
      targetId: "ingress-evidence",
      severity: "error",
    };
  }
  if (detail.topology.observed.placement.runtime_identity_status === "mismatch") {
    return {
      title: "Runtime identity does not match",
      detail: detail.topology.observed.placement.runtime_identity_detail,
      targetId: "runtime-identity",
      severity: "error",
    };
  }
  if (warnings.length) {
    return {
      title: warnings[0].scope,
      detail: warnings[0].detail,
      targetId: "topology-warnings",
      severity: warnings[0].severity,
    };
  }
  return null;
}

function statusTone(status: string): ConditionTone {
  const normalized = status.toLowerCase();
  if (["pass", "passed", "healthy", "ok", "success", "valid", "match"].includes(normalized)) {
    return "pass";
  }
  if (["fail", "failed", "error", "unhealthy", "down", "blocked"].includes(normalized)) {
    return "danger";
  }
  if (["pending", "degraded", "expiring", "warning"].includes(normalized)) {
    return "warning";
  }
  return "unknown";
}

function conditionTone(tone: ConditionTone, trustState: TrustState): ConditionTone {
  if (trustState === "missing" || trustState === "unsupported") {
    return "unknown";
  }
  if (trustState === "stale" && tone !== "danger") {
    return "warning";
  }
  return tone;
}

function tlsTone(domain: ProductObservedTlsDomain | null): ConditionTone {
  if (!domain) {
    return "unknown";
  }
  if (domain.status === "valid") {
    return "pass";
  }
  if (domain.status === "expiring") {
    return "warning";
  }
  if (
    ["expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable"].includes(
      domain.status,
    )
  ) {
    return "danger";
  }
  return "unknown";
}

function runtimeIdentityTone(
  status: ProductObservedPlacement["runtime_identity_status"],
): ConditionTone {
  if (status === "match") {
    return "pass";
  }
  if (status === "mismatch" || status === "malformed") {
    return "danger";
  }
  if (status === "missing" || status === "unverifiable") {
    return "warning";
  }
  return "unknown";
}

function readyResource<T>(data: T): ResourceState<T> {
  return {
    status: "ready",
    data,
    error: "",
    traceId: "",
    statusCode: 200,
  };
}

function notFoundResource<T>(label: string): ResourceState<T> {
  return {
    status: "error",
    data: null,
    error: `${label} was not found.`,
    traceId: "",
    statusCode: 404,
  };
}

function errorResource<T>(data: T | null, error: unknown): ResourceState<T> {
  return {
    status: "error",
    data,
    error:
      error instanceof Error ? error.message : "Launchplane environment request failed.",
    traceId: error instanceof LaunchplaneApiError ? error.traceId : "",
    statusCode: error instanceof LaunchplaneApiError ? error.statusCode : 503,
  };
}
