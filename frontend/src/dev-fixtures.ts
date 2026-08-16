import type { OwnerAcceptanceDecision } from "./api";
import type {
  ApplyProductEnvironmentConfigData,
  DataProvenance,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  EveryCodeSummaryResponse,
  GitHubHumanIdentityResponse,
  GovernanceProjectionResponse,
  MergeAdmissionRecord,
  MergeLandingOutcomeRecord,
  MergeReadinessResult,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
  OwnerAcceptanceQueueResponse,
  OwnerAcceptanceProductDecision,
  ProductActionAvailability,
  ProductActivityReadModel,
  ProductEnvironmentConfigStatus,
  ProductEnvironmentIncidentList,
  ProductConfigApplyResponse,
  ProductConfigWriteAvailability,
  ProductEnvironmentDetail,
  ProductEnvironmentSummary,
  ProductIncidentDetail,
  ProductIncidentSummary,
  ProductOperationalReadiness,
  ProductOperationalReadinessDimension,
  ProductPromotionDryRunResponse,
  ProductPromotionOperationAvailability,
  ProductPromotionStatus,
  ProductPromotionWorkflowDeliveryStatusResponse,
  ProductPromotionWorkflowDispatchResponse,
  ProductSiteOverview,
  RuntimeIdentity,
  TenantAdmissionEvaluationReadResponse,
  TenantAdmissionPathResult,
  TenantAdmissionTechnicalChecks,
  WorkGraphIssueInboxResponse,
  WorkGraphQueue,
  WorkGraphSnapshotResponse,
} from "./generated/openapi.ts";

type TrustState = ProductSiteOverview["trust_state"];
type DataFixtureMode = "products" | "empty" | "error" | "missing" | "denied";
type EngineeringLoadReason = "initial" | "refresh";

const OBSERVED_AT = "2026-07-14T14:32:00Z";
const STALE_AFTER = "2026-07-14T15:02:00Z";
const promotionDeliveries = new Map<string, number>();

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

export function assertEngineeringRefreshAvailable(
  reason: EngineeringLoadReason,
): void {
  if (
    reason === "refresh" &&
    new URLSearchParams(window.location.search).get("refresh") === "error"
  ) {
    throw Object.assign(
      new Error("The engineering fixture refresh is intentionally unavailable."),
      {
        statusCode: 503,
        traceId: "fixture-engineering-refresh-error",
      },
    );
  }
}

export async function waitForEngineeringFixture(
  signal: AbortSignal,
): Promise<void> {
  if (new URLSearchParams(window.location.search).get("delay") !== "slow") {
    return;
  }
  if (signal.aborted) {
    throw new DOMException("Engineering fixture request cancelled.", "AbortError");
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Engineering fixture request cancelled.", "AbortError"));
    };
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, 1800);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export async function waitForReadinessFixture(
  signal: AbortSignal,
): Promise<void> {
  if (new URLSearchParams(window.location.search).get("readiness") !== "slow") {
    return;
  }
  if (signal.aborted) {
    throw new DOMException("Readiness fixture request cancelled.", "AbortError");
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Readiness fixture request cancelled.", "AbortError"));
    };
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, 1800);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function productsForFixture(
  fixture: DataFixtureMode,
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
  fixture: DataFixtureMode,
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
    health_monitoring: summary.health_monitoring,
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

export function incidentsForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
): ProductEnvironmentIncidentList | null {
  const detail = environmentForFixture(fixture, product, environment);
  if (!detail) {
    return null;
  }
  const incidentMode = new URLSearchParams(window.location.search).get("incident");
  if (incidentMode === "error") {
    throw new Error("Incident history is intentionally unavailable.");
  }
  const activeIncident =
    incidentMode === "empty" || incidentMode === "stale"
      ? null
      : incidentSummaryForFixture(detail);
  const resolvedIncident = activeIncident
    ? {
        ...activeIncident,
        incident_id: `${activeIncident.incident_id}-resolved`,
        status: "resolved" as const,
        severity: "warning" as const,
        opened_at: "2026-07-13T09:00:00Z",
        latest_observed_at: "2026-07-13T10:15:00Z",
        resolved_at: "2026-07-13T10:15:00Z",
        resolution_reason: "recovered" as const,
        latest_material_event_id: "incident-event-resolved-example",
        latest_material_event: "resolved" as const,
        latest_material_event_at: "2026-07-13T10:15:00Z",
        next_reminder_at: "",
        last_reminded_at: "2026-07-13T09:45:00Z",
        consecutive_recovery_observations: 2,
        summary: "Public ingress recovered after the required observation threshold.",
      }
    : null;
  return {
    product: detail.product,
    display_name: detail.display_name,
    environment: detail.environment,
    context: detail.context,
    instance: detail.environment,
    incidents: [activeIncident, resolvedIncident].filter(
      (incident): incident is ProductIncidentSummary => incident !== null,
    ),
    trust_state:
      incidentMode === "stale" ? "stale" : detail.health_monitoring.trust_state,
    provenance: provenance(
      incidentMode === "stale" ? "stale" : detail.health_monitoring.trust_state,
      activeIncident
        ? "Launchplane incident history is available for this environment."
        : incidentMode === "stale"
          ? "The last incident history refresh is stale and must not be treated as current."
        : "Launchplane recorded no public ingress incidents for this environment.",
    ),
  };
}

export function incidentForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
  incidentId: string,
): ProductIncidentDetail | null {
  const incidentList = incidentsForFixture(fixture, product, environment);
  const incident = incidentList?.incidents.find(
    (candidate) => candidate.incident_id === incidentId,
  );
  if (!incident) {
    return null;
  }
  const resolved = incident.status === "resolved";
  const eventId = incident.latest_material_event_id || `event-${incident.incident_id}`;
  const observationId = resolved
    ? `observation-${incident.incident_id}-resolved`
    : `observation-${incident.incident_id}-latest`;
  return {
    incident,
    material_evidence: {
      check_kind: incident.check_kind,
      failure_code: incident.failure_code,
      failure_layer: incident.failure_layer,
      severity: incident.severity,
      affected_targets: ["health_url"],
      route_authority_kind: "public_urls",
      runtime_identity_status: "",
      runtime_identity_mismatched_fields: [],
      tls_status: "",
    },
    observations: [
      {
        record_id: observationId,
        purpose: "probe",
        observed_at: incident.latest_observed_at,
        status: resolved ? "pass" : "fail",
        failure_code: resolved ? "" : incident.failure_code,
        summary: resolved
          ? "The public path passed the required recovery observation threshold."
          : incident.summary,
        incident_event_id: eventId,
        material_fingerprint_sha256: incident.material_fingerprint_sha256,
        notification_sent: !resolved,
      },
      ...(!resolved
        ? [
            {
              record_id: `observation-${incident.incident_id}-opened`,
              purpose: "probe" as const,
              observed_at: incident.opened_at,
              status: "fail" as const,
              failure_code: incident.failure_code,
              summary: "The first durable failing observation opened this incident.",
              incident_event_id: eventId,
              material_fingerprint_sha256: incident.material_fingerprint_sha256,
              notification_sent: true,
            },
          ]
        : []),
    ],
    events: [
      {
        event_id: eventId,
        event: incident.latest_material_event || (resolved ? "resolved" : "opened"),
        reason: resolved ? "recovered" : "incident_opened",
        occurred_at: incident.latest_material_event_at || incident.opened_at,
        observation_id: observationId,
        severity: incident.severity,
        material_fingerprint_sha256: incident.material_fingerprint_sha256,
        previous_material_fingerprint_sha256: "",
        delivery_eligible: true,
        suppression_reason: "",
        reminder_window_index: null,
        reminder_window_started_at: "",
        reminder_window_ended_at: "",
        summary: resolved
          ? "Launchplane resolved the incident after the recovery threshold."
          : "Launchplane opened a material public ingress incident.",
      },
    ],
    reminders: incident.next_reminder_at
      ? [
          {
            reminder_state_id: `reminder-${incident.incident_id}`,
            status: "active",
            material_event_id: eventId,
            interval_seconds: 21600,
            last_window_index: 0,
            last_reminded_at: incident.last_reminded_at,
            next_reminder_at: incident.next_reminder_at,
            updated_at: incident.latest_observed_at,
          },
        ]
      : [],
    notification_attempts: [
      {
        attempt_id: `attempt-${incident.incident_id}`,
        event: resolved ? "resolved" : "opened",
        destination_kind: "github_issue",
        delivery_status: "delivered",
        attempted_at: incident.latest_material_event_at || incident.opened_at,
        observation_id: observationId,
        incident_event_id: eventId,
        external_url: "https://github.com/example/example/issues/42",
        action: resolved ? "closed" : "created",
        error_message: "",
      },
    ],
    outbox_deliveries: [
      {
        delivery_id: `outbox-${incident.incident_id}`,
        state: "delivered",
        created_at: incident.latest_material_event_at || incident.opened_at,
        updated_at: incident.latest_material_event_at || incident.opened_at,
        next_attempt_at: incident.latest_material_event_at || incident.opened_at,
        attempt: 1,
        max_attempts: 6,
        external_url: "https://github.com/example/example/issues/42",
        action: resolved ? "closed" : "created",
        error_code: "",
      },
    ],
  };
}

export function operationalReadinessForFixture(
  fixture: DataFixtureMode,
  detail: ProductEnvironmentDetail,
  action: ProductActionAvailability,
): ProductOperationalReadiness {
  assertFixtureAvailable(fixture);
  const mode = new URLSearchParams(window.location.search).get("readiness");
  if (mode === "error") {
    throw Object.assign(
      new Error("Operational readiness is intentionally unavailable."),
      { statusCode: 503, traceId: "fixture-readiness-unavailable" },
    );
  }
  if (mode === "denied") {
    throw Object.assign(
      new Error("This browser session cannot read operational readiness evidence."),
      { statusCode: 403, traceId: "fixture-readiness-denied" },
    );
  }
  const unsupported = mode === "unsupported";
  const workflowCaller = mode === "ready";
  const dimensions = unsupported
    ? unsupportedReadinessDimensions(detail)
    : readinessDimensions(detail, workflowCaller);
  const nonReadyDimensions = dimensions
    .filter((dimension) => dimension.state !== "ready")
    .map((dimension) => dimension.dimension);
  return {
    schema_version: 1,
    generated_at: OBSERVED_AT,
    product: detail.product,
    display_name: detail.display_name,
    repository: detail.repository,
    driver_id: detail.driver_id,
    context: detail.context,
    instance: detail.environment,
    caller: workflowCaller
      ? {
          identity_type: "github_actions",
          repository: "example-org/launchplane",
          repository_id: "1001",
          repository_owner_id: "2001",
          workflow_ref:
            "example-org/launchplane/.github/workflows/example-action.yml@refs/heads/main",
          job_workflow_ref:
            `example-org/launchplane/.github/workflows/reusable-example-action.yml@${"a".repeat(40)}`,
        }
      : {
          identity_type: "github_human",
          repository: "",
          repository_id: "",
          repository_owner_id: "",
          workflow_ref: "",
          job_workflow_ref: "",
        },
    action: {
      requested_action: action.authz_action,
      action_id: action.action_id,
      label: action.label,
      safety: action.safety,
      scope: action.scope,
      method: action.method,
      route_path: action.route_path,
      supported: !unsupported,
      readiness_requirements: unsupported
        ? []
        : [
            "provider_target",
            "route_binding",
            "runtime_environment",
            "managed_secrets",
            "artifact",
            "deployment",
            "topology",
          ],
    },
    state: unsupported
      ? "unsupported"
      : nonReadyDimensions.length
        ? "blocked"
        : "ready",
    ready: nonReadyDimensions.length === 0,
    dimensions,
    non_ready_dimensions: nonReadyDimensions,
  };
}

