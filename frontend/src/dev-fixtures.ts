import type {
  DataProvenance,
  GitHubHumanIdentityResponse,
  ProductEnvironmentSummary,
  ProductSiteOverview,
} from "./generated/openapi.ts";

type TrustState = ProductSiteOverview["trust_state"];

const OBSERVED_AT = "2026-07-14T14:32:00Z";
const STALE_AFTER = "2026-07-14T15:02:00Z";

export const fixtureIdentity: GitHubHumanIdentityResponse = {
  provider: "github",
  login: "operator-demo",
  github_id: 1001,
  name: "Demo Operator",
  email: "operator@example.invalid",
  organizations: ["example-operations"],
  teams: ["platform"],
  role: "admin",
};

export function productsForFixture(
  fixture: "products" | "empty" | "error" | "missing",
): ProductSiteOverview[] {
  if (fixture === "error") {
    throw new Error("The fixture product inventory is intentionally unavailable.");
  }
  if (fixture === "empty") {
    return [];
  }
  if (fixture === "missing") {
    return [missingEvidenceProduct];
  }
  return [atlasProduct, missingEvidenceProduct];
}

const atlasTesting = environmentFixture({
  environment: "testing",
  host: "testing.atlas.invalid",
  trustState: "verified",
  ingressStatus: "pass",
  ingressSummary: "Public health evidence passed at the expected endpoint.",
  tlsStatus: "valid",
  tlsSummary: "Certificate and presented hostname match the desired domain.",
});

const atlasProduction = environmentFixture({
  environment: "prod",
  host: "atlas.invalid",
  trustState: "verified",
  ingressStatus: "fail",
  ingressSummary: "Public TLS verification failed for the expected hostname.",
  tlsStatus: "hostname_mismatch",
  tlsSummary: "The presented certificate does not cover the desired public hostname.",
  warning: true,
});

const atlasProduct: ProductSiteOverview = {
  schema_version: 1,
  product: "atlas-commerce",
  display_name: "Atlas Commerce",
  repository: "example/atlas-commerce",
  driver_id: "generic-web",
  base_driver_id: "",
  environments: [atlasTesting, atlasProduction],
  preview: {
    enabled: true,
    context: "atlas-preview",
    slug_template: "pr-{number}",
    active_count: 3,
    latest_preview_id: "preview-example-3",
    trust_state: "recorded",
    provenance: provenance(
      "recorded",
      "Preview inventory was recorded by the lifecycle read model.",
    ),
  },
  warnings: ["Production public ingress does not match the expected hostname."],
  trust_state: "verified",
  provenance: provenance(
    "verified",
    "Product identity and stable lane summaries were refreshed from Launchplane evidence.",
  ),
  available_actions: [],
};

const missingEvidenceProduct: ProductSiteOverview = {
  schema_version: 1,
  product: "beacon-docs",
  display_name: "Beacon Docs",
  repository: "example/beacon-docs",
  driver_id: "generic-web",
  base_driver_id: "",
  environments: [
    missingEnvironmentFixture("testing", "testing.beacon.invalid"),
    missingEnvironmentFixture("prod", "beacon.invalid"),
  ],
  preview: {
    enabled: false,
    context: "",
    slug_template: "",
    active_count: 0,
    latest_preview_id: "",
    trust_state: "unsupported",
    provenance: provenance(
      "unsupported",
      "This product profile does not expose preview lifecycle capability.",
    ),
  },
  warnings: ["Stable environment evidence has not been recorded."],
  trust_state: "missing",
  provenance: provenance(
    "missing",
    "The product profile exists without current stable environment evidence.",
  ),
  available_actions: [],
};

