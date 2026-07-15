import {
  AlertTriangle,
  CheckCircle2,
  KeyRound,
  ListChecks,
  LockKeyhole,
  ShieldQuestion,
} from "lucide-react";

import {
  EvidenceBadge,
  InlineError,
  MissingEvidenceState,
  evidenceTimestamp,
  humanize,
  trustLabel,
} from "./ProductOps";
import {
  ManagedSecretsChangePanel,
  RuntimeSettingsChangePanel,
} from "./ProductConfigForms";
import type { DevFixtureMode } from "./dev-fixture-loader";
import type { ResourceState } from "./resource";

import type {
  ProductEnvironmentConfigStatus,
  ProductEnvironmentDetail,
  ProductManagedSecretConfigStatusItem,
  ProductRuntimeConfigStatusItem,
} from "./generated/openapi.ts";

type ConfigStatus =
  | "configured"
  | "missing"
  | "disabled"
  | "unvalidated"
  | "stale"
  | "unsupported";

export function RuntimeSettingsView({
  configResource,
  detail,
  fixtureMode,
  onApplied,
}: {
  configResource: ResourceState<ProductEnvironmentConfigStatus>;
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
  onApplied: () => void;
}) {
  const settings = configResource.data?.runtime_settings ?? [];
  return (
    <section className="config-view" aria-busy={configResource.status === "loading"}>
      <ConfigViewHeader
        detail="Launchplane compares expected runtime keys with recorded environment authority. Values are never returned to this browser."
        icon={<ListChecks />}
        title="Runtime settings"
      />
      {configResource.status === "error" ? (
        <InlineError
          message={`${configResource.error}${configResource.data ? " Showing the last config-status evidence received by this browser." : " Recorded setting sources remain available below."}`}
          traceId={configResource.traceId}
        />
      ) : null}
      {configResource.data ? (
        <ConfigReadEvidence config={configResource.data} />
      ) : null}
      {configResource.status === "ready" && configResource.data ? (
        <RuntimeSettingsChangePanel
          config={configResource.data}
          fixtureMode={fixtureMode}
          onApplied={onApplied}
        />
      ) : null}
      {configResource.data && configResource.status !== "ready" ? (
        <ConfigWriteAuthorityUnavailable />
      ) : null}
      {configResource.data ? <ConfigStatusSummary items={settings} /> : null}
      {(configResource.status === "idle" || configResource.status === "loading") &&
      !configResource.data ? (
        <ConfigLoading />
      ) : configResource.status === "error" && !configResource.data ? (
        <ConfigEmpty
          detail="Launchplane could not determine whether runtime-setting requirements are configured for this environment."
          title="Runtime-setting status unavailable"
        />
      ) : settings.length ? (
        <ul className="config-status-list" aria-label="Runtime setting requirements">
          {settings.map((setting) => (
            <RuntimeSettingRow key={setting.key} setting={setting} />
          ))}
        </ul>
      ) : (
        <ConfigEmpty
          detail="No expected runtime-setting keys were returned for this environment."
          title="No runtime-setting requirements recorded"
        />
      )}
      <RecordedSettingSources detail={detail} />
    </section>
  );
}

