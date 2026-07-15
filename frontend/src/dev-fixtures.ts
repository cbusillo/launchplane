import type {
  DataProvenance,
  ApplyProductEnvironmentConfigData,
  EveryCodeSummaryResponse,
  GitHubHumanIdentityResponse,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
  ProductActionAvailability,
  ProductActivityReadModel,
  ProductEnvironmentConfigStatus,
  ProductConfigApplyResponse,
  ProductConfigWriteAvailability,
  ProductEnvironmentDetail,
  ProductEnvironmentSummary,
  ProductSiteOverview,
  RuntimeIdentity,
  WorkGraphIssueInboxResponse,
  WorkGraphQueue,
  WorkGraphSnapshotResponse,
} from "./generated/openapi.ts";

type TrustState = ProductSiteOverview["trust_state"];
type DataFixtureMode = "products" | "empty" | "error" | "missing" | "denied";
type EngineeringLoadReason = "initial" | "refresh";

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
