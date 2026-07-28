import {
  BellRing,
  CheckCircle2,
  Clock3,
  ExternalLink,
  FileClock,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  LaunchplaneApiError,
  listProductEnvironmentIncidents,
  readProductEnvironmentIncident,
} from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import { formatTime } from "./format";
import { EvidenceBadge, InlineError, humanize, type TrustState } from "./ProductOps";
import { emptyResource, type ResourceState } from "./resource";
import { safeExternalUrl } from "./url";

import type {
  ProductEnvironmentIncidentList,
  ProductIncidentDetail,
  ProductIncidentSummary,
} from "./generated/openapi.ts";

export function EnvironmentIncidents({
  currentIncidentId,
  environment,
  fixtureMode,
  monitoringTrustState,
  product,
}: {
  currentIncidentId: string;
  environment: string;
  fixtureMode: DevFixtureMode;
  monitoringTrustState: TrustState;
  product: string;
}) {
  const [listRetryToken, setListRetryToken] = useState(0);
  const [detailRetryToken, setDetailRetryToken] = useState(0);
  const [selectedIncidentId, setSelectedIncidentId] = useState(currentIncidentId);
  const [listResource, setListResource] = useState<
    ResourceState<ProductEnvironmentIncidentList>
  >(emptyResource());
  const [detailResource, setDetailResource] = useState<
    ResourceState<ProductIncidentDetail>
  >(emptyResource());
  const listLoadedKey = useRef("");
  const detailLoadedKey = useRef("");
  const resourceKey = `${product}:${environment}`;

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const sameEnvironment = listLoadedKey.current === resourceKey;
    setListResource((current) => ({
      ...current,
      status: "loading",
      data: sameEnvironment ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadIncidentList() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          const incidentList = fixtures.incidentsForFixture(
            fixtureMode,
            product,
            environment,
          );
          if (!active) {
            return;
          }
          if (!incidentList) {
            setListResource(
              errorResource<ProductEnvironmentIncidentList>(
                null,
                new Error("Incident history was not found."),
                404,
              ),
            );
            return;
          }
          listLoadedKey.current = resourceKey;
          setListResource(readyResource(incidentList));
          return;
        }
        const response = await listProductEnvironmentIncidents(
          product,
          environment,
          controller.signal,
        );
        if (!active || controller.signal.aborted) {
          return;
        }
        listLoadedKey.current = resourceKey;
        setListResource(readyResource(response.incident_list));
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setListResource((current) =>
          errorResource(sameEnvironment ? current.data : null, error),
        );
      }
    }

    void loadIncidentList();
    return () => {
      active = false;
      controller.abort();
    };
  }, [environment, fixtureMode, listRetryToken, product, resourceKey]);

  useEffect(() => {
    const incidents = listResource.data?.incidents ?? [];
    if (!incidents.length) {
      setSelectedIncidentId("");
      return;
    }
    setSelectedIncidentId((current) => {
      if (current && incidents.some((incident) => incident.incident_id === current)) {
        return current;
      }
      if (
        currentIncidentId &&
        incidents.some((incident) => incident.incident_id === currentIncidentId)
      ) {
        return currentIncidentId;
      }
      return incidents[0]?.incident_id ?? "";
    });
  }, [currentIncidentId, listResource.data]);

  useEffect(() => {
    if (!selectedIncidentId) {
      setDetailResource(emptyResource());
      return;
    }
    let active = true;
    const controller = new AbortController();
    const detailKey = `${resourceKey}:${selectedIncidentId}`;
    const sameIncident = detailLoadedKey.current === detailKey;
    setDetailResource((current) => ({
      ...current,
      status: "loading",
      data: sameIncident ? current.data : null,
      error: "",
      traceId: "",
      statusCode: 0,
    }));

    async function loadIncidentDetail() {
      try {
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          const detail = fixtures.incidentForFixture(
            fixtureMode,
            product,
            environment,
            selectedIncidentId,
          );
          if (!active) {
            return;
          }
          if (!detail) {
            setDetailResource(
              errorResource<ProductIncidentDetail>(
                null,
                new Error("Incident evidence was not found."),
                404,
              ),
            );
            return;
          }
          detailLoadedKey.current = detailKey;
          setDetailResource(readyResource(detail));
          return;
        }
        const response = await readProductEnvironmentIncident(
          product,
          environment,
          selectedIncidentId,
          controller.signal,
        );
        if (!active || controller.signal.aborted) {
          return;
        }
        detailLoadedKey.current = detailKey;
        setDetailResource(readyResource(response.incident));
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        setDetailResource((current) =>
          errorResource(sameIncident ? current.data : null, error),
        );
      }
    }

    void loadIncidentDetail();
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    detailRetryToken,
    environment,
    fixtureMode,
    product,
    resourceKey,
    selectedIncidentId,
  ]);

  const activeIncidents = useMemo(
    () => listResource.data?.incidents.filter((incident) => incident.status === "open") ?? [],
    [listResource.data],
  );

  return (
    <section
      aria-labelledby="incident-history-heading"
      className="incident-workbench"
      id="incident-history"
    >
      <div className="incident-workbench-head">
        <div>
          <p className="eyebrow">Incident response</p>
          <h2 id="incident-history-heading">Public ingress incidents</h2>
          <p>
            Launchplane incident records are authoritative. External notifications are
            delivery evidence, not the incident source of truth.
          </p>
        </div>
        <div className="incident-workbench-status">
          {listResource.data ? (
            <div className="incident-history-trust">
              <EvidenceBadge
                compact
                state={listResource.data.trust_state}
                timestamp={
                  listResource.data.provenance.refreshed_at ||
                  listResource.data.provenance.recorded_at
                }
              />
              <small>{listResource.data.provenance.detail}</small>
            </div>
          ) : null}
          <div className="incident-count" data-active={activeIncidents.length > 0}>
            <ShieldAlert size={17} aria-hidden="true" />
            <strong>{activeIncidents.length}</strong>
            <span>open</span>
          </div>
        </div>
      </div>

      {listResource.status === "error" ? (
        <div className="incident-list-error">
          <InlineError
            message={`${listResource.error}${listResource.data ? " Showing the last incident history received by this browser." : ""}`}
            traceId={listResource.traceId}
          />
          {!listResource.data &&
          listResource.statusCode !== 403 &&
          listResource.statusCode !== 404 ? (
            <button
              className="secondary-button"
              onClick={() => setListRetryToken((current) => current + 1)}
              type="button"
            >
              <RefreshCw size={14} aria-hidden="true" />
              Retry incident history
            </button>
          ) : null}
        </div>
      ) : null}

      {(listResource.status === "idle" || listResource.status === "loading") &&
      !listResource.data ? (
        <IncidentLoadingState />
      ) : null}

      {listResource.data && !listResource.data.incidents.length ? (
        <IncidentEmptyState
          monitoringTrustState={
            listResource.data.trust_state === "missing" ||
            listResource.data.trust_state === "stale"
              ? listResource.data.trust_state
              : monitoringTrustState
          }
        />
      ) : null}

      {listResource.data?.incidents.length ? (
        <div className="incident-workbench-grid">
          <IncidentList
            incidents={listResource.data.incidents}
            onSelect={setSelectedIncidentId}
            selectedIncidentId={selectedIncidentId}
          />
          <div className="incident-detail-region" aria-live="polite">
            {detailResource.status === "error" ? (
              <div className="incident-detail-error">
                <InlineError message={detailResource.error} traceId={detailResource.traceId} />
                {detailResource.statusCode !== 403 && detailResource.statusCode !== 404 ? (
                  <button
                    className="secondary-button"
                    onClick={() => setDetailRetryToken((current) => current + 1)}
                    type="button"
                  >
                    <RefreshCw size={14} aria-hidden="true" />
                    Retry evidence
                  </button>
                ) : null}
              </div>
            ) : null}
            {(detailResource.status === "idle" || detailResource.status === "loading") &&
            !detailResource.data ? (
              <IncidentDetailLoadingState />
            ) : null}
            {detailResource.data ? <IncidentDetailView detail={detailResource.data} /> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function IncidentList({
  incidents,
  onSelect,
  selectedIncidentId,
}: {
  incidents: ProductIncidentSummary[];
  onSelect: (incidentId: string) => void;
  selectedIncidentId: string;
}) {
  return (
    <nav className="incident-list" aria-label="Recorded incidents">
      {incidents.map((incident) => (
        <button
          aria-current={incident.incident_id === selectedIncidentId ? "true" : undefined}
          className="incident-list-item"
          data-severity={incident.severity}
          data-status={incident.status}
          key={incident.incident_id}
          onClick={() => onSelect(incident.incident_id)}
          type="button"
        >
          <span className="incident-list-status">
            <IncidentStateBadge incident={incident} />
            <IncidentNotificationBadge state={incident.notification_state} />
          </span>
          <strong>{humanize(incident.check_name)}</strong>
          <span>{incident.summary}</span>
          <small>
            {incident.status === "resolved" ? "Resolved" : "Opened"} {formatTime(
              incident.status === "resolved" ? incident.resolved_at : incident.opened_at,
            )}
          </small>
        </button>
      ))}
    </nav>
  );
}

function IncidentDetailView({ detail }: { detail: ProductIncidentDetail }) {
  const { incident } = detail;
  return (
    <article className="incident-detail" data-severity={incident.severity}>
      <header className="incident-detail-head">
        <div>
          <div className="incident-detail-badges">
            <IncidentStateBadge incident={incident} />
            <IncidentNotificationBadge state={incident.notification_state} />
          </div>
          <h3>{humanize(incident.check_name)}</h3>
          <p>{incident.summary}</p>
        </div>
        <EvidenceBadge state={incident.trust_state} timestamp={incident.latest_observed_at} />
      </header>

      <dl className="incident-facts">
        <IncidentFact label="Failure layer" value={humanize(incident.failure_layer)} />
        <IncidentFact label="Failure code" value={humanize(incident.failure_code)} />
        <IncidentFact
          label="Duration"
          value={formatIncidentDuration(incident.opened_at, incident.resolved_at)}
        />
        <IncidentFact label="Opened" value={formatTime(incident.opened_at)} />
        <IncidentFact
          label="Latest event"
          value={`${humanize(incident.latest_material_event || "not recorded")} · ${formatTime(incident.latest_material_event_at)}`}
        />
        <IncidentFact
          label="Recovery"
          value={`${incident.consecutive_recovery_observations} of ${incident.recovery_observation_threshold} required observations`}
        />
        {incident.notification_state === "active" && incident.next_reminder_at ? (
          <IncidentFact label="Next reminder" value={formatTime(incident.next_reminder_at)} />
        ) : null}
        {incident.last_reminded_at ? (
          <IncidentFact label="Last reminder" value={formatTime(incident.last_reminded_at)} />
        ) : null}
        {incident.status === "resolved" ? (
          <IncidentFact
            label="Resolution"
            value={`${humanize(incident.resolution_reason)} · ${formatTime(incident.resolved_at)}`}
          />
        ) : null}
        <IncidentFact
          label="Evidence fingerprint"
          value={shortRecordId(incident.material_fingerprint_sha256) || "Not recorded"}
          mono
        />
      </dl>

      {detail.material_evidence ? (
        <section className="incident-evidence-summary" aria-label="Material incident evidence">
          <h4>Material evidence</h4>
          <p>
            {humanize(detail.material_evidence.failure_layer)} failure affecting{" "}
            {detail.material_evidence.affected_targets.map(humanize).join(", ")}. Route
            authority: {humanize(detail.material_evidence.route_authority_kind)}.
          </p>
          {detail.material_evidence.runtime_identity_status ? (
            <p>
              Runtime identity: {humanize(detail.material_evidence.runtime_identity_status)}
              {detail.material_evidence.runtime_identity_mismatched_fields.length
                ? ` (${detail.material_evidence.runtime_identity_mismatched_fields.join(", ")})`
                : ""}
            </p>
          ) : null}
          {detail.material_evidence.tls_status ? (
            <p>TLS state: {humanize(detail.material_evidence.tls_status)}</p>
          ) : null}
        </section>
      ) : null}

      <div className="incident-detail-sections">
        <IncidentTimeline detail={detail} />
        <IncidentObservations detail={detail} />
        <IncidentReminders detail={detail} />
        <IncidentDeliveries detail={detail} />
      </div>
    </article>
  );
}

function IncidentTimeline({ detail }: { detail: ProductIncidentDetail }) {
  return (
    <section className="incident-detail-section">
      <div className="incident-section-title">
        <FileClock size={16} aria-hidden="true" />
        <div>
          <h4>Material timeline</h4>
          <p>Open, update, reminder, suppression, and resolution events.</p>
        </div>
      </div>
      {detail.events.length ? (
        <ol className="incident-timeline">
          {detail.events.map((event) => (
            <li key={event.event_id}>
              <span className="incident-timeline-marker" data-event={event.event} />
              <div>
                <strong>{humanize(event.event)}</strong>
                <small>{formatTime(event.occurred_at)}</small>
                <p>{event.summary}</p>
                {!event.delivery_eligible ? (
                  <span className="incident-suppression">
                    Delivery suppressed · {humanize(event.suppression_reason)}
                  </span>
                ) : null}
                <RecordReference label="Event" value={event.event_id} />
                <RecordReference label="Observation" value={event.observation_id} />
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="incident-section-empty">No material event records were returned.</p>
      )}
    </section>
  );
}

function IncidentObservations({ detail }: { detail: ProductIncidentDetail }) {
  return (
    <section className="incident-detail-section">
      <div className="incident-section-title">
        <Clock3 size={16} aria-hidden="true" />
        <div>
          <h4>Observation evidence</h4>
          <p>Durable probe and reconciliation records linked to this occurrence.</p>
        </div>
      </div>
      {detail.observations.length ? (
        <ul className="incident-observation-list">
          {detail.observations.map((observation) => (
            <li data-status={observation.status} key={observation.record_id}>
              <div>
                <strong>
                  {humanize(observation.purpose)} · {humanize(observation.status)}
                </strong>
                <small>{formatTime(observation.observed_at)}</small>
              </div>
              <p>{observation.summary}</p>
              <RecordReference label="Observation" value={observation.record_id} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="incident-section-empty">No linked observation records were returned.</p>
      )}
    </section>
  );
}

function IncidentReminders({ detail }: { detail: ProductIncidentDetail }) {
  return (
    <section className="incident-detail-section">
      <div className="incident-section-title">
        <Clock3 size={16} aria-hidden="true" />
        <div>
          <h4>Reminder state</h4>
          <p>Bounded cadence anchored to the current material event.</p>
        </div>
      </div>
      {detail.reminders.length ? (
        <ul className="incident-reminder-list">
          {detail.reminders.map((reminder) => (
            <li key={reminder.reminder_state_id}>
              <div>
                <strong>{humanize(reminder.status)}</strong>
                <span>{formatReminderInterval(reminder.interval_seconds)}</span>
              </div>
              <small>
                {reminder.next_reminder_at
                  ? `Next ${formatTime(reminder.next_reminder_at)}`
                  : reminder.last_reminded_at
                    ? `Last ${formatTime(reminder.last_reminded_at)}`
                    : `Updated ${formatTime(reminder.updated_at)}`}
              </small>
              <RecordReference label="Reminder" value={reminder.reminder_state_id} />
              <RecordReference label="Material event" value={reminder.material_event_id} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="incident-section-empty">No reminder state was recorded.</p>
      )}
    </section>
  );
}

function IncidentDeliveries({ detail }: { detail: ProductIncidentDetail }) {
  const deliveries = detail.notification_attempts;
  return (
    <section className="incident-detail-section">
      <div className="incident-section-title">
        <BellRing size={16} aria-hidden="true" />
        <div>
          <h4>Notification delivery</h4>
          <p>External sinks and crash-safe outbox state linked to material events.</p>
        </div>
      </div>
      {deliveries.length ? (
        <ul className="incident-delivery-list">
          {deliveries.map((delivery) => {
            const externalUrl = safeExternalUrl(delivery.external_url);
            return (
              <li data-status={delivery.delivery_status} key={delivery.attempt_id}>
                <div>
                  <strong>{humanize(delivery.destination_kind)}</strong>
                  <span>{humanize(delivery.delivery_status)}</span>
                </div>
                <small>
                  {humanize(delivery.event)} · {formatTime(delivery.attempted_at)}
                </small>
                {delivery.error_message ? <p>Delivery failed.</p> : null}
                {externalUrl ? (
                  <a href={externalUrl.href} rel="noreferrer" target="_blank">
                    Open delivery sink
                    <ExternalLink size={13} aria-hidden="true" />
                  </a>
                ) : null}
                <RecordReference label="Attempt" value={delivery.attempt_id} />
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="incident-section-empty">No notification attempts were recorded.</p>
      )}
      {detail.outbox_deliveries.length ? (
        <details className="incident-outbox-details">
          <summary>Outbox delivery state · {detail.outbox_deliveries.length}</summary>
          <ul>
            {detail.outbox_deliveries.map((delivery) => (
              <li key={delivery.delivery_id}>
                <span>
                  <strong>{humanize(delivery.state)}</strong>
                  <small>
                    Attempt {delivery.attempt} of {delivery.max_attempts} · {formatTime(
                      delivery.updated_at,
                    )}
                  </small>
                </span>
                <RecordReference label="Outbox" value={delivery.delivery_id} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function IncidentStateBadge({ incident }: { incident: ProductIncidentSummary }) {
  const label =
    incident.status === "resolved"
      ? "Resolved"
      : `${humanize(incident.severity)} incident`;
  return (
    <span
      className="incident-state-badge"
      data-severity={incident.severity}
      data-status={incident.status}
    >
      {incident.status === "resolved" ? (
        <CheckCircle2 size={12} aria-hidden="true" />
      ) : (
        <ShieldAlert size={12} aria-hidden="true" />
      )}
      {label}
    </span>
  );
}

function IncidentNotificationBadge({ state }: { state: ProductIncidentSummary["notification_state"] }) {
  return (
    <span className="incident-notification-badge" data-state={state}>
      {state === "silenced"
        ? "Silenced"
        : state === "acknowledged"
          ? "Acknowledged"
          : "Notifications active"}
    </span>
  );
}

function IncidentFact({
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
      <dd className={mono ? "record-reference" : undefined}>{value}</dd>
    </div>
  );
}

function RecordReference({ label, value }: { label: string; value: string }) {
  return (
    <span className="record-reference" title={value}>
      {label} · {shortRecordId(value)}
    </span>
  );
}

function IncidentLoadingState() {
  return (
    <div className="incident-loading" aria-label="Loading incident history">
      <span />
      <span />
      <span />
    </div>
  );
}

function IncidentDetailLoadingState() {
  return (
    <div className="incident-detail-loading" aria-label="Loading incident evidence">
      <RefreshCw size={18} aria-hidden="true" />
      <span>Loading linked incident evidence…</span>
    </div>
  );
}

function IncidentEmptyState({ monitoringTrustState }: { monitoringTrustState: TrustState }) {
  const incomplete = monitoringTrustState === "missing" || monitoringTrustState === "stale";
  return (
    <div className="incident-empty-state" data-incomplete={incomplete}>
      {incomplete ? (
        <ShieldAlert size={18} aria-hidden="true" />
      ) : (
        <CheckCircle2 size={18} aria-hidden="true" />
      )}
      <div>
        <strong>{incomplete ? "Incident evidence is incomplete" : "No incidents recorded"}</strong>
        <p>
          {incomplete
            ? "Launchplane returned no incident records, but monitoring evidence is missing or stale."
            : "Launchplane returned no public ingress incident occurrences for this environment."}
        </p>
      </div>
    </div>
  );
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

function errorResource<T>(
  data: T | null,
  error: unknown,
  fallbackStatusCode = 503,
): ResourceState<T> {
  if (error instanceof LaunchplaneApiError) {
    return {
      status: "error",
      data,
      error: error.message,
      traceId: error.traceId,
      statusCode: error.statusCode,
    };
  }
  return {
    status: "error",
    data,
    error: error instanceof Error ? error.message : "Incident evidence request failed.",
    traceId: "",
    statusCode: fallbackStatusCode,
  };
}

function formatReminderInterval(intervalSeconds: number): string {
  const hours = intervalSeconds / 3600;
  if (Number.isInteger(hours)) {
    return hours === 1 ? "Every hour" : `Every ${hours} hours`;
  }
  const minutes = Math.round(intervalSeconds / 60);
  return minutes === 1 ? "Every minute" : `Every ${minutes} minutes`;
}

function shortRecordId(value: string): string {
  if (value.length <= 18) {
    return value;
  }
  return `${value.slice(0, 10)}…${value.slice(-6)}`;
}

function formatIncidentDuration(openedAt: string, resolvedAt: string): string {
  const opened = Date.parse(openedAt);
  const ended = resolvedAt ? Date.parse(resolvedAt) : Date.now();
  if (!Number.isFinite(opened) || !Number.isFinite(ended) || ended < opened) {
    return "Unknown";
  }
  const totalMinutes = Math.floor((ended - opened) / 60_000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days) {
    return `${days}d ${hours}h`;
  }
  if (hours) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}
