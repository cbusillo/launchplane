import type {
  DataProvenance,
  GitHubHumanIdentityResponse,
  ProductActionAvailability,
  ProductActivityReadModel,
  ProductEnvironmentConfigStatus,
  ProductEnvironmentDetail,
  ProductEnvironmentSummary,
  ProductSiteOverview,
  RuntimeIdentity,
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

export function environmentForFixture(
  fixture: "products" | "empty" | "error" | "missing",
  product: string,
  environment: string,
): ProductEnvironmentDetail | null {
  assertFixtureAvailable(fixture);
  const site = productsForFixture(fixture).find((candidate) => candidate.product === product);
  const summary = site?.environments.find(
    (candidate) => candidate.environment === environment,
  );
  if (!site || !summary) {
    return null;
  }
  const missingEvidence = site.trust_state === "missing";
  const expectedIdentity = missingEvidence
    ? null
    : runtimeIdentity(site.product, summary.context, environment, "expected");
  const observedIdentity = missingEvidence
    ? null
    : runtimeIdentity(site.product, summary.context, environment, "observed");
  const runtimeIdentityDetail = missingEvidence
    ? "No runtime identity evidence is recorded."
    : "Observed runtime identity matches the expected stable-lane artifact.";
  const runtimeIdentityStatus = missingEvidence ? "missing" : "match";
  const runtimeIdentityTrust = missingEvidence ? "missing" : "verified";
  return {
    schema_version: 1,
    product: site.product,
    display_name: site.display_name,
    repository: site.repository,
    driver_id: site.driver_id,
    base_driver_id: site.base_driver_id,
    environment: summary.environment,
    context: summary.context,
    base_url: summary.base_url,
    health_url: summary.health_url,
    trust_state: summary.trust_state,
    provenance: summary.provenance,
    warnings: summary.warnings,
    available_actions: summary.available_actions,
    driver_extensions: summary.driver_extensions,
    public_ingress: summary.public_ingress,
    topology: {
      ...summary.topology,
      observed: {
        ...summary.topology.observed,
        placement: {
          expected_runtime_identity: expectedIdentity,
          observed_runtime_identity: observedIdentity,
          runtime_identity_detail: runtimeIdentityDetail,
          runtime_identity_status: runtimeIdentityStatus,
          trust_state: runtimeIdentityTrust,
          provenance: provenance(runtimeIdentityTrust, runtimeIdentityDetail),
        },
      },
    },
    target: {
      target_name: missingEvidence ? "" : `${site.product}-${environment}`,
      target_type: missingEvidence ? "" : "application",
      provider: missingEvidence ? "" : "managed-runtime",
      provider_target_type: missingEvidence ? "" : "application",
      target_id_recorded: !missingEvidence,
      artifact_manifest: null,
      expected_runtime_identity: expectedIdentity,
      observed_runtime_identity: observedIdentity,
      runtime_identity_status: runtimeIdentityStatus,
      runtime_identity_detail: runtimeIdentityDetail,
      trust_state: runtimeIdentityTrust,
    },
    runtime_settings: missingEvidence
      ? []
      : [
          {
            scope: "instance",
            context: summary.context,
            instance: environment,
            env_keys: ["LOG_LEVEL", "PUBLIC_ORIGIN"],
            env_value_count: 2,
            source_label: "stable lane profile",
            updated_at: OBSERVED_AT,
            trust_state: "recorded",
          },
        ],
    managed_secrets: missingEvidence
      ? []
      : [
          {
            binding_id: `binding-${environment}-smtp-example`,
            secret_id: "secret-smtp-example",
            integration: "runtime_environment",
            binding_type: "env",
            binding_key: "SMTP_PASSWORD",
            context: summary.context,
            instance: environment,
            status: "configured",
            updated_at: OBSERVED_AT,
            trust_state: "recorded",
          },
        ],
  };
}

export function configStatusForFixture(
  fixture: "products" | "empty" | "error" | "missing",
  product: string,
  environment: string,
): ProductEnvironmentConfigStatus | null {
  assertFixtureAvailable(fixture);
  const detail = environmentForFixture(fixture, product, environment);
  if (!detail) {
    return null;
  }
  const missingEvidence = detail.trust_state === "missing";
  return {
    schema_version: 1,
    product: detail.product,
    display_name: detail.display_name,
    repository: detail.repository,
    driver_id: detail.driver_id,
    base_driver_id: detail.base_driver_id,
    environment: detail.environment,
    context: detail.context,
    trust_state: detail.trust_state,
    provenance: detail.provenance,
    warnings: detail.warnings,
    runtime_settings: [
      {
        key: "PUBLIC_ORIGIN",
        status: missingEvidence ? "missing" : "configured",
        context: detail.context,
        instance: environment,
        source_label: missingEvidence ? "" : "stable lane profile",
        updated_at: missingEvidence ? "" : OBSERVED_AT,
        trust_state: missingEvidence ? "missing" : "recorded",
      },
      {
        key: "ANALYTICS_WRITE_KEY",
        status: "missing",
        context: detail.context,
        instance: environment,
        source_label: "",
        updated_at: "",
        trust_state: "missing",
      },
    ],
    managed_secrets: [
      {
        binding_key: "SMTP_PASSWORD",
        integration: "runtime_environment",
        status: missingEvidence ? "missing" : "configured",
        context: detail.context,
        instance: environment,
        updated_at: missingEvidence ? "" : OBSERVED_AT,
        trust_state: missingEvidence ? "missing" : "recorded",
      },
      {
        binding_key: "ANALYTICS_TOKEN",
        integration: "runtime_environment",
        status: "missing",
        context: detail.context,
        instance: environment,
        updated_at: "",
        trust_state: "missing",
      },
    ],
  };
}

export function activityForFixture(
  fixture: "products" | "empty" | "error" | "missing",
  product: string,
): ProductActivityReadModel | null {
  assertFixtureAvailable(fixture);
  const site = productsForFixture(fixture).find((candidate) => candidate.product === product);
  if (!site) {
    return null;
  }
  if (site.trust_state === "missing") {
    return {
      schema_version: 1,
      product: site.product,
      display_name: site.display_name,
      repository: site.repository,
      driver_id: site.driver_id,
      events: [],
    };
  }
  return {
    schema_version: 1,
    product: site.product,
    display_name: site.display_name,
    repository: site.repository,
    driver_id: site.driver_id,
    events: [
      {
        event_id: "event-tls-example",
        event_type: "public_ingress_incident",
        product: site.product,
        context: "atlas-prod",
        environment: "prod",
        driver_id: site.driver_id,
        action_id: "public_ingress_probe",
        title: "Production TLS verification failed",
        summary: "The certificate presented at the public endpoint did not cover the desired hostname.",
        status: "fail",
        occurred_at: "2026-07-14T14:32:00Z",
        trust_state: "verified",
        records: [{ record_type: "public_ingress_incident", record_id: "incident-example" }],
      },
      {
        event_id: "event-deploy-example",
        event_type: "deployment",
        product: site.product,
        context: "atlas-testing",
        environment: "testing",
        driver_id: site.driver_id,
        action_id: "stable_deploy",
        title: "Testing deployment verified",
        summary: "The testing environment reported the expected public health evidence.",
        status: "pass",
        occurred_at: "2026-07-14T13:08:00Z",
        trust_state: "verified",
        records: [{ record_type: "deployment", record_id: "deployment-example" }],
      },
      {
        event_id: "event-authz-example",
        event_type: "authz_policy",
        product: site.product,
        context: "launchplane",
        environment: "",
        driver_id: "launchplane",
        action_id: "authz_policy.update",
        title: "Product read policy recorded",
        summary: "Launchplane recorded updated product read authority for the stable lanes.",
        status: "recorded",
        occurred_at: "2026-07-13T20:15:00Z",
        trust_state: "recorded",
        records: [{ record_type: "authz_policy", record_id: "policy-example" }],
      },
    ],
  };
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
  available_actions: actionsForEnvironment("prod"),
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
    available_actions: actionsForEnvironment(environment),
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

function actionsForEnvironment(
  environment: "testing" | "prod",
): ProductActionAvailability[] {
  return [
    actionFixture({
      actionId: "prod_promotion",
      description:
        "Promote a generic-web testing image to prod and record promotion health evidence.",
      label: "Promote testing to prod",
      routePath: "/v1/drivers/generic-web/prod-promotion",
      safety: "mutation",
    }),
    actionFixture({
      actionId: "prod_promotion_workflow",
      description:
        "Dispatch the product-owned GitHub workflow that promotes testing to prod.",
      disabledReasons: ["Caller is not authorized for this action."],
      enabled: false,
      label: "Dispatch promote workflow",
      routePath: "/v1/drivers/generic-web/prod-promotion-workflow",
      safety: "mutation",
    }),
    actionFixture({
      actionId: "stable_deploy",
      description:
        "Deploy an immutable container image to a configured generic-web product lane.",
      label: `Deploy ${environment} lane`,
      routePath: "/v1/drivers/generic-web/deploy",
      safety: "mutation",
    }),
    actionFixture({
      actionId: "preview_readiness",
      description:
        "Validate generic-web preview template settings before provider mutation.",
      label: "Evaluate preview readiness",
      routePath: "/v1/drivers/generic-web/preview-readiness",
      safety: "read",
      scope: "context",
    }),
    actionFixture({
      actionId: "prod_rollback",
      description:
        "Revalidate and apply a generic-web rollback by deploying a previous immutable artifact.",
      label: "Apply prod rollback",
      routePath: "/v1/drivers/generic-web/prod-rollback",
      safety: "destructive",
    }),
  ];
}

function actionFixture({
  actionId,
  description,
  disabledReasons = [],
  enabled = true,
  label,
  routePath,
  safety,
  scope = "instance",
}: {
  actionId: string;
  description: string;
  disabledReasons?: string[];
  enabled?: boolean;
  label: string;
  routePath: string;
  safety: string;
  scope?: string;
}): ProductActionAvailability {
  return {
    action_id: actionId,
    alternate_authz_actions: [],
    authz_action: `fixture.${actionId}`,
    description,
    disabled_reasons: disabledReasons,
    enabled,
    label,
    method: "POST",
    route_path: routePath,
    safety,
    scope,
    trust_state: "recorded",
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

function runtimeIdentity(
  product: string,
  context: string,
  environment: string,
  kind: "expected" | "observed",
): RuntimeIdentity {
  return {
    schema_version: 1,
    product,
    context,
    instance: environment,
    environment_kind: "stable",
    artifact_id: `${product}-artifact-v17`,
    image_reference: `registry.example.invalid/${product}@sha256:example`,
    source_git_ref: "refs/heads/main",
    release_tuple_id: `${product}-release-v17`,
    deployment_record_id: `${kind}-deployment-example`,
    deployed_at: OBSERVED_AT,
    preview_id: "",
    preview_generation_id: "",
  };
}

function assertFixtureAvailable(
  fixture: "products" | "empty" | "error" | "missing",
): void {
  if (fixture === "error") {
    throw new Error("The fixture product inventory is intentionally unavailable.");
  }
}