export function ManagedSecretsView({
  configResource,
  detail,
  fixtureMode,
  onApplied,
}: {
  configResource: ResourceState<ProductEnvironmentConfigStatus>;
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
  onApplied: () => void;
}) {
  const secrets = configResource.data?.managed_secrets ?? [];
  return (
    <section className="config-view" aria-busy={configResource.status === "loading"}>
      <ConfigViewHeader
        detail="This surface shows binding names, integration, status, trust, and timestamps only. Secret plaintext and ciphertext never enter browser state."
        icon={<LockKeyhole />}
        title="Managed secrets"
      />
      <div className="secret-safety-banner">
        <KeyRound size={18} aria-hidden="true" />
        <div>
          <strong>Secret values are never displayed</strong>
          <p>
            Launchplane returns binding metadata and validation state, not secret
            material.
          </p>
        </div>
      </div>
      {configResource.status === "error" ? (
        <InlineError
          message={`${configResource.error}${configResource.data ? " Showing the last secret-status evidence received by this browser." : " Recorded binding metadata remains available below and in Diagnostics."}`}
          traceId={configResource.traceId}
        />
      ) : null}
      {configResource.data ? (
        <ConfigReadEvidence config={configResource.data} />
      ) : null}
      {configResource.status === "ready" && configResource.data ? (
        <ManagedSecretsChangePanel
          config={configResource.data}
          fixtureMode={fixtureMode}
          onApplied={onApplied}
        />
      ) : null}
      {configResource.data && configResource.status !== "ready" ? (
        <ConfigWriteAuthorityUnavailable />
      ) : null}
      {configResource.data ? <ConfigStatusSummary items={secrets} /> : null}
      {(configResource.status === "idle" || configResource.status === "loading") &&
      !configResource.data ? (
        <ConfigLoading />
      ) : configResource.status === "error" && !configResource.data ? (
        <ConfigEmpty
          detail="Launchplane could not determine whether managed-secret requirements are configured for this environment."
          title="Managed-secret status unavailable"
        />
      ) : secrets.length ? (
        <ul className="config-status-list" aria-label="Managed secret requirements">
          {secrets.map((secret) => (
            <ManagedSecretRow key={`${secret.integration}:${secret.binding_key}`} secret={secret} />
          ))}
        </ul>
      ) : (
        <ConfigEmpty
          detail="No expected managed-secret bindings were returned for this environment."
          title="No managed-secret requirements recorded"
        />
      )}
      <RecordedBindingsSummary detail={detail} />
    </section>
  );
}

function ConfigViewHeader({
  detail,
  icon,
  title,
}: {
  detail: string;
  icon: React.ReactNode;
  title: string;
}) {
  return (
    <header className="config-view-header">
      <span aria-hidden="true">{icon}</span>
      <div>
        <p className="eyebrow">Environment configuration</p>
        <h2>{title}</h2>
        <p>{detail}</p>
      </div>
    </header>
  );
}

function ConfigWriteAuthorityUnavailable() {
  return (
    <div className="product-config-blockers" role="status">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>Configuration writes are paused</strong>
        <p>
          Launchplane must refresh current authorization and prerequisite evidence before
          planning or applying changes.
        </p>
      </div>
    </div>
  );
}