function unsupportedReadinessDimensions(
  detail: ProductEnvironmentDetail,
): ProductOperationalReadinessDimension[] {
  return [
    {
      dimension: "product_lane",
      state: "ready",
      required: true,
      summary: "The product profile owns the requested context and instance.",
      owner_record_type: "product_profile_record",
      evidence: [
        {
          record_type: "product_profile_record",
          record_id: detail.product,
          revision: null,
          recorded_at: OBSERVED_AT,
          freshness_status: "recorded",
        },
      ],
      details: [],
      remediation: null,
    },
    {
      dimension: "action",
      state: "unsupported",
      required: true,
      summary:
        "The requested action has no instance-scoped operational readiness contract.",
      owner_record_type: "driver_descriptor",
      evidence: [],
      details: [],
      remediation: {
        action: "driver.read",
        method: "GET",
        route_path: "/v1/drivers",
        summary:
          "Choose an instance-scoped driver action with declared readiness requirements.",
      },
    },
    {
      dimension: "authorization",
      state: "unsupported",
      required: true,
      summary:
        "Authorization readiness cannot be evaluated for an unsupported action.",
      owner_record_type: "authz_policy_record",
      evidence: [],
      details: [],
      remediation: null,
    },
  ];
}

function readinessDimensions(
  detail: ProductEnvironmentDetail,
  workflowCaller: boolean,
): ProductOperationalReadinessDimension[] {
  const evidence = (
    recordType: string,
    recordId: string,
    freshnessStatus: "verified" | "recorded" = "recorded",
  ) => ({
    record_type: recordType,
    record_id: recordId,
    revision: null,
    recorded_at: OBSERVED_AT,
    freshness_status: freshnessStatus,
  });
  const ready = (
    dimension: ProductOperationalReadinessDimension["dimension"],
    summary: string,
    ownerRecordType: string,
    recordId = "",
    details: string[] = [],
  ): ProductOperationalReadinessDimension => ({
    dimension,
    state: "ready",
    required: true,
    summary,
    owner_record_type: ownerRecordType,
    evidence: recordId ? [evidence(ownerRecordType, recordId)] : [],
    details,
    remediation: null,
  });
  return [
    ready(
      "product_lane",
      "The product profile owns the requested context and instance.",
      "product_profile_record",
      detail.product,
    ),
    ready(
      "action",
      "The product driver declares operational readiness requirements for the action.",
      "driver_descriptor",
      `${detail.driver_id}:fixture-action`,
    ),
    workflowCaller
      ? ready(
          "authorization",
          "The active policy authorizes the immutable workflow caller for this exact lane and action.",
          "authz_policy_record",
          "fixture-authz-policy-r285",
        )
      : {
          dimension: "authorization",
          state: "blocked",
          required: true,
          summary:
            "Operational workflow readiness requires an authenticated GitHub Actions caller.",
          owner_record_type: "authz_policy_record",
          evidence: [evidence("authz_policy_record", "fixture-authz-policy-r285")],
          details: [],
          remediation: {
            action: "authz_policy.manage",
            method: "POST",
            route_path: "/v1/authz-policies/managed-rule-sets/reconcile",
            summary:
              "Reconcile one exact managed workflow grant through the protected service workflow.",
          },
        },
    ready(
      "provider_target",
      "Launchplane has recorded provider-target authority for this lane.",
      "provider_target_record",
      "fixture-provider-target",
    ),
    ready(
      "route_binding",
      "Provider-neutral route authority is active and current.",
      "environment_route_binding_record",
      "fixture-route-binding",
      workflowCaller ? [] : ["external_ingress_internals_unsupported"],
    ),
    ready(
      "runtime_environment",
      "All declared runtime-environment keys are recorded for this lane.",
      "runtime_environment_record",
    ),
    ready(
      "managed_secrets",
      "All declared managed-secret bindings are recorded and enabled.",
      "secret_binding_record",
    ),
    ready(
      "artifact",
      "The exact requested artifact manifest is persisted in Launchplane.",
      "artifact_manifest",
      detail.target.expected_runtime_identity?.artifact_id || "fixture-artifact",
    ),
    workflowCaller
      ? ready(
          "deployment",
          "Current deployment, health, and runtime-identity evidence are passing.",
          "deployment_record",
          "fixture-deployment-current",
        )
      : {
          dimension: "deployment",
          state: "blocked",
          required: true,
          summary:
            "Current deployment, health, or runtime-identity evidence is not passing.",
          owner_record_type: "deployment_record",
          evidence: [evidence("deployment_record", "fixture-deployment-current")],
          details: ["runtime_identity_status:unchecked"],
          remediation: null,
        },
    {
      ...ready(
        "topology",
        "Recorded and observed route, runtime, HTTP, and TLS topology evidence is current.",
        "product_environment_topology",
      ),
      evidence: [
        evidence("environment_route_binding_record", "fixture-route-binding"),
        evidence(
          "public_ingress_observation_record",
          "fixture-ingress-observation",
          "verified",
        ),
      ],
    },
  ];
}

export function configStatusForFixture(
  fixture: DataFixtureMode,
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
    write_availability: productConfigWriteAvailabilityForFixture(
      detail.product,
      detail.environment,
      !missingEvidence,
    ),
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

export function promotionStatusForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
): ProductPromotionStatus | null {
  assertFixtureAvailable(fixture);
  const detail = environmentForFixture(fixture, product, environment);
  if (!detail || environment !== "prod") {
    return null;
  }
  const promotionMode = new URLSearchParams(window.location.search).get("promotion");
  if (promotionMode === "error") {
    throw Object.assign(new Error("Promotion authority is intentionally unavailable."), {
      statusCode: 503,
      traceId: "fixture-promotion-status-error",
    });
  }
  const missingEvidence = detail.trust_state === "missing";
  const staleEvidence = promotionMode === "stale";
  const sourceArtifact = missingEvidence
    ? ""
    : `ghcr.io/example/atlas-commerce@sha256:${"a".repeat(64)}`;
  const sourceGitRef = missingEvidence ? "" : "1".repeat(40);
  const blockers = missingEvidence
    ? [
        "Current testing inventory is unavailable.",
        "Testing inventory does not contain generated runtime identity evidence.",
        "Current production inventory is unavailable.",
        "Production provider target authority is unavailable.",
      ]
    : staleEvidence
      ? ["Testing inventory evidence is stale."]
      : [];
  const workflowAction = detail.available_actions.find(
    (action) => action.action_id === "prod_promotion_workflow",
  );
  const workflowBlockers = [
    ...blockers,
    ...(workflowAction?.enabled
      ? []
      : workflowAction?.disabled_reasons.length
        ? workflowAction.disabled_reasons
        : ["Promotion workflow availability was not recorded."]),
  ];
  const direct = promotionAvailability(
    "direct_dry_run",
    "generic_web_prod_promotion.execute",
    blockers,
  );
  const workflowDryRun = promotionAvailability(
    "workflow_dry_run",
    "generic_web_prod_promotion.dispatch",
    workflowBlockers,
    true,
  );
  const workflowLive = promotionAvailability(
    "workflow_live",
    "generic_web_prod_promotion.dispatch",
    workflowBlockers,
    true,
    true,
  );
  const confirmation = (bump: "patch" | "minor" | "major") =>
    `PROMOTE ${detail.product} ${sourceArtifact} ${sourceGitRef} TO prod BUMP ${bump} CREATE RELEASE TAG AND DEPLOY PRODUCTION`;
  return {
    schema_version: 1,
    product: detail.product,
    display_name: detail.display_name,
    driver_id: detail.driver_id,
    base_driver_id: detail.base_driver_id,
    repository: detail.repository,
    workflow_id: "promote-prod.yml",
    workflow_ref: "main",
    context: detail.context,
    source_environment: "testing",
    destination_environment: "prod",
    source: {
      environment: "testing",
      artifact_id: sourceArtifact,
      deploy_reference: `ghcr.io/example/atlas-commerce:sha-${sourceGitRef}`,
      source_git_ref: sourceGitRef,
      deployment_record_id: missingEvidence ? "" : "deployment-fixture-testing",
      inventory_updated_at: missingEvidence ? "" : OBSERVED_AT,
      inventory_stale_after: missingEvidence ? "" : STALE_AFTER,
      deployment_status: missingEvidence ? "skipped" : "pass",
      health_status: missingEvidence ? "skipped" : "pass",
      runtime_identity_status: missingEvidence ? "missing" : "match",
      runtime_identity_detail: missingEvidence
        ? "Runtime identity is unavailable."
        : "Runtime identity matches the recorded testing deployment.",
      trust_state: missingEvidence ? "missing" : staleEvidence ? "stale" : "verified",
    },
    destination: {
      environment: "prod",
      artifact_id: missingEvidence
        ? ""
        : `ghcr.io/example/atlas-commerce@sha256:${"b".repeat(64)}`,
      deploy_reference: missingEvidence
        ? ""
        : `ghcr.io/example/atlas-commerce:sha-${"2".repeat(40)}`,
      source_git_ref: missingEvidence ? "" : "2".repeat(40),
      deployment_record_id: missingEvidence ? "" : "deployment-fixture-prod",
      inventory_updated_at: missingEvidence ? "" : OBSERVED_AT,
      inventory_stale_after: missingEvidence ? "" : STALE_AFTER,
      deployment_status: missingEvidence ? "skipped" : "pass",
      health_status: missingEvidence ? "skipped" : "pass",
      runtime_identity_status: missingEvidence ? "missing" : "match",
      runtime_identity_detail: missingEvidence
        ? "Runtime identity is unavailable."
        : "Runtime identity matches the recorded production deployment.",
      trust_state: missingEvidence ? "missing" : "verified",
    },
    evidence_fingerprint: missingEvidence
      ? "fixture-promotion-missing-evidence"
      : staleEvidence
        ? "fixture-promotion-stale-evidence"
        : "fixture-promotion-current-evidence",
    default_bump: "patch",
    bump_options: ["patch", "minor", "major"],
    direct_dry_run: direct,
    workflow_dry_run: workflowDryRun,
    workflow_live: workflowLive,
    live_confirmations: {
      patch: confirmation("patch"),
      minor: confirmation("minor"),
      major: confirmation("major"),
    },
    trust_state: missingEvidence ? "missing" : staleEvidence ? "stale" : "verified",
  };
}