function environmentFixture({
  environment,
  host,
  ingressStatus,
  ingressSummary,
  tlsStatus,
  tlsSummary,
  trustState,
  warning = false,
}: {
  environment: "testing" | "prod";
  host: string;
  ingressStatus: string;
  ingressSummary: string;
  tlsStatus:
    | "valid"
    | "expiring"
    | "expired"
    | "hostname_mismatch"
    | "untrusted"
    | "self_signed"
    | "unreachable"
    | "unknown"
    | "unsupported"
    | "missing";
  tlsSummary: string;
  trustState: TrustState;
  warning?: boolean;
}): ProductEnvironmentSummary {
  const baseUrl = `https://${host}`;
  const evidence = provenance(
    trustState,
    `The ${environment} environment summary was refreshed from Launchplane evidence.`,
  );
  return {
    environment,
    context: `atlas-${environment}`,
    base_url: baseUrl,
    health_url: `${baseUrl}/health`,
    trust_state: trustState,
    provenance: evidence,
    warnings: warning ? [ingressSummary] : [],
    available_actions: [],
    driver_extensions: { odoo: null },
    public_ingress: {
      status: ingressStatus,
      summary: ingressSummary,
      failure_code: warning ? "tls_hostname_mismatch" : "",
      incident_id: warning ? "incident-example" : "",
      incident_status: warning ? "open" : "",
      incident_opened_at: warning ? OBSERVED_AT : "",
      notification_sent: warning,
      observed_at: OBSERVED_AT,
      record_id: `ingress-${environment}-example`,
      trust_state: "verified",
      provenance: provenance("verified", ingressSummary),
    },
    topology: {
      trust_state: trustState,
      warnings: warning
        ? [
            {
              code: "tls_mismatch",
              detail: tlsSummary,
              domain_name: host,
              scope: "tls",
              severity: "error",
            },
            {
              code: "public_ingress_failure",
              detail: ingressSummary,
              domain_name: host,
              scope: "observation",
              severity: "error",
            },
          ]
        : [],
      desired: {
        base_url: baseUrl,
        health_url: `${baseUrl}/health`,
        domains: [{ domain_name: host, role: "primary", tls_expected: true }],
        trust_state: "recorded",
        provenance: provenance("recorded", "Desired topology comes from the product lane profile."),
      },
      provider_recorded: {
        authority_status: "active",
        domains: [{ domain_name: host, role: "primary", tls_expected: true }],
        ingress: {
          endpoint_key: `${environment}-endpoint`,
          path: "edge_to_provider",
          provider: "managed-runtime",
          termination_kind: "edge",
          trust_state: "recorded",
          provenance: provenance("recorded", "Ingress ownership is recorded by Launchplane."),
        },
        placement: {
          provider: "managed-runtime",
          provider_target_record_present: true,
          provider_target_type: "application",
          target_name: `${environment}-application`,
          target_type: "application",
          trust_state: "recorded",
          provenance: provenance("recorded", "Placement authority is recorded by Launchplane."),
        },
        tls: {
          owner: "launchplane",
          provider: "managed-runtime",
          terminator: "edge",
          trust_state: "recorded",
          provenance: provenance("recorded", "TLS ownership is recorded by Launchplane."),
        },
        trust_state: "recorded",
        provenance: provenance("recorded", "Provider topology authority is active."),
      },
      observed: {
        ingress: {
          failure_code: warning ? "tls_hostname_mismatch" : "",
          incident_id: warning ? "incident-example" : "",
          incident_status: warning ? "open" : "",
          observed_at: OBSERVED_AT,
          record_id: `ingress-${environment}-example`,
          status: ingressStatus,
          summary: ingressSummary,
          trust_state: "verified",
          provenance: provenance("verified", ingressSummary),
        },
        placement: {
          expected_runtime_identity: null,
          observed_runtime_identity: null,
          runtime_identity_detail: "Runtime identity was not included in this visual fixture.",
          runtime_identity_status: "unchecked",
          trust_state: "recorded",
          provenance: provenance("recorded", "Placement observation is recorded."),
        },
        tls_domains: [
          {
            domain_name: host,
            role: "primary",
            status: tlsStatus,
            summary: tlsSummary,
            likely_failure_cause: warning
              ? "Certificate coverage differs from the desired hostname."
              : "",
            failure_code: warning ? "hostname_mismatch" : "",
            incident_id: warning ? "incident-example" : "",
            incident_status: warning ? "open" : "",
            issuer: "Example Trust Services",
            subject: host,
            not_before: "2026-06-01T00:00:00Z",
            not_after: "2026-10-01T00:00:00Z",
            days_remaining: 79,
            observed_at: OBSERVED_AT,
            stale_after: STALE_AFTER,
            public_name_match: !warning,
            public_name_match_source: "certificate_san",
            presented_name_evidence: [host],
            presented_san_count: 1,
            validated_address_count: 1,
            record_id: `tls-${environment}-example`,
            trust_state: "verified",
            provenance: provenance("verified", tlsSummary),
          },
        ],
        trust_state: "verified",
      },
    },
  };
}