function ConfigReadEvidence({
  config,
}: {
  config: ProductEnvironmentConfigStatus;
}) {
  return (
    <div className="config-read-evidence">
      <div>
        <span className="config-read-evidence-copy">
          <strong>Config-status evidence</strong>
          <small>{config.provenance.detail || "No provenance detail recorded"}</small>
        </span>
        <EvidenceBadge
          state={config.trust_state}
          timestamp={evidenceTimestamp(config.provenance)}
        />
      </div>
      {config.warnings.length ? (
        <ul aria-label="Configuration read warnings">
          {config.warnings.map((warning, index) => (
            <li key={`${index}:${warning}`}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function ConfigStatusSummary({
  items,
}: {
  items: Array<{ status: ConfigStatus }>;
}) {
  const counts = new Map<ConfigStatus, number>();
  for (const item of items) {
    counts.set(item.status, (counts.get(item.status) ?? 0) + 1);
  }
  const priority: ConfigStatus[] = [
    "missing",
    "disabled",
    "stale",
    "unvalidated",
    "configured",
    "unsupported",
  ];
  return (
    <div className="config-summary-strip" aria-label="Configuration status summary">
      {priority.map((status) => (
        <div data-status={status} key={status}>
          <strong>{counts.get(status) ?? 0}</strong>
          <span>{humanize(status)}</span>
        </div>
      ))}
    </div>
  );
}

function RuntimeSettingRow({ setting }: { setting: ProductRuntimeConfigStatusItem }) {
  return (
    <li className="config-status-row" data-status={setting.status}>
      <span className="config-status-icon" aria-hidden="true">
        <ConfigStatusIcon status={setting.status} />
      </span>
      <span className="config-status-identity">
        <strong>{setting.key}</strong>
        <small>Runtime setting</small>
      </span>
      <ConfigStatusBadge status={setting.status} />
      <span className="config-status-source">
        <strong>{setting.source_label || "No source recorded"}</strong>
        <small>{setting.updated_at ? formatDate(setting.updated_at) : "No timestamp"}</small>
      </span>
      <FreshnessText state={setting.trust_state} />
    </li>
  );
}

function ManagedSecretRow({
  secret,
}: {
  secret: ProductManagedSecretConfigStatusItem;
}) {
  return (
    <li className="config-status-row" data-status={secret.status}>
      <span className="config-status-icon" aria-hidden="true">
        <ConfigStatusIcon status={secret.status} />
      </span>
      <span className="config-status-identity">
        <strong>{secret.binding_key}</strong>
        <small>{secret.integration || "Integration not recorded"}</small>
      </span>
      <ConfigStatusBadge status={secret.status} />
      <span className="config-status-source">
        <strong>{secret.updated_at ? "Binding metadata recorded" : "No binding metadata"}</strong>
        <small>{secret.updated_at ? formatDate(secret.updated_at) : "No timestamp"}</small>
      </span>
      <FreshnessText state={secret.trust_state} />
    </li>
  );
}

function ConfigStatusBadge({ status }: { status: ConfigStatus }) {
  return (
    <span className="config-status-badge" data-status={status}>
      {humanize(status)}
    </span>
  );
}

function FreshnessText({ state }: { state: string }) {
  const normalized = state === "disabled" ? "disabled" : state;
  return (
    <span className="config-trust" data-state={normalized}>
      <span aria-hidden="true" />
      {normalized === "disabled" ? "Disabled" : trustLabel(normalized as Parameters<typeof trustLabel>[0])}
    </span>
  );
}

function ConfigStatusIcon({ status }: { status: ConfigStatus }) {
  if (status === "configured") {
    return <CheckCircle2 />;
  }
  if (status === "missing" || status === "disabled") {
    return <AlertTriangle />;
  }
  return <ShieldQuestion />;
}

function RecordedSettingSources({ detail }: { detail: ProductEnvironmentDetail }) {
  return (
    <section className="recorded-config-panel">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Recorded authority</p>
          <h2>Runtime setting sources</h2>
        </div>
        <span>{detail.runtime_settings.length} sources</span>
      </div>
      {detail.runtime_settings.length ? (
        <ul className="recorded-config-list">
          {detail.runtime_settings.map((record, index) => (
            <li key={`${record.scope}:${record.source_label}:${index}`}>
              <div>
                <strong>{record.source_label || "Unnamed source"}</strong>
                <small>{humanize(record.scope)}</small>
              </div>
              <div>
                <strong>{record.env_value_count} keys recorded</strong>
                <small>{record.updated_at ? formatDate(record.updated_at) : "No timestamp"}</small>
              </div>
              <EvidenceBadge compact state={record.trust_state} />
              <details>
                <summary>Key names</summary>
                <ul>
                  {record.env_keys.map((key) => (
                    <li key={key}>
                      <code>{key}</code>
                    </li>
                  ))}
                </ul>
              </details>
            </li>
          ))}
        </ul>
      ) : (
        <MissingEvidenceState
          detail="No runtime-environment authority records were returned for this lane."
          title="No recorded runtime setting sources"
        />
      )}
    </section>
  );
}

function RecordedBindingsSummary({ detail }: { detail: ProductEnvironmentDetail }) {
  const configured = detail.managed_secrets.filter(
    (binding) => binding.status === "configured",
  ).length;
  const disabled = detail.managed_secrets.length - configured;
  return (
    <section className="recorded-config-panel">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Recorded authority</p>
          <h2>Managed-secret bindings</h2>
        </div>
        <span>{detail.managed_secrets.length} bindings</span>
      </div>
      {detail.managed_secrets.length ? (
        <div className="binding-summary">
          <div>
            <strong>{configured}</strong>
            <span>configured</span>
          </div>
          <div>
            <strong>{disabled}</strong>
            <span>disabled or unavailable</span>
          </div>
          <p>
            Binding IDs and secret record IDs remain in Diagnostics. Values are never
            returned.
          </p>
        </div>
      ) : (
        <MissingEvidenceState
          detail="No managed-secret binding metadata was returned for this lane."
          title="No recorded managed-secret bindings"
        />
      )}
    </section>
  );
}

function ConfigLoading() {
  return (
    <div className="config-loading" aria-live="polite">
      <span />
      <span />
      <span />
    </div>
  );
}

function ConfigEmpty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="config-empty">
      <ShieldQuestion size={20} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}