export async function dryRunProductPromotionForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
  payload: DryRunProductPromotionData["body"],
  signal?: AbortSignal,
): Promise<ProductPromotionDryRunResponse> {
  const status = promotionStatusForFixture(fixture, product, environment);
  await waitForPromotionFixture(signal);
  if (!status || !status.direct_dry_run.enabled) {
    throw Object.assign(new Error("Current fixture evidence blocks promotion dry-run."), {
      statusCode: 409,
      traceId: "fixture-promotion-dry-run-blocked",
    });
  }
  if (payload.evidence_fingerprint !== status.evidence_fingerprint) {
    throw Object.assign(new Error("Promotion evidence changed."), {
      statusCode: 409,
      traceId: "fixture-promotion-evidence-changed",
    });
  }
  return {
    status: "accepted",
    trace_id: "fixture-promotion-direct-dry-run",
    records: {
      backup_record_id: "",
      backup_status: "skipped",
      promotion_record_id: "promotion-fixture-dry-run",
      deployment_record_id: "",
      deployment_status: "skipped",
      destination_health_status: "pending",
      dry_run: "true",
      inventory_record_id: "",
      promotion_status: "pending",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      source_health_status: "pending",
    },
    replayed: false,
    result: {
      product: status.product,
      context: status.context,
      from_instance: status.source_environment,
      to_instance: status.destination_environment,
      artifact_id: status.source.artifact_id,
      deploy_reference: status.source.deploy_reference,
      source_git_ref: status.source.source_git_ref,
      backup_record_id: "",
      promotion_record_id: "promotion-fixture-dry-run",
      promotion_status: "pending",
      deployment_status: "skipped",
      backup_status: "skipped",
      source_health_status: "pending",
      destination_health_status: "pending",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      deployment_record_id: "",
      inventory_record_id: "",
      target_name: "",
      target_category: "unknown",
      provider_target_type: "",
      provider_id: "",
      target_id: "",
      dry_run: true,
      error_message: "",
      evidence_fingerprint: status.evidence_fingerprint,
      bump: payload.bump ?? status.default_bump,
    },
  };
}

export async function dispatchProductPromotionWorkflowForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
  payload: DispatchProductPromotionWorkflowData["body"],
  signal?: AbortSignal,
): Promise<ProductPromotionWorkflowDispatchResponse> {
  const status = promotionStatusForFixture(fixture, product, environment);
  await waitForPromotionFixture(signal);
  if (!status || payload.evidence_fingerprint !== status.evidence_fingerprint) {
    throw Object.assign(new Error("Promotion evidence changed."), {
      statusCode: 409,
      traceId: "fixture-promotion-evidence-changed",
    });
  }
  const bump = payload.bump ?? status.default_bump;
  const dryRun = payload.dry_run ?? true;
  if (!dryRun && payload.confirmation !== status.live_confirmations[bump]) {
    throw Object.assign(new Error("Live confirmation did not match."), {
      statusCode: 400,
      traceId: "fixture-promotion-confirmation-required",
    });
  }
  const deliveryId = `fixture-promotion-${dryRun ? "dry-run" : "live"}-${bump}`;
  promotionDeliveries.set(deliveryId, 0);
  return {
    status: "accepted",
    trace_id: `fixture-promotion-workflow-${dryRun ? "dry-run" : "live"}`,
    records: { outbox_delivery_id: deliveryId },
    replayed: false,
    result: {
      product: status.product,
      context: status.context,
      repository: status.repository,
      workflow_id: status.workflow_id,
      ref: status.workflow_ref,
      dry_run: dryRun,
      bump,
      dispatch_status: "pending",
      run_id: 0,
      run_url: "",
      run_status: "pending",
      run_conclusion: "",
      evidence_fingerprint: status.evidence_fingerprint,
      delivery_id: deliveryId,
      artifact_id: status.source.artifact_id,
      source_git_ref: status.source.source_git_ref,
    },
  };
}

export async function readProductPromotionWorkflowDeliveryForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
  deliveryId: string,
): Promise<ProductPromotionWorkflowDeliveryStatusResponse> {
  const status = promotionStatusForFixture(fixture, product, environment);
  if (
    status &&
    !promotionDeliveries.has(deliveryId) &&
    /^fixture-promotion-(dry-run|live)-(patch|minor|major)$/.test(deliveryId)
  ) {
    promotionDeliveries.set(deliveryId, 0);
  }
  if (!status || !promotionDeliveries.has(deliveryId)) {
    throw Object.assign(new Error("Promotion workflow delivery was not found."), {
      statusCode: 404,
      traceId: "fixture-promotion-delivery-missing",
    });
  }
  const observation = promotionDeliveries.get(deliveryId) ?? 0;
  promotionDeliveries.set(deliveryId, observation + 1);
  const observed = observation > 0;
  return {
    status: "ok",
    trace_id: "fixture-promotion-delivery-status",
    delivery: {
      delivery_id: deliveryId,
      state: observed ? "delivered" : "pending",
      dispatch_status: observed ? "dispatched" : "pending",
      run_id: observed ? 4242 : 0,
      run_url: observed ? "https://github.com/example/atlas-commerce/actions/runs/4242" : "",
      run_status: observed ? "queued" : "pending",
      run_conclusion: "",
      run_observation_status: observed ? "observed" : "pending",
      error_code: "",
      observed_at: OBSERVED_AT,
    },
  };
}

function promotionAvailability(
  operation: ProductPromotionOperationAvailability["operation"],
  authzAction: string,
  blockers: string[],
  requiresMatchingDirectDryRun = false,
  requiresConfirmation = false,
): ProductPromotionOperationAvailability {
  return {
    operation,
    authz_action: authzAction,
    enabled: blockers.length === 0,
    disabled_reasons: blockers,
    requires_reason: true,
    requires_idempotency_key: true,
    requires_matching_direct_dry_run: requiresMatchingDirectDryRun,
    requires_confirmation: requiresConfirmation,
    consequences:
      operation === "direct_dry_run"
        ? ["No production deployment or release is created."]
        : ["Dispatch acceptance does not mean the GitHub run completed."],
    trust_state: blockers.length ? "missing" : "verified",
  };
}

async function waitForPromotionFixture(signal?: AbortSignal): Promise<void> {
  if (new URLSearchParams(window.location.search).get("delay") !== "slow") {
    return;
  }
  if (signal?.aborted) {
    throw new DOMException("Promotion fixture request cancelled.", "AbortError");
  }
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(resolve, 4000);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Promotion fixture request cancelled.", "AbortError"));
      },
      { once: true },
    );
  });
}

export async function applyProductEnvironmentConfigForFixture(
  fixture: DataFixtureMode,
  product: string,
  environment: string,
  payload: ApplyProductEnvironmentConfigData["body"],
  signal?: AbortSignal,
): Promise<ProductConfigApplyResponse> {
  assertFixtureAvailable(fixture);
  if (signal?.aborted) {
    throw new DOMException("Product config fixture request cancelled.", "AbortError");
  }
  const detail = environmentForFixture(fixture, product, environment);
  if (!detail || detail.trust_state === "missing") {
    throw Object.assign(
      new Error("Product configuration writes are unavailable without recorded lane authority."),
      { statusCode: 409, traceId: "fixture-product-config-blocked" },
    );
  }
  const runtimeKeys = Object.keys(payload.runtime_settings ?? {}).sort();
  const secretInputs = payload.managed_secrets ?? [];
  const nextActions = runtimeKeys.length
    ? [
        {
          kind: "live_target_runtime_apply" as const,
          required: true,
          status: "live_sync_required" as const,
          target: {
            context: detail.context,
            instance: detail.environment,
            target_type: "application",
            target_name: `${detail.product}-${detail.environment}`,
          },
          changed_keys: runtimeKeys,
          dry_run: {
            method: "POST" as const,
            endpoint: "/v1/live-target-runtime/apply",
            mode: "dry-run" as const,
          },
          apply: {
            method: "POST" as const,
            endpoint: "/v1/live-target-runtime/apply",
            mode: "apply" as const,
          },
          instruction:
            "Inspect the separately typed live-target runtime operation before synchronization.",
        },
      ]
    : [];
  return {
    status: "accepted",
    trace_id: `fixture-product-config-${payload.mode}`,
    records: {},
    replayed: false,
    result: {
      status:
        payload.mode === "apply" && nextActions.length
          ? "records_applied_live_sync_required"
          : "ok",
      mode: payload.mode,
      product: detail.product,
      context: detail.context,
      instance: detail.environment,
      actor: "github:operator-demo",
      source_label: "product-environment-api",
      reason: payload.reason ?? "",
      runtime_environment: {
        action: runtimeKeys.length ? "updated" : "skipped",
        scope: "instance",
        context: detail.context,
        instance: detail.environment,
        keys: runtimeKeys,
        changed_keys: runtimeKeys,
        unchanged_keys: [],
        env_value_count_after: runtimeKeys.length,
      },
      runtime_key_safety: {
        required: secretInputs.length > 0,
        status: secretInputs.length ? "pass" : "skipped",
        policy_record_id: secretInputs.length ? "fixture-runtime-key-safety" : "",
        policy_sha256: secretInputs.length ? "fixture-runtime-key-safety-sha256" : "",
        checked_binding_keys: secretInputs.map((secret) => secret.binding_key),
        findings: [],
      },
      secrets: secretInputs.map((secret) => ({
        action: "rotated",
        scope: "context_instance",
        integration: "runtime_environment",
        name: secret.binding_key,
        binding_key: secret.binding_key,
        context: detail.context,
        instance: detail.environment,
        secret_id: `fixture-${secret.binding_key.toLowerCase()}`,
      })),
      summary: {
        runtime_changed_key_count: runtimeKeys.length,
        secret_change_count: secretInputs.length,
      },
      next_actions: nextActions,
    },
  };
}

function productConfigWriteAvailabilityForFixture(
  product: string,
  environment: string,
  enabled: boolean,
): ProductConfigWriteAvailability {
  const disabledReasons = enabled
    ? []
    : ["Recorded product environment authority is unavailable."];
  const operation = (mode: "dry-run" | "apply") => ({
    mode,
    authz_action: mode === "dry-run" ? "product_config.plan" : "product_config.apply",
    method: "POST" as const,
    route_path: "/v1/products/{product}/environments/{environment}/config/apply",
    enabled,
    disabled_reasons: disabledReasons,
    requires_reason: true,
    requires_idempotency_key: true,
    requires_matching_dry_run: mode === "apply",
    confirmation_text: mode === "apply" ? `APPLY ${product}/${environment}` : "",
    trust_state: enabled ? ("recorded" as const) : ("missing" as const),
  });
  return {
    runtime_settings: {
      input_kind: "runtime_settings",
      plan: operation("dry-run"),
      apply: operation("apply"),
      consequences: [
        "Dry-run and apply expose key names and counts, not runtime values.",
        "Live target synchronization remains a separate inspect-only step when advertised.",
      ],
    },
    managed_secrets: {
      input_kind: "managed_secrets",
      plan: operation("dry-run"),
      apply: operation("apply"),
      consequences: [
        "Managed-secret creation or rotation cannot restore prior plaintext.",
        "Live target synchronization remains a separate inspect-only step when advertised.",
      ],
    },
  };
}