function missingEnvironmentFixture(
  environment: "testing" | "prod",
  host: string,
): ProductEnvironmentSummary {
  const missing = provenance(
    "missing",
    `No ${environment} operational evidence has been recorded.`,
  );
  return {
    environment,
    context: `beacon-${environment}`,
    base_url: `https://${host}`,
    health_url: `https://${host}/health`,
    trust_state: "missing",
    provenance: missing,
    warnings: ["Operational evidence is missing."],
    available_actions: [],
    driver_extensions: { odoo: null },
    public_ingress: {
      status: "",
      summary: "",
      failure_code: "",
      incident_id: "",
      incident_status: "",
      incident_opened_at: "",
      notification_sent: false,
      observed_at: "",
      record_id: "",
      trust_state: "missing",
      provenance: missing,
    },
    topology: {
      trust_state: "missing",
      warnings: [
        {
          code: "missing_route_authority",
          detail: "No active route authority is recorded for this stable lane.",
          domain_name: host,
          scope: "authority",
          severity: "error",
        },
      ],
      desired: {
        base_url: `https://${host}`,
        health_url: `https://${host}/health`,
        domains: [{ domain_name: host, role: "primary", tls_expected: true }],
        trust_state: "recorded",
        provenance: provenance("recorded", "Desired topology comes from the product lane profile."),
      },
      provider_recorded: {
        authority_status: "missing",
        domains: [],
        ingress: {
          endpoint_key: "",
          path: "unknown",
          provider: "",
          termination_kind: "unknown",
          trust_state: "missing",
          provenance: missing,
        },
        placement: {
          provider: "",
          provider_target_record_present: false,
          provider_target_type: "",
          target_name: "",
          target_type: "",
          trust_state: "missing",
          provenance: missing,
        },
        tls: {
          owner: "unknown",
          provider: "",
          terminator: "unknown",
          trust_state: "missing",
          provenance: missing,
        },
        trust_state: "missing",
        provenance: missing,
      },
      observed: {
        ingress: {
          failure_code: "",
          incident_id: "",
          incident_status: "",
          observed_at: "",
          record_id: "",
          status: "",
          summary: "",
          trust_state: "missing",
          provenance: missing,
        },
        placement: {
          expected_runtime_identity: null,
          observed_runtime_identity: null,
          runtime_identity_detail: "No runtime identity observation is recorded.",
          runtime_identity_status: "missing",
          trust_state: "missing",
          provenance: missing,
        },
        tls_domains: [],
        trust_state: "missing",
      },
    },
  };
}

function provenance(state: TrustState, detail: string): DataProvenance {
  const hasEvidence = state === "verified" || state === "recorded" || state === "stale";
  return {
    source_kind: state === "unsupported" ? "unsupported" : "record",
    source_record_id: hasEvidence ? `fixture-${state}` : "",
    recorded_at: hasEvidence ? OBSERVED_AT : "",
    refreshed_at: state === "verified" ? OBSERVED_AT : "",
    freshness_status: state,
    stale_after: hasEvidence ? STALE_AFTER : "",
    detail,
  };
}
