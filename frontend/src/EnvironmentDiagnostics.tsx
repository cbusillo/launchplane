import { Braces, ShieldAlert } from "lucide-react";

import { EvidenceBadge, humanize } from "./ProductOps";
import type { ResourceState } from "./resource";

import type {
  DataProvenance,
  ProductEnvironmentConfigStatus,
  ProductEnvironmentDetail,
  RuntimeIdentity,
} from "./generated/openapi.ts";

export function EnvironmentDiagnostics({
  configResource,
  detail,
}: {
  configResource: ResourceState<ProductEnvironmentConfigStatus>;
  detail: ProductEnvironmentDetail;
}) {
  return (
    <section className="environment-diagnostics">
      <header className="diagnostics-route-header">
        <span aria-hidden="true">
          <Braces />
        </span>
        <div>
          <p className="eyebrow">Diagnostics</p>
          <h2>Technical evidence and identifiers</h2>
          <p>
            Contexts, provider metadata, record IDs, and route paths are isolated
            here so ordinary product operation does not depend on provider plumbing.
          </p>
        </div>
      </header>

      <div className="diagnostics-safety-note">
        <ShieldAlert size={18} aria-hidden="true" />
        <p>
          Secret values and runtime-setting values are not returned by these read
          models. Only key names and binding metadata appear below.
        </p>
      </div>

      <DiagnosticSection title="Product and lane routing">
        <DiagnosticGrid
          items={[
            ["Product key", detail.product],
            ["Context", detail.context],
            ["Environment", detail.environment],
            ["Driver", detail.driver_id],
            ["Base driver", detail.base_driver_id || "none"],
            ["Repository", detail.repository || "missing"],
            ["Base URL", detail.base_url || "missing"],
            ["Health URL", detail.health_url || "missing"],
          ]}
        />
      </DiagnosticSection>

      <DiagnosticSection title="Provider-recorded topology">
        <DiagnosticGrid
          items={[
            ["Authority status", detail.topology.provider_recorded.authority_status],
            ["Provider", detail.target.provider || "missing"],
            ["Provider target type", detail.target.provider_target_type || "missing"],
            ["Target name", detail.target.target_name || "missing"],
            ["Target type", detail.target.target_type || "missing"],
            ["Target ID recorded", detail.target.target_id_recorded ? "yes" : "no"],
            ["Ingress endpoint key", detail.topology.provider_recorded.ingress.endpoint_key || "missing"],
            ["Ingress provider", detail.topology.provider_recorded.ingress.provider || "missing"],
            ["Ingress path", detail.topology.provider_recorded.ingress.path],
            ["TLS provider", detail.topology.provider_recorded.tls.provider || "missing"],
            ["TLS owner", detail.topology.provider_recorded.tls.owner],
            ["TLS terminator", detail.topology.provider_recorded.tls.terminator],
          ]}
        />
      </DiagnosticSection>

      <DiagnosticSection title="Runtime identities">
        <RuntimeIdentityBlock
          identity={detail.target.expected_runtime_identity}
          label="Expected runtime identity"
        />
        <RuntimeIdentityBlock
          identity={detail.target.observed_runtime_identity}
          label="Observed runtime identity"
        />
      </DiagnosticSection>

      <DiagnosticSection title="Provenance records">
        <ul className="provenance-diagnostic-list">
          <ProvenanceDiagnostic label="Environment" provenance={detail.provenance} />
          <ProvenanceDiagnostic
            label="Desired topology"
            provenance={detail.topology.desired.provenance}
          />
          <ProvenanceDiagnostic
            label="Provider-recorded topology"
            provenance={detail.topology.provider_recorded.provenance}
          />
          <ProvenanceDiagnostic
            label="Public ingress"
            provenance={detail.public_ingress.provenance}
          />
        </ul>
      </DiagnosticSection>

      <DiagnosticSection title="Advertised action routes">
        {detail.available_actions.length ? (
          <ul className="diagnostic-action-routes">
            {detail.available_actions.map((action) => (
              <li key={`${action.action_id}:${action.route_path}`}>
                <div>
                  <strong>{action.label}</strong>
                  <span>{humanize(action.safety)} · {action.enabled ? "enabled" : "blocked"}</span>
                </div>
                <code>{action.method} {action.route_path}</code>
                {action.disabled_reasons.length ? (
                  <small>{action.disabled_reasons.join("; ")}</small>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="diagnostic-empty">No environment action routes were advertised.</p>
        )}
      </DiagnosticSection>

      <DiagnosticSection title="Configuration authority metadata">
        {configResource.data ? (
          <DiagnosticGrid
            items={[
              ["Config context", configResource.data.context],
              ["Config environment", configResource.data.environment],
              ["Config trust", configResource.data.trust_state],
              ["Config source record", configResource.data.provenance.source_record_id || "missing"],
            ]}
          />
        ) : (
          <p className="diagnostic-empty">
            {configResource.status === "error"
              ? `Configuration diagnostics unavailable: ${configResource.error}`
              : "Configuration diagnostics are loading."}
          </p>
        )}
        <h3>Managed-secret record identifiers</h3>
        {detail.managed_secrets.length ? (
          <ul className="diagnostic-binding-list">
            {detail.managed_secrets.map((binding) => (
              <li key={binding.binding_id || `${binding.integration}:${binding.binding_key}`}>
                <strong>{binding.binding_key}</strong>
                <code>{binding.binding_id || "binding id missing"}</code>
                <code>{binding.secret_id || "secret id missing"}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="diagnostic-empty">No managed-secret binding records were returned.</p>
        )}
      </DiagnosticSection>
    </section>
  );
}

function DiagnosticSection({
  children,
  title,
}: {
  children: React.ReactNode;
  title: string;
}) {
  return (
    <section className="environment-diagnostic-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function DiagnosticGrid({ items }: { items: Array<[string, string]> }) {
  return (
    <dl className="environment-diagnostic-grid">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function RuntimeIdentityBlock({
  identity,
  label,
}: {
  identity: RuntimeIdentity | null;
  label: string;
}) {
  return (
    <details className="runtime-identity-diagnostic">
      <summary>{label}</summary>
      {identity ? (
        <DiagnosticGrid
          items={[
            ["Artifact ID", identity.artifact_id],
            ["Image reference", identity.image_reference],
            ["Source Git ref", identity.source_git_ref],
            ["Release tuple ID", identity.release_tuple_id],
            ["Deployment record ID", identity.deployment_record_id],
            ["Deployed at", identity.deployed_at],
            ["Context", identity.context],
            ["Instance", identity.instance],
          ]}
        />
      ) : (
        <p className="diagnostic-empty">No runtime identity record was returned.</p>
      )}
    </details>
  );
}

function ProvenanceDiagnostic({
  label,
  provenance,
}: {
  label: string;
  provenance: DataProvenance;
}) {
  return (
    <li>
      <div>
        <strong>{label}</strong>
        <small>{provenance.detail || "No provenance detail"}</small>
      </div>
      <EvidenceBadge compact state={provenance.freshness_status} />
      <code>{provenance.source_record_id || "source record missing"}</code>
    </li>
  );
}