export function activityForFixture(
  fixture: DataFixtureMode,
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

export function workGraphForFixture(fixture: DataFixtureMode): {
  queue: WorkGraphQueue;
  rankTraceId: string;
  snapshotResponse: WorkGraphSnapshotResponse;
} {
  assertEngineeringFixtureAvailable(fixture);
  const snapshotResponse = engineeringWorkGraphSnapshot();
  if (fixture === "empty") {
    snapshotResponse.snapshot.issues = [];
    return {
      snapshotResponse,
      rankTraceId: "",
      queue: {
        generated_at: OBSERVED_AT,
        hidden_count: 0,
        items: [],
        schema_version: 1,
      },
    };
  }
  const missingEvidence = fixture === "missing";
  return {
    snapshotResponse,
    rankTraceId: "fixture-work-graph-rank",
    queue: {
      generated_at: OBSERVED_AT,
      hidden_count: missingEvidence ? 1 : 0,
      items: missingEvidence
        ? [
            engineeringWorkGraphItem({
              evidenceState: "missing",
              number: 308,
              recommendation: "attention_needed",
              safeToStart: false,
              state: "blocked",
              title: "Restore missing dependency evidence",
            }),
          ]
        : [
            engineeringWorkGraphItem({
              evidenceState: "verified",
              number: 308,
              recommendation: "quick_win",
              safeToStart: true,
              state: "ready",
              title: "Finish the operator evidence route",
            }),
            engineeringWorkGraphItem({
              evidenceState: "recorded",
              number: 311,
              recommendation: "deep_work",
              safeToStart: true,
              state: "ready",
              title: "Split controller orchestration boundaries",
            }),
            engineeringWorkGraphItem({
              evidenceState: "stale",
              number: 319,
              recommendation: "watch",
              safeToStart: false,
              state: "waiting",
              title: "Refresh external check evidence",
            }),
          ],
      schema_version: 1,
    },
  };
}

export function issueInboxForFixture(
  fixture: DataFixtureMode,
): WorkGraphIssueInboxResponse {
  assertEngineeringFixtureAvailable(fixture);
  if (fixture === "empty") {
    return {
      configured: true,
      inbox: {
        generated_at: OBSERVED_AT,
        issue_count: 0,
        project_configured: true,
        repositories: [
          {
            issue_count: 0,
            issues: [],
            missing_from_project_count: 0,
            present_in_project_count: 0,
            repository: "example/control-plane",
          },
        ],
        repository_count: 1,
        schema_version: 1,
        stale_project_item_count: 0,
      },
      status: "ok",
      trace_id: "fixture-issue-inbox-empty",
    };
  }
  const projectConfigured = fixture !== "missing";
  const issues = [
    {
      author: "platform-operator",
      created_at: "2026-07-13T08:00:00Z",
      key: "example/control-plane#308",
      labels: ["plan:active", "frontend"],
      number: 308,
      present_in_project: projectConfigured ? true : null,
      project_status: projectConfigured ? ("present" as const) : ("unconfigured" as const),
      repository: "example/control-plane",
      state: "open",
      title: "Finish the operator evidence route",
      updated_at: OBSERVED_AT,
      url: "https://example.invalid/example/control-plane/issues/308",
    },
    {
      author: "release-operator",
      created_at: "2026-07-12T09:15:00Z",
      key: "example/control-plane#319",
      labels: ["plan:next"],
      number: 319,
      present_in_project: projectConfigured ? false : null,
      project_status: projectConfigured ? ("missing" as const) : ("unconfigured" as const),
      repository: "example/control-plane",
      state: "open",
      title: "Refresh external check evidence",
      updated_at: "2026-07-14T10:10:00Z",
      url: "https://example.invalid/example/control-plane/issues/319",
    },
  ];
  return {
    configured: true,
    inbox: {
      generated_at: OBSERVED_AT,
      issue_count: issues.length,
      project_configured: projectConfigured,
      repositories: [
        {
          issue_count: issues.length,
          issues,
          missing_from_project_count: projectConfigured ? 1 : 0,
          present_in_project_count: projectConfigured ? 1 : 0,
          repository: "example/control-plane",
        },
      ],
      repository_count: 1,
      schema_version: 1,
      stale_project_item_count: 0,
    },
    status: "ok",
    trace_id: "fixture-issue-inbox",
  };
}

export function everyCodeForFixture(
  fixture: DataFixtureMode,
): EveryCodeSummaryResponse {
  assertEngineeringFixtureAvailable(fixture);
  const summaries =
    fixture === "empty"
      ? []
      : fixture === "missing"
        ? [
            engineeringEveryCodeSummary({
              freshness: "missing",
              issueNumber: 319,
              state: "blocked",
              summaryStatus: "stuck",
              title: "Refresh external check evidence",
            }),
          ]
        : [
            engineeringEveryCodeSummary({
              freshness: "verified",
              issueNumber: 308,
              state: "running",
              summaryStatus: "active",
              title: "Finish the operator evidence route",
            }),
            engineeringEveryCodeSummary({
              freshness: "recorded",
              issueNumber: 302,
              state: "done",
              summaryStatus: "complete",
              title: "Publish generated API contracts",
            }),
          ];
  return {
    status: "ok",
    summary: {
      generated_at: OBSERVED_AT,
      issue_number: null,
      repository: "",
      schema_version: 1,
      state_filter: "",
      summaries,
    },
    trace_id: "fixture-every-code-summary",
  };
}

export function mergeTrainTargetsForFixture(
  fixture: DataFixtureMode,
): MergeTrainPolicyTargetsResponse {
  assertEngineeringFixtureAvailable(fixture);
  return {
    policy: {
      policy_sha256: "fixture-policy-sha256",
      record_id: "fixture-merge-train-policy",
      updated_at: OBSERVED_AT,
    },
    status: "ok",
    targets:
      fixture === "empty"
        ? []
        : [
            {
              base_branch: "main",
              policy_key: "example/control-plane:main",
              repository: "example/control-plane",
              scheduler: {
                enabled: true,
                mutate: false,
                runner_mode: "controller",
              },
              service_authz: {
                action: "merge_train.run_once",
                context: "launchplane",
                product: "launchplane",
              },
            },
            {
              base_branch: "main",
              policy_key: "example/runtime-site:main",
              repository: "example/runtime-site",
              scheduler: {
                enabled: false,
                mutate: false,
                runner_mode: "level1",
              },
              service_authz: {
                action: "merge_train.run_once",
                context: "launchplane",
                product: "launchplane",
              },
            },
          ],
    trace_id: "fixture-merge-train-targets",
  };
}

export function mergeTrainStatusForFixture(
  fixture: DataFixtureMode,
  repository: string,
  baseBranch: string,
): MergeTrainControllerStatusResponse {
  assertEngineeringFixtureAvailable(fixture);
  const reconciliationRequired = fixture === "missing";
  return {
    controller_status: {
      admission: {
        base_branch: baseBranch,
        controller_action: reconciliationRequired
          ? "reconcile_required"
          : "observe_candidate",
        controller_candidate_record_id: "fixture-candidate-record",
        controller_landing_plan_record_id: "",
        controller_reason: reconciliationRequired
          ? "Stored controller evidence requires operator reconciliation."
          : "Candidate checks remain pending.",
        controller_stack_collapse_plan_record_id: "",
        detail: reconciliationRequired
          ? "The expected candidate SHA no longer matches provider evidence."
          : "Waiting for the required checks on the current candidate.",
        latest_run_id: "fixture-run-27",
        latest_run_recorded_at: OBSERVED_AT,
        latest_run_status: "waiting",
        next_allowed_at: "2026-07-14T14:34:00Z",
        reason_code: "poll_interval_pending",
        repository,
        requested_at: OBSERVED_AT,
        schema_version: 1,
        status: "deferred",
      },
      base_branch: baseBranch,
      controller_diagnostics: {
        active_action: reconciliationRequired ? "reconcile_required" : "observe_candidate",
        active_phase: reconciliationRequired ? "candidate_reconcile" : "candidate_observe",
        active_pull_request_number: 418,
        active_record_id: "fixture-candidate-record",
        heartbeat_age_seconds: reconciliationRequired ? 900 : 24,
        lease_age_seconds: reconciliationRequired ? 1200 : 80,
        lease_expires_at: "2026-07-14T14:37:00Z",
        owner: reconciliationRequired ? "" : "controller-fixture",
        reconciliation_detail: reconciliationRequired
          ? "operator_required: expected candidate SHA changed"
          : "",
        reconciliation_status: reconciliationRequired ? "required" : "clean",
        status: reconciliationRequired ? "expired" : "active",
      },
      controller_records: [
        {
          batch_id: "fixture-batch-27",
          blocked_count: reconciliationRequired ? 1 : 0,
          candidate_sha: "fixture-candidate-sha",
          merged_count: 0,
          planned_count: 2,
          policy_key: `${repository}:${baseBranch}`,
          policy_sha256: "fixture-policy-sha256",
          policy_status: reconciliationRequired ? "stale" : "current",
          pull_request_numbers: [418, 421],
          record_id: "fixture-candidate-record",
          record_type: "batch_candidate",
          required_checks_status: reconciliationRequired ? "unknown" : "pending",
          skipped_count: 0,
          stale_count: reconciliationRequired ? 1 : 0,
          stale_reason: reconciliationRequired
            ? "Candidate evidence no longer matches the active policy."
            : "",
          status: reconciliationRequired ? "reconcile_required" : "waiting",
          updated_at: OBSERVED_AT,
        },
      ],
      controller_state: {
        active_action: reconciliationRequired ? "reconcile_required" : "observe_candidate",
        active_phase: reconciliationRequired ? "candidate_reconcile" : "candidate_observe",
        active_pull_request_number: 418,
        active_record_id: "fixture-candidate-record",
        base_branch: baseBranch,
        controller_key: `${repository}:${baseBranch}`,
        heartbeat_at: OBSERVED_AT,
        last_action: "build_candidate",
        last_owner: "controller-fixture",
        last_phase: "candidate_build",
        last_pull_request_number: 418,
        last_record_id: "fixture-candidate-record",
        last_transition_at: OBSERVED_AT,
        lease_acquired_at: "2026-07-14T14:30:40Z",
        lease_expires_at: "2026-07-14T14:37:00Z",
        lease_owner: reconciliationRequired ? "" : "controller-fixture",
        policy_key: `${repository}:${baseBranch}`,
        policy_sha256: "fixture-policy-sha256",
        reconciliation_detail: reconciliationRequired
          ? "operator_required: expected candidate SHA changed"
          : "",
        reconciliation_status: reconciliationRequired ? "required" : "clean",
        repository,
        schema_version: 1,
        status: reconciliationRequired ? "reconcile_required" : "running",
        step_payload: {},
        updated_at: OBSERVED_AT,
      },
      current_policy_key: `${repository}:${baseBranch}`,
      current_policy_sha256: "fixture-policy-sha256",
      generated_at: OBSERVED_AT,
      latest_dry_run: {
        eligible_count: 1,
        intended_next_action: reconciliationRequired
          ? "reconcile_required"
          : "observe_candidate",
        next_action_detail: reconciliationRequired
          ? "Repair repository or policy evidence before retrying."
          : "Observe required checks for the current candidate.",
        queue_count: 2,
        queue_entries: [
          {
            eligible: true,
            ineligible_reasons: [],
            mergeable: "mergeable",
            pull_request_number: 418,
            required_checks_status: "pending",
            title: "Finish the operator evidence route",
            url: "https://example.invalid/example/control-plane/pull/418",
          },
          {
            eligible: false,
            ineligible_reasons: ["required checks are pending"],
            mergeable: "unknown",
            pull_request_number: 421,
            required_checks_status: "pending",
            title: "Split controller orchestration boundaries",
            url: "https://example.invalid/example/control-plane/pull/421",
          },
        ],
        selected_pr_number: 418,
      },
      latest_run: {
        base_branch: baseBranch,
        dry_run_result: {},
        intended_next_action: "observe_candidate",
        mode: "dry_run",
        policy_key: `${repository}:${baseBranch}`,
        policy_sha256: "fixture-policy-sha256",
        poll_required: true,
        recorded_at: OBSERVED_AT,
        repository,
        reread_required: true,
        run_id: "fixture-run-27",
        schema_version: 1,
        selected_head_sha: "fixture-head-sha",
        selected_mergeable: "mergeable",
        selected_pr_number: 418,
        selected_required_checks_status: "pending",
        snapshot: {},
        status: "waiting",
        trace_id: "fixture-merge-train-run",
        worker_step_result: null,
      },
      repository,
      schema_version: 1,
    },
    status: "ok",
    trace_id: "fixture-merge-train-status",
  };
}

export function tenantAdmissionForFixture(
  fixture: DataFixtureMode,
): TenantAdmissionEvaluationReadResponse {
  assertEngineeringFixtureAvailable(fixture);
  const classificationMissing = fixture === "empty";
  const evidenceStale = fixture === "missing";
  const classificationDigest = classificationMissing ? "" : "c".repeat(64);
  const pathState = evidenceStale ? "stale" : "pending";
  const admissionCategory = classificationMissing
    ? "unavailable"
    : evidenceStale
      ? "stale"
      : "pending";
  const technicalStatus: TenantAdmissionTechnicalChecks["status"] = evidenceStale
    ? "pending"
    : "pass";
  const candidate = {
    context: "example-site",
    head_sha: "a".repeat(40),
    product: "example-site",
    pull_request_number: 69,
    repository: "example/tenant-site",
    repository_id: "1001",
    repository_owner_id: "2001",
    schema_version: 1,
  };
  const paths = classificationMissing
    ? {
        manager_preview_approval: null,
        schema_version: 1,
        technical_human_waiver: null,
        trusted_maintenance: null,
      }
    : {
        manager_preview_approval: tenantAdmissionPath(
          "manager_preview_approval",
          pathState,
          classificationDigest,
        ),
        schema_version: 1,
        technical_human_waiver: tenantAdmissionPath(
          "technical_human_waiver",
          pathState,
          classificationDigest,
        ),
        trusted_maintenance: tenantAdmissionPath(
          "trusted_maintenance",
          pathState,
          classificationDigest,
        ),
      };
  const technicalChecks = classificationMissing
    ? null
    : {
        base_sha: "b".repeat(40),
        base_up_to_date: true,
        binding_sha256: "d".repeat(64),
        evaluated_at: OBSERVED_AT,
        excluded_contexts: ["manager-preview-approval", "tenant-admission"],
        head_sha: candidate.head_sha,
        required_checks: [
          { app_id: 100, name: "ci-gate" },
          { app_id: 101, name: "security-gate" },
        ],
        schema_version: 1,
        signals: [
          {
            app_id: 100,
            name: "ci-gate",
            source: "check_run" as const,
            state: technicalStatus,
          },
          {
            app_id: 101,
            name: "security-gate",
            source: "check_run" as const,
            state: "pass" as const,
          },
        ],
        status: technicalStatus,
        strict: true,
      };
  return {
    read_model: {
      agent_authoring_allowed: false,
      evaluation: {
        admission: {
          category: admissionCategory,
          classification_digest: classificationDigest,
          classification_kind: classificationMissing ? "" : "tenant_ui",
          classification_revision: classificationMissing ? 0 : 1,
          classification_status: classificationMissing ? "missing" : "available",
          decision: {
            classification_digest: classificationDigest,
            classification_kind: classificationMissing ? "" : "tenant_ui",
            classification_revision: classificationMissing ? 0 : 1,
            context: candidate.context,
            decision_binding_sha256: "e".repeat(64),
            detail: classificationMissing
              ? "No repository classification record is available for this GitHub repository ID."
              : evidenceStale
                ? "Tenant admission evidence is stale for this exact head."
                : "Tenant UI repository requires one current admission path.",
            evaluated_at: OBSERVED_AT,
            evidence_digest: "",
            evidence_id: "",
            evidence_kind: evidenceStale ? "manager_preview_approval" : "none",
            head_sha: candidate.head_sha,
            product: candidate.product,
            pull_request_number: candidate.pull_request_number,
            reason_code: classificationMissing
              ? "classification_missing"
              : evidenceStale
                ? "evidence_stale"
                : "manager_preview_required",
            repository: candidate.repository,
            repository_id: candidate.repository_id,
            repository_owner_id: candidate.repository_owner_id,
            schema_version: 1,
            status: "blocked",
          },
          generated_at: OBSERVED_AT,
          paths,
          schema_version: 1,
        },
        base_branch: "main",
        candidate,
        detail: classificationMissing
          ? "Tenant admission is unavailable for the exact current head."
          : `Tenant admission is ${admissionCategory} and technical checks are ${technicalStatus} for the exact current head.`,
        merge_commit_sha: "",
        merge_method: "merge",
        mutated: false,
        outcome: "blocked",
        pull_request_facts: {
          base_branch: "main",
          base_sha: "b".repeat(40),
          draft: false,
          head_sha: candidate.head_sha,
          merge_commit_sha: "",
          mergeable: true,
          merged: false,
          pull_request_number: candidate.pull_request_number,
          pull_request_url: "https://example.invalid/example/tenant-site/pull/69",
          repository: candidate.repository,
          state: "open",
        },
        schema_version: 1,
        technical_checks: technicalChecks,
      },
      generated_at: OBSERVED_AT,
      human_actions: classificationMissing
        ? []
        : [
            {
              action_kind: "manager_preview_approval",
              agent_authoring_allowed: false,
              availability: "available",
              detail: evidenceStale
                ? "The prior manager approval is stale; review and approve the current preview fingerprint."
                : "The manager can review the current preview and approve its exact fingerprint.",
              path_state: pathState,
              requires_human: true,
              schema_version: 1,
              title: "Manager preview approval",
            },
            {
              action_kind: "technical_human_waiver",
              agent_authoring_allowed: false,
              availability: "available",
              detail: evidenceStale
                ? "The prior waiver is stale; an authorized repository owner can review the current head."
                : "An authorized repository owner can issue a reasoned waiver for this exact head.",
              path_state: pathState,
              requires_human: true,
              schema_version: 1,
              title: "Repository-owner technical waiver",
            },
          ],
      schema_version: 1,
    },
    status: "ok",
    trace_id: "fixture-tenant-admission",
  };
}

function tenantAdmissionPath(
  pathKind: TenantAdmissionPathResult["path_kind"],
  state: "pending" | "stale",
  classificationDigest: string,
): TenantAdmissionPathResult {
  return {
    classification_digest: classificationDigest,
    evidence_digest: "",
    evidence_id: "",
    head_sha: "a".repeat(40),
    path_kind: pathKind,
    pull_request_number: 69,
    repository: "example/tenant-site",
    repository_id: "1001",
    repository_owner_id: "2001",
    schema_version: 1,
    state,
  };
}

function engineeringWorkGraphSnapshot(): WorkGraphSnapshotResponse {
  return {
    snapshot: {
      generated_at: OBSERVED_AT,
      issues: [
        engineeringIssueSnapshot(308, "Finish the operator evidence route", "Now"),
        engineeringIssueSnapshot(311, "Split controller orchestration boundaries", "Next"),
        engineeringIssueSnapshot(319, "Refresh external check evidence", "Waiting"),
      ],
      repos: [
        {
          classification: "managed_runtime",
          display_name: "Example Control Plane",
          product: "example-control-plane",
          repository: "example/control-plane",
        },
      ],
      schema_version: 1,
    },
    source: {
      every_code_requests: 2,
      planning_facts: 3,
      product_repositories: 1,
      project_configured: true,
    },
    status: "ok",
    trace_id: "fixture-work-graph-snapshot",
  };
}

function engineeringIssueSnapshot(
  number: number,
  title: string,
  focus: "Now" | "Next" | "Waiting",
) {
  return {
    blocked_by: focus === "Waiting" ? 1 : 0,
    blocking: 0,
    check_state: focus === "Waiting" ? ("unknown" as const) : ("success" as const),
    deploy_state: "unknown" as const,
    finish_line: "The route has independent evidence, refresh, and failure states.",
    focus,
    is_pull_request: false,
    labels: [`plan:${focus.toLowerCase()}`],
    manager: "Code",
    number,
    repository: "example/control-plane",
    state: "open" as const,
    subissues_completed: 0,
    subissues_total: 0,
    title,
    updated_at: OBSERVED_AT,
    url: `https://example.invalid/example/control-plane/issues/${number}`,
  };
}

function engineeringWorkGraphItem({
  evidenceState,
  number,
  recommendation,
  safeToStart,
  state,
  title,
}: {
  evidenceState: "verified" | "recorded" | "stale" | "missing";
  number: number;
  recommendation: "quick_win" | "deep_work" | "attention_needed" | "watch";
  safeToStart: boolean;
  state: "ready" | "waiting" | "blocked";
  title: string;
}) {
  return {
    blocked_by_count: state === "blocked" ? 1 : 0,
    evidence: [
      {
        code: "project_status",
        detail: "Launchplane engineering fixture evidence from compact planning facts.",
        source_url: `https://example.invalid/example/control-plane/issues/${number}`,
        state: evidenceState,
      },
    ],
    finish_line: "The route has independent evidence, refresh, and failure states.",
    focus: state === "waiting" ? ("Waiting" as const) : ("Now" as const),
    handoff_url: `https://example.invalid/example/control-plane/issues/${number}`,
    manager: "Code",
    next_action: safeToStart
      ? "Open the source issue and continue the scoped implementation."
      : "Inspect the blocker and refresh source evidence.",
    number,
    product: "example-control-plane",
    product_display_name: "Example Control Plane",
    reasons: [
      {
        code: recommendation,
        detail: safeToStart
          ? "The issue is active and has no recorded blocker."
          : "The issue needs fresh dependency or check evidence.",
      },
    ],
    recommendation,
    repo_classification: "managed_runtime" as const,
    repository: "example/control-plane",
    safe_to_start: safeToStart,
    score: safeToStart ? 88 : 41,
    source_of_truth_url: `https://example.invalid/example/control-plane/issues/${number}`,
    state,
    title,
    updated_at: OBSERVED_AT,
    url: `https://example.invalid/example/control-plane/issues/${number}`,
    why_now: safeToStart
      ? "This work is in the active lane and its compact evidence is current."
      : "The item remains visible because stale or missing evidence needs attention.",
  };
}

function engineeringEveryCodeSummary({
  freshness,
  issueNumber,
  state,
  summaryStatus,
  title,
}: {
  freshness: "verified" | "recorded" | "missing";
  issueNumber: number;
  state: "running" | "done" | "blocked";
  summaryStatus: "active" | "complete" | "stuck";
  title: string;
}) {
  const hasEvidence = freshness !== "missing";
  return {
    claimed_at: hasEvidence ? "2026-07-14T14:15:00Z" : "",
    claimed_by_host: hasEvidence ? "every-code-fixture" : "",
    evidence: [
      {
        code: "work_request_state",
        detail: hasEvidence
          ? "The durable work-request state was recorded by Launchplane."
          : "No current worker evidence is available.",
        recorded_at: hasEvidence ? OBSERVED_AT : "",
        sensitivity: "internal" as const,
        source_url: `https://example.invalid/example/control-plane/issues/${issueNumber}`,
        state: freshness,
      },
    ],
    finished_at: state === "done" ? OBSERVED_AT : "",
    issue_number: issueNumber,
    issue_title: title,
    issue_url: `https://example.invalid/example/control-plane/issues/${issueNumber}`,
    next_action:
      state === "blocked"
        ? "Restore worker evidence before rerunning."
        : state === "done"
          ? "Review the result pull request."
          : "Wait for the current worker attempt to finish.",
    provenance: {
      detail: hasEvidence
        ? "Durable Every Code work-request summary."
        : "Every Code worker evidence is missing.",
      freshness_status: freshness,
      recorded_at: hasEvidence ? OBSERVED_AT : "",
      refreshed_at: hasEvidence ? OBSERVED_AT : "",
      sensitivity: "internal" as const,
      source_kind: hasEvidence ? ("record" as const) : ("unsupported" as const),
      source_record_id: hasEvidence ? `fixture-request-${issueNumber}` : "",
      source_url: `https://example.invalid/example/control-plane/issues/${issueNumber}`,
      stale_after: hasEvidence ? STALE_AFTER : "",
    },
    queued_at: "2026-07-14T14:00:00Z",
    repository: "example/control-plane",
    request_id: `fixture-request-${issueNumber}`,
    result_pr_url:
      state === "done"
        ? `https://example.invalid/example/control-plane/pull/${issueNumber + 100}`
        : "",
    result_summary: state === "done" ? "The scoped change completed successfully." : "",
    safe_to_rerun: state === "blocked" && hasEvidence,
    source: "github_issue_label",
    started_at: hasEvidence ? "2026-07-14T14:17:00Z" : "",
    state,
    summary_status: summaryStatus,
    trigger_actor: "platform-operator",
    trigger_label: "every-code",
    updated_at: OBSERVED_AT,
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

function incidentFixtureState(open: boolean) {
  const fixtureState = new URLSearchParams(window.location.search).get("incident");
  const notificationState =
    fixtureState === "acknowledged"
      ? ("acknowledged" as const)
      : fixtureState === "silenced"
        ? ("silenced" as const)
        : ("active" as const);
  const severity = fixtureState === "warning" ? ("warning" as const) : ("critical" as const);
  return {
    incident_last_reminded_at: "",
    incident_latest_event: open ? ("opened" as const) : ("" as const),
    incident_latest_event_at: open ? OBSERVED_AT : "",
    incident_material_fingerprint_sha256: open ? "fixture-material-fingerprint" : "",
    incident_next_reminder_at:
      open && notificationState === "active" ? "2026-07-14T20:32:00Z" : "",
    incident_notification_state: open ? notificationState : ("" as const),
    incident_severity: open ? severity : ("" as const),
  };
}

function incidentSummaryForFixture(
  detail: ProductEnvironmentDetail,
): ProductIncidentSummary | null {
  const ingress = detail.public_ingress;
  const check = detail.health_monitoring.checks.find(
    (candidate) => candidate.incident_id === ingress.incident_id,
  );
  if (!ingress.incident_id || ingress.incident_status !== "open") {
    return null;
  }
  const failureCode = (check?.failure_code || ingress.failure_code) as ProductIncidentSummary["failure_code"];
  const failureLayer: ProductIncidentSummary["failure_layer"] = failureCode.startsWith("tls_")
    ? "tls"
    : failureCode === "wrong_runtime_identity"
      ? "runtime_identity"
      : failureCode === "dns_failure"
        ? "dns"
        : "network";
  const notificationState =
    ingress.incident_notification_state || check?.incident_notification_state || "active";
  return {
    schema_version: 1,
    incident_id: ingress.incident_id,
    product: detail.product,
    display_name: detail.display_name,
    environment: detail.environment,
    context: detail.context,
    instance: detail.environment,
    check_name: check?.name || "public-ingress",
    check_kind: (check?.kind || "public_http") as ProductIncidentSummary["check_kind"],
    status: "open",
    severity: ingress.incident_severity || check?.incident_severity || "critical",
    failure_code: failureCode,
    failure_layer: failureLayer,
    summary: check?.summary || ingress.summary,
    opened_at: ingress.incident_opened_at || OBSERVED_AT,
    latest_observed_at: ingress.observed_at || OBSERVED_AT,
    resolved_at: "",
    resolution_reason: "",
    notification_state: notificationState,
    notification_state_changed_at: ingress.incident_latest_event_at || OBSERVED_AT,
    silenced_until:
      notificationState === "silenced" ? "2026-07-15T02:32:00Z" : "",
    latest_material_event_id: `event-${ingress.incident_id}`,
    latest_material_event: ingress.incident_latest_event || "opened",
    latest_material_event_at: ingress.incident_latest_event_at || OBSERVED_AT,
    material_fingerprint_sha256: ingress.incident_material_fingerprint_sha256,
    recovery_observation_threshold: 2,
    consecutive_recovery_observations: 0,
    next_reminder_at: ingress.incident_next_reminder_at,
    last_reminded_at: ingress.incident_last_reminded_at,
    trust_state: "recorded",
    provenance: provenance(
      "recorded",
      "Launchplane material incident occurrence and reminder state.",
    ),
  };
}

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
    health_monitoring: {
      monitoring_intent: "public",
      public_incident_eligible: true,
      checks: [
        {
          name: "public-ingress",
          kind: "public_http",
          enabled: true,
          probe_effective: true,
          incident_eligible: true,
          private_endpoint_configured: false,
          status: ingressStatus,
          failure_code: warning ? "tls_hostname_mismatch" : "",
          observed_at: OBSERVED_AT,
          record_id: `ingress-${environment}-example`,
          summary: ingressSummary,
          incident_status: warning ? "open" : "",
          incident_id: warning ? "incident-example" : "",
          ...incidentFixtureState(Boolean(warning)),
          trust_state: "verified",
          provenance: provenance("verified", ingressSummary),
        },
      ],
      trust_state: "recorded",
      provenance: provenance("recorded", "Public monitoring intent is recorded."),
    },
    public_ingress: {
      monitoring_intent: "public",
      incident_eligible: true,
      status: ingressStatus,
      summary: ingressSummary,
      failure_code: warning ? "tls_hostname_mismatch" : "",
      incident_id: warning ? "incident-example" : "",
      incident_status: warning ? "open" : "",
      ...incidentFixtureState(Boolean(warning)),
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
          monitoring_intent: "public",
          incident_eligible: true,
          failure_code: warning ? "tls_hostname_mismatch" : "",
          incident_id: warning ? "incident-example" : "",
          incident_status: warning ? "open" : "",
          ...incidentFixtureState(Boolean(warning)),
          observed_at: OBSERVED_AT,
          record_id: `ingress-${environment}-example`,
          status: ingressStatus,
          summary: ingressSummary,
          expected_runtime_identity: null,
          observed_runtime_identity: null,
          runtime_identity_detail: "Runtime identity was not included in this visual fixture.",
          runtime_identity_status: "unchecked",
          stale_after: "2099-01-01T00:00:00Z",
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
            ...incidentFixtureState(Boolean(warning)),
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
    health_monitoring: {
      monitoring_intent: "prelaunch",
      public_incident_eligible: false,
      checks: [
        {
          name: "public-ingress",
          kind: "public_http",
          enabled: true,
          probe_effective: true,
          incident_eligible: false,
          private_endpoint_configured: false,
          status: "missing",
          failure_code: "",
          observed_at: "",
          record_id: "",
          summary: "No prelaunch public probe observation is recorded.",
          incident_status: "",
          incident_id: "",
          ...incidentFixtureState(false),
          trust_state: "missing",
          provenance: missing,
        },
      ],
      trust_state: "recorded",
      provenance: provenance("recorded", "Prelaunch monitoring intent is recorded."),
    },
    public_ingress: {
      monitoring_intent: "prelaunch",
      incident_eligible: false,
      status: "",
      summary: "",
      failure_code: "",
      incident_id: "",
      incident_status: "",
      ...incidentFixtureState(false),
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
          monitoring_intent: "prelaunch",
          incident_eligible: false,
          failure_code: "",
          incident_id: "",
          incident_status: "",
          ...incidentFixtureState(false),
          observed_at: "",
          record_id: "",
          status: "",
          summary: "",
          expected_runtime_identity: null,
          observed_runtime_identity: null,
          runtime_identity_detail: "No public runtime identity observation is recorded.",
          runtime_identity_status: "missing",
          stale_after: "",
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

function _ownerAcceptanceBinding(overrides: {
  pull_request_number: number;
  binding_sha256: string;
  head_sha: string;
  product?: string;
  system?: string;
  action?: string;
  environment?: string;
}) {
  return {
    action: overrides.action ?? "deploy",
    binding_sha256: overrides.binding_sha256,
    change_impact_policy_digest: "b".repeat(64),
    change_impact_policy_record_id: "policy-001",
    change_impact_policy_revision: 1,
    environment: overrides.environment ?? "production",
    head_sha: overrides.head_sha,
    owner_policy_digest: "c".repeat(64),
    owner_policy_record_id: "owner-policy-001",
    owner_policy_revision: 1,
    owner_requirement_digest: "d".repeat(64),
    owner_requirement_record_id: "owner-req-001",
    owner_requirement_revision: 1,
    preview: null,
    review_context: {
      schema_version: 1,
      base_ref: "main",
      base_sha: "b".repeat(40),
      change_class: "routine" as const,
      engineering_review_tier: "routine" as const,
      review_max_age_seconds: 2592000,
      contributions: {
        schema_version: 1,
        resolution: "resolved" as const,
        reason_code: "server_resolved" as const,
        contributor_github_ids: [4001],
        commit_count: 2,
      },
      policy_fingerprints: {
        schema_version: 1,
        fingerprint_version: 1,
        owner_membership_fingerprint: "1".repeat(64),
        self_review_fingerprint: "2".repeat(64),
        review_age_fingerprint: "3".repeat(64),
        requirement_fingerprint: "4".repeat(64),
        preview_trust_fingerprint: "5".repeat(64),
      },
      preview_isolation: {
        schema_version: 1,
        isolation_class: "not_applicable" as const,
        data_transport_mode: "",
        source: "no_preview_binding" as const,
      },
    },
    product: overrides.product ?? "example-site",
    pull_request_number: overrides.pull_request_number,
    repository: "example/control-plane",
    repository_id: "1001",
    repository_owner_id: "2001",
    schema_version: 1,
    system: overrides.system ?? "web",
    tree_sha: "b".repeat(40),
  };
}

function _ownerAcceptanceEvent(
  action: "accepted" | "changes_requested" | "revoked" | "superseded" | "invalidated",
  binding: ReturnType<typeof _ownerAcceptanceBinding>,
  overrides: { occurred_at?: string; event_id?: string; acceptance_id?: string } = {},
) {
  return {
    schema_version: 1,
    event_id: overrides.event_id ?? `owner-acceptance-event-fixture-${action}`,
    acceptance_id: overrides.acceptance_id ?? `owner-acceptance-${binding.binding_sha256.slice(0, 32)}`,
    subject_sequence: 1,
    binding,
    action,
    occurred_at: overrides.occurred_at ?? OBSERVED_AT,
    source_event_kind: (["superseded", "invalidated"].includes(action) ? "system" : "browser_api") as
      | "system"
      | "browser_api",
    source_event_id: `fixture-${action}`,
    reason: action === "accepted" ? "" : `Fixture reason for ${action}`,
    resolution: null,
    authorization:
      action === "accepted" || action === "changes_requested" || action === "revoked"
        ? {
            schema_version: 1,
            owner_identity_id: "fixture-owner-identity",
            owner_github_id: 12345,
            owner_login: "fixture-owner",
            owner_policy_record_id: "owner-policy-001",
            owner_policy_revision: 1,
            owner_policy_digest: "c".repeat(64),
            owner_requirement_record_id: "owner-req-001",
            owner_requirement_revision: 1,
            owner_requirement_digest: "d".repeat(64),
            authorized_at: overrides.occurred_at ?? OBSERVED_AT,
            self_review: false,
            self_review_exception_revision: 0,
          }
        : null,
  };
}

function _ownerAcceptanceQueueEntry(
  ledger_status: "accepted" | "changes_requested" | "revoked" | "stale" | "unavailable",
  binding: ReturnType<typeof _ownerAcceptanceBinding>,
  event: ReturnType<typeof _ownerAcceptanceEvent>,
  next_action: string,
) {
  return {
    schema_version: 1,
    repository_id: binding.repository_id,
    repository: binding.repository,
    pull_request_number: binding.pull_request_number,
    product: binding.product,
    system: binding.system,
    action: binding.action,
    environment: binding.environment,
    verification_required: true as const,
    ledger_status,
    next_action,
    latest_event: event,
    latest_binding: binding,
    occurred_at: event.occurred_at,
  };
}

export function ownerAcceptanceForFixture(
  fixture: DataFixtureMode,
): OwnerAcceptanceQueueResponse {
  assertEngineeringFixtureAvailable(fixture);

  if (fixture === "empty") {
    return {
      candidate: 0,
      derivation: "ledger_only",
      entries: [],
      entry_count: 0,
      generated_at: OBSERVED_AT,
      has_more: false,
      status: "ok",
      total: 0,
      trace_id: "fixture-owner-acceptance-empty",
      truncated: false,
    };
  }

  const acceptedBinding = _ownerAcceptanceBinding({
    pull_request_number: 302,
    binding_sha256: "e".repeat(64),
    head_sha: "f".repeat(40),
  });
  const acceptedEvent = _ownerAcceptanceEvent("accepted", acceptedBinding, {
    occurred_at: "2026-08-07T10:00:00.000000Z",
    event_id: "owner-acceptance-event-fixture-accepted-e2",
    acceptance_id: "owner-acceptance-" + "e".repeat(32),
  });

  const revokedBinding = _ownerAcceptanceBinding({
    pull_request_number: 305,
    binding_sha256: "7".repeat(64),
    head_sha: "8".repeat(40),
  });
  const revokedEvent = _ownerAcceptanceEvent("revoked", revokedBinding, {
    occurred_at: "2026-08-06T15:30:00.000000Z",
    event_id: "owner-acceptance-event-fixture-revoked-r5",
    acceptance_id: "owner-acceptance-" + "7".repeat(32),
  });

  const staleBinding = _ownerAcceptanceBinding({
    pull_request_number: 299,
    binding_sha256: "9".repeat(64),
    head_sha: "a".repeat(40),
    product: "example-worker",
    system: "queue",
  });
  const supersededEvent = _ownerAcceptanceEvent("superseded", staleBinding, {
    occurred_at: "2026-08-05T08:00:00.000000Z",
    event_id: "owner-acceptance-event-fixture-superseded-s9",
    acceptance_id: "owner-acceptance-" + "9".repeat(32),
  });

  const unavailableBinding = _ownerAcceptanceBinding({
    pull_request_number: 295,
    binding_sha256: "3".repeat(64),
    head_sha: "4".repeat(40),
    product: "example-api",
    system: "rest",
    action: "promote",
    environment: "staging",
  });
  const invalidatedEvent = _ownerAcceptanceEvent("invalidated", unavailableBinding, {
    occurred_at: "2026-08-04T12:00:00.000000Z",
    event_id: "owner-acceptance-event-fixture-invalidated-i3",
    acceptance_id: "owner-acceptance-" + "3".repeat(32),
  });

  const allEntries = [
    _ownerAcceptanceQueueEntry("accepted", acceptedBinding, acceptedEvent, "Owner acceptance is current"),
    _ownerAcceptanceQueueEntry("revoked", revokedBinding, revokedEvent, "Re-acceptance required after changes"),
    _ownerAcceptanceQueueEntry("stale", staleBinding, supersededEvent, "Re-evaluation required; binding has changed"),
    _ownerAcceptanceQueueEntry("unavailable", unavailableBinding, invalidatedEvent, "Evidence unavailable; retry after prerequisites are met"),
  ];

  const entries = fixture === "missing" ? allEntries.slice(0, 1) : allEntries;

  return {
    candidate: entries.length,
    derivation: "ledger_only",
    entries,
    entry_count: entries.length,
    generated_at: OBSERVED_AT,
    has_more: false,
    status: "ok",
    total: entries.length,
    trace_id: "fixture-owner-acceptance",
    truncated: false,
  };
}

export function ownerAcceptanceEvaluationForFixture(
  fixture: DataFixtureMode,
): OwnerAcceptanceDecision {
  assertEngineeringFixtureAvailable(fixture);
  const binding = _ownerAcceptanceBinding({
    pull_request_number: 308,
    binding_sha256: "a".repeat(64),
    head_sha: "a".repeat(40),
  });
  return {
    schema_version: 1,
    status: "pending",
    reason_code: "acceptance_missing",
    binding,
    current_event: null,
    admissible: false,
    human_action_semantics: "none",
    products: [
      {
        schema_version: 1,
        product: binding.product,
        system: binding.system,
        action: binding.action,
        environment: binding.environment,
        status: "pending",
        reason_code: "acceptance_missing",
        binding,
        current_event: null,
        admissible: false,
        human_action_semantics: "none",
      },
    ],
    evaluated_at: OBSERVED_AT,
  };
}

export function governanceProjectionForFixture(
  fixture: DataFixtureMode,
): GovernanceProjectionResponse {
  assertEngineeringFixtureAvailable(fixture);
  const scenario = new URLSearchParams(window.location.search).get("scenario") ?? "25";
  const binding = _ownerAcceptanceBinding({
    pull_request_number: 308,
    binding_sha256: "a".repeat(64),
    head_sha: "a".repeat(40),
  });
  const acceptedEvent = _ownerAcceptanceEvent("accepted", binding, {
    occurred_at: "2026-08-12T04:00:00.000000Z",
    event_id: "owner-acceptance-event-governance-accepted",
    acceptance_id: "owner-acceptance-" + "a".repeat(32),
  });
  const revokedEvent = _ownerAcceptanceEvent("revoked", binding, {
    occurred_at: "2026-08-12T04:40:00.000000Z",
    event_id: "owner-acceptance-event-governance-revoked",
    acceptance_id: "owner-acceptance-" + "a".repeat(32),
  });
  const currentStatus =
    scenario === "15"
      ? "revoked"
      : scenario === "20"
        ? "stale"
        : scenario === "24"
          ? "changes_requested"
          : "accepted";
  const currentReason =
    scenario === "15"
      ? "acceptance_revoked"
      : scenario === "20"
        ? "preview_isolation_insufficient"
        : scenario === "24"
          ? "changes_requested"
          : "acceptance_valid";
  const currentEvent = scenario === "15" ? revokedEvent : acceptedEvent;
  const product: OwnerAcceptanceProductDecision = {
    schema_version: 1,
    product: binding.product,
    system: binding.system,
    action: binding.action,
    environment: binding.environment,
    status: currentStatus,
    reason_code: currentReason,
    binding,
    current_event: currentEvent,
    admissible: currentStatus === "accepted",
    human_action_semantics:
      scenario === "15" ? "product_review_revoked" : "product_review_accepted",
  };
  const products: OwnerAcceptanceProductDecision[] =
    scenario === "24"
      ? [
          { ...product, status: "accepted", reason_code: "acceptance_valid", admissible: true },
          {
            ...product,
            product: "example-secondary",
            status: "changes_requested",
            reason_code: "changes_requested",
            admissible: false,
          },
        ]
      : [product];
  const decision: OwnerAcceptanceDecision = {
    schema_version: 1,
    status: currentStatus,
    reason_code: currentReason,
    binding,
    current_event: currentEvent,
    admissible: currentStatus === "accepted",
    human_action_semantics:
      scenario === "15" ? "product_review_revoked" : "product_review_accepted",
    products,
    evaluated_at: OBSERVED_AT,
  };
  const readiness = governanceReadinessFixture(
    scenario === "3" ? "unknown" : scenario === "24" || scenario === "20" ? "blocked_owner_evidence" : "blocked_checks",
    products,
  );
  const admission = scenario === "15" ? governanceAdmissionFixture(readiness) : null;
  const outcome = scenario === "15" && admission ? governanceOutcomeFixture(admission) : null;
  return {
    status: "ok",
    trace_id: `fixture-governance-${scenario}`,
    projection: {
      schema_version: 1,
      mode: "read_only_projection",
      authoritative: false,
      authorizes: [],
      target: {
        schema_version: 1,
        repository_id: binding.repository_id,
        repository_owner_id: binding.repository_owner_id,
        repository: binding.repository,
        pull_request_number: binding.pull_request_number,
        head_sha: binding.head_sha,
        tree_sha: binding.tree_sha,
      },
      owner_judgment: {
        level: 1,
        mode: "owner_acceptance",
        authoritative: true,
        current: decision,
        history: [
          {
            record: acceptedEvent,
            human_action_semantics: "product_review_accepted",
            target_status: "current",
            decision_relationship: scenario === "15" ? "historical" : "current",
          },
          ...(scenario === "15"
            ? [
                {
                  record: revokedEvent,
                  human_action_semantics: "product_review_revoked" as const,
                  target_status: "current" as const,
                  decision_relationship: "current" as const,
                },
              ]
            : []),
        ],
      },
      merge_readiness: {
        level: 2,
        mode: "ephemeral",
        authoritative: false,
        authorizes: [],
        availability: "available",
        reason_code: "current_evaluation_available",
        result: readiness,
      },
      merge_admission: {
        level: 3,
        mode: "immutable_attempt_authorization",
        authoritative: false,
        status: admission ? "admitted_current_target" : "not_recorded",
        current_effect_authority: false,
        authorizes: admission ? ["one_exact_merge_attempt"] : [],
        record: admission,
      },
      landing_outcome: {
        mode: "immutable_provider_observation",
        authoritative: false,
        authorizes: [],
        target_status: outcome ? "current" : "none",
        status: outcome?.status ?? "not_observed",
        landed: outcome?.status === "landed",
        record: outcome,
      },
      advisory_observations: [
        {
          observation_scope: "current_readiness",
          observation: {
            name: "launchplane/owner-acceptance",
            state: "neutral",
            app_id: 42,
          },
          authoritative: false,
          authorizes: [],
        },
        {
          observation_scope: "current_readiness",
          observation: {
            name: "launchplane/engineering-review",
            state: "neutral",
            app_id: 42,
          },
          authoritative: false,
          authorizes: [],
        },
      ],
      generated_at: OBSERVED_AT,
    },
  };
}

function governanceReadinessFixture(
  state: MergeReadinessResult["state"],
  products: readonly unknown[],
): MergeReadinessResult {
  const ownerState = state === "blocked_owner_evidence" ? "blocked_owner_evidence" : "ready";
  return {
    schema_version: 1,
    mode: "ephemeral" as const,
    authoritative: false as const,
    authorizes: [],
    engineering_review_authority: "required",
    target: {
      schema_version: 1,
      repository: "example/tenant-site",
      pull_request_number: 308,
      base_branch: "main",
      base_sha: "1".repeat(40),
      pull_request_head_sha: "a".repeat(40),
      pull_request_tree_sha: "b".repeat(40),
      expected_effect_sha: "4".repeat(40),
      queue_position: 1,
    },
    state,
    reason_codes:
      state === "unknown"
        ? ["checks_unknown"]
        : state === "blocked_owner_evidence"
          ? ["owner_changes_requested", "checks_passed"]
          : ["owner_acceptance_valid", "checks_pending"],
    owner_facets: products.map((_, index) => ({
      product: index ? "example-secondary" : "example-site",
      system: "web",
      action: "change",
      environment: "production",
      owner_status: ownerState === "ready" ? "accepted" : "changes_requested",
      owner_reason_code:
        ownerState === "ready" ? "acceptance_valid" : "changes_requested",
      binding_sha256: "a".repeat(64),
      event_id: "owner-acceptance-event-governance-accepted",
      state: ownerState,
      reason_codes:
        ownerState === "ready" ? ["owner_acceptance_valid"] : ["owner_changes_requested"],
    })),
    technical_checks: {
      head_sha: "4".repeat(40),
      status: state === "unknown" ? "unknown" : state === "blocked_checks" ? "pending" : "pass",
      required_checks: ["ci-gate", "security-gate"],
      advisory_observations: [],
      state: state === "unknown" ? "unknown" : state === "blocked_checks" ? "blocked_checks" : "ready",
      reason_codes:
        state === "unknown" ? ["checks_unknown"] : state === "blocked_checks" ? ["checks_pending"] : ["checks_passed"],
    },
    engineering_review: {
      status: "approved",
      decision_id: "engineering-review-fixture",
      decision_binding_sha256: "c".repeat(64),
      head_sha: "a".repeat(40),
      tree_sha: "b".repeat(40),
      evidence: [],
      state: "ready",
      reason_codes: ["engineering_review_approved"],
    },
    policy: {
      fingerprints: governancePolicyFingerprintsFixture(),
      state: "ready",
      reason_codes: ["policy_fingerprints_match"],
    },
    candidate: {
      evidence: {
        structural_status: "exact" as const,
        record_id: "candidate-fixture",
        repository: "example/tenant-site",
        base_sha: "1".repeat(40),
        candidate_sha: "4".repeat(40),
        pull_request_number: 308,
        queue_position: 1,
        pull_request_head_sha: "a".repeat(40),
      },
      state: "ready",
      reason_codes: ["candidate_exact"],
    },
    fence: {
      evidence: {
        controller_base_branch: "main",
        controller_key: "merge-train-controller:example:tenant-site:main",
        controller_repository: "example/tenant-site",
        controller_status: "running",
        expected_lease_owner: "fixture",
        lease_expires_at: "2026-08-12T04:50:00Z",
        observed_effect_sha: "4".repeat(40),
        observed_lease_owner: "fixture",
      },
      state: "ready",
      reason_codes: ["controller_lease_held", "expected_sha_match"],
    },
    evaluated_at: OBSERVED_AT,
    readiness_digest: "d".repeat(64),
  };
}

function governancePolicyFingerprintsFixture() {
  const fingerprint = (
    dimension:
      | "impact"
      | "technical_checks"
      | "engineering_review"
      | "ruleset"
      | "merge_train"
      | "authorization"
      | "admission_algorithm",
  ) => ({
    dimension,
    expected_sha256: "e".repeat(64),
    current_sha256: "e".repeat(64),
  });
  return {
    impact: fingerprint("impact"),
    technical_checks: fingerprint("technical_checks"),
    engineering_review: fingerprint("engineering_review"),
    ruleset: fingerprint("ruleset"),
    merge_train: fingerprint("merge_train"),
    authorization: fingerprint("authorization"),
    admission_algorithm: fingerprint("admission_algorithm"),
  };
}

function governanceAdmissionFixture(readiness: MergeReadinessResult): MergeAdmissionRecord {
  return {
    schema_version: 1 as const,
    admission_id: "merge-admission-fixture",
    admission_binding_sha256: "1".repeat(64),
    attempt_id: "merge-effect-attempt-fixture",
    attempt_sequence: 1,
    decision: "admitted" as const,
    source: "fixture",
    repository: "example/tenant-site",
    base_branch: "main",
    pull_request_number: 308,
    queue_position: 1,
    batch_id: "batch-fixture",
    candidate_record_id: "candidate-fixture",
    landing_plan_record_id: "landing-record-fixture",
    landing_plan_id: "landing-plan-fixture",
    merge_method: "merge" as const,
    effective_base_sha: "1".repeat(40),
    effective_base_tree_sha: "2".repeat(40),
    pull_request_head_sha: "a".repeat(40),
    pull_request_head_tree_sha: "b".repeat(40),
    candidate_sha: "4".repeat(40),
    candidate_tree_sha: "5".repeat(40),
    expected_effect_sha: "4".repeat(40),
    candidate_sha256: "6".repeat(64),
    structural_provenance_sha256: "7".repeat(64),
    landing_plan_sha256: "8".repeat(64),
    readiness,
    structural_result: {
      status: "exact" as const,
      reason_codes: ["structural_single_entry_exact"],
      effective_base_sha: "1".repeat(40),
      effective_base_tree_sha: "2".repeat(40),
      candidate_sha256: "6".repeat(64),
      landing_plan_sha256: "8".repeat(64),
      provenance_sha256: "7".repeat(64),
    },
    admission_algorithm_version: "merge-admission-v1",
    controller_key: "merge-train-controller:example:tenant-site:main",
    lease_owner: "fixture",
    lease_acquired_at: "2026-08-12T03:50:00Z",
    lease_expires_at: "2026-08-12T04:50:00Z",
    created_at: "2026-08-12T04:05:00Z",
  };
}

function governanceOutcomeFixture(admission: MergeAdmissionRecord): MergeLandingOutcomeRecord {
  return {
    schema_version: 1 as const,
    outcome_id: "merge-landing-outcome-fixture",
    outcome_binding_sha256: "9".repeat(64),
    admission_id: admission.admission_id,
    admission_binding_sha256: admission.admission_binding_sha256,
    attempt_id: admission.attempt_id,
    observation_sequence: 1,
    prior_outcome_id: "",
    source: "fixture",
    repository: admission.repository,
    base_branch: admission.base_branch,
    pull_request_number: admission.pull_request_number,
    status: "landed" as const,
    reason: "provider_and_git_confirmed" as const,
    provider_effect_attempted: true,
    provider_conclusive_rejection: false,
    provider_status_code: 200,
    provider_request_id: "fixture-request",
    provider_message: "",
    observed_pull_request_state: "merged" as const,
    observed_pull_request_head_sha: admission.pull_request_head_sha,
    observed_pull_request_head_tree_sha: admission.pull_request_head_tree_sha,
    observed_base_sha: admission.candidate_sha,
    observed_base_tree_sha: admission.candidate_tree_sha,
    merge_commit_sha: admission.candidate_sha,
    merge_commit_tree_sha: admission.candidate_tree_sha,
    base_contains_merge_commit: true,
    exact_landing_confirmed: true,
    observed_at: "2026-08-12T04:10:00Z",
  };
}

function assertFixtureAvailable(fixture: DataFixtureMode): void {
  if (fixture === "error") {
    throw new Error("The fixture product inventory is intentionally unavailable.");
  }
}

function assertEngineeringFixtureAvailable(fixture: DataFixtureMode): void {
  if (fixture === "denied") {
    throw Object.assign(
      new Error("This fixture browser session is denied Engineering Ops evidence."),
      {
        statusCode: 403,
        traceId: "fixture-engineering-denied",
      },
    );
  }
  if (fixture === "error") {
    throw Object.assign(
      new Error("The engineering fixture inventory is intentionally unavailable."),
      {
        statusCode: 503,
        traceId: "fixture-engineering-error",
      },
    );
  }
}
