import type {
  AuthSessionResponse as GeneratedAuthSessionResponse,
  DataProvenance as GeneratedDataProvenance,
  DriverContextViewResponse as GeneratedDriverContextViewResponse,
  DriverDescriptorsResponse as GeneratedDriverDescriptorsResponse,
  EveryCodeSummaryResponse as GeneratedEveryCodeSummaryResponse,
  EveryCodeWorkRequestRecordsResponse as GeneratedEveryCodeWorkRequestRecordsResponse,
  GitHubHumanIdentityResponse as GeneratedGitHubHumanIdentityResponse,
  LaunchplaneErrorResponse as GeneratedLaunchplaneErrorResponse,
  MergeTrainControllerStatusResponse as GeneratedMergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse as GeneratedMergeTrainPolicyTargetsResponse,
  MergeTrainRunRecord as GeneratedMergeTrainRunRecord,
  ProductActionAvailability as GeneratedProductActionAvailability,
  ProductEnvironmentListResponse as GeneratedProductEnvironmentListResponse,
  ProductEnvironmentSummary as GeneratedProductEnvironmentSummary,
  ProductPreviewSummary as GeneratedProductPreviewSummary,
  ProductSiteOverview as GeneratedProductSiteOverview,
  PreviewReadinessResponse as GeneratedPreviewReadinessResponse,
  RankWorkGraphSnapshotResponse as GeneratedRankWorkGraphSnapshotResponse,
  RepoProductMappingResponse as GeneratedRepoProductMappingResponse,
  WorkGraphIssueInboxResponse as GeneratedWorkGraphIssueInboxResponse,
  WorkGraphSnapshot as GeneratedWorkGraphSnapshot,
  WorkGraphSnapshotResponse as GeneratedWorkGraphSnapshotResponse,
} from "./generated/openapi.ts";

export type Safety = "read" | "safe_write" | "mutation" | "destructive";
export type Status =
  | "pass"
  | "fail"
  | "pending"
  | "skipped"
  | "unknown"
  | "blocked";
export type FreshnessStatus = GeneratedDataProvenance["freshness_status"];

export type DataProvenance = GeneratedDataProvenance;

export interface AgentContextEvidence {
  code: string;
  state: FreshnessStatus;
  detail: string;
  source_url: string;
  sensitivity: "public" | "internal" | "restricted";
  recorded_at: string;
}

export interface AgentContextProvenance {
  source_kind: "record" | "provider" | "descriptor" | "unsupported";
  freshness_status: FreshnessStatus;
  source_url: string;
  source_record_id: string;
  recorded_at: string;
  refreshed_at: string;
  stale_after: string;
  detail: string;
  sensitivity: "public" | "internal" | "restricted";
}

export interface DriverActionDescriptor {
  action_id: string;
  label: string;
  description: string;
  safety: Safety;
  scope: "global" | "context" | "instance" | "preview";
  method: "GET" | "POST";
  route_path: string;
  writes_records: string[];
}

export interface DriverCapabilityDescriptor {
  capability_id: string;
  label: string;
  description: string;
  actions: string[];
  panels: string[];
}

export interface DriverSettingGroupDescriptor {
  group_id: string;
  label: string;
  description: string;
  scope: "global" | "context" | "instance" | "preview";
  fields: string[];
  secret_bindings: string[];
}

export interface DriverDescriptor {
  schema_version: number;
  driver_id: string;
  base_driver_id: string;
  label: string;
  product: string;
  description: string;
  context_patterns: string[];
  provider_boundary: string;
  capabilities: DriverCapabilityDescriptor[];
  actions: DriverActionDescriptor[];
  setting_groups: DriverSettingGroupDescriptor[];
}

export interface ArtifactIdentityReference {
  artifact_id: string;
  manifest_version?: number;
}

export interface DeploymentEvidence {
  target_name: string;
  target_type: "compose" | "application";
  deploy_mode: string;
  deployment_id?: string;
  status: Status;
  started_at?: string;
  finished_at?: string;
}

export interface HealthcheckEvidence {
  verified: boolean;
  urls: string[];
  timeout_seconds?: number | null;
  status: Status;
}

export interface EnvironmentInventory {
  context: string;
  instance: string;
  artifact_identity?: ArtifactIdentityReference | null;
  source_git_ref: string;
  deploy: DeploymentEvidence;
  destination_health: HealthcheckEvidence;
  updated_at: string;
  deployment_record_id: string;
  promotion_record_id?: string;
  promoted_from_instance?: string;
}

export interface ReleaseTupleRecord {
  tuple_id: string;
  context: string;
  channel: string;
  artifact_id: string;
  repo_shas: Record<string, string>;
  image_repository?: string;
  image_digest?: string;
  deployment_record_id?: string;
  promotion_record_id?: string;
  promoted_from_channel?: string;
  provenance: "ship" | "promotion";
  minted_at: string;
}

export interface DeploymentRecord {
  record_id: string;
  artifact_identity?: ArtifactIdentityReference | null;
  context: string;
  instance: string;
  source_git_ref: string;
  deploy: DeploymentEvidence;
  destination_health: HealthcheckEvidence;
}

export interface BackupGateRecord {
  record_id: string;
  context: string;
  instance: string;
  created_at: string;
  source: string;
  required: boolean;
  status: Status;
  evidence: Record<string, string>;
}

export interface PromotionRecord {
  record_id: string;
  artifact_identity: ArtifactIdentityReference;
  deployment_record_id?: string;
  backup_record_id?: string;
  context: string;
  from_instance: string;
  to_instance: string;
  source_health?: HealthcheckEvidence;
  backup_gate: {
    required: boolean;
    status: Status;
    evidence: Record<string, string>;
  };
  deploy: DeploymentEvidence;
  destination_health: HealthcheckEvidence;
}

export interface SecretBinding {
  binding_id: string;
  secret_id: string;
  integration: string;
  binding_type: "env";
  binding_key: string;
  context?: string;
  instance?: string;
  status: "configured" | "disabled";
  created_at: string;
  updated_at: string;
}

export interface RuntimeEnvironmentRecord {
  scope: "global" | "context" | "instance";
  context: string;
  instance: string;
  env: Record<string, string | number | boolean>;
  updated_at: string;
  source_label: string;
}

export interface LaneSummary {
  context: string;
  instance: string;
  inventory?: EnvironmentInventory | null;
  release_tuple?: ReleaseTupleRecord | null;
  latest_deployment?: DeploymentRecord | null;
  latest_promotion?: PromotionRecord | null;
  latest_backup_gate?: BackupGateRecord | null;
  odoo_instance_override?: unknown | null;
  runtime_environment_records?: RuntimeEnvironmentRecord[];
  secret_bindings: SecretBinding[];
  provenance: DataProvenance;
}

export interface PreviewRecord {
  preview_id: string;
  context: string;
  anchor_repo: string;
  anchor_pr_number: number;
  anchor_pr_url: string;
  preview_label: string;
  canonical_url: string;
  state: string;
  created_at: string;
  updated_at: string;
  eligible_at: string;
}

export interface PreviewGenerationRecord {
  generation_id: string;
  preview_id: string;
  sequence: number;
  state: string;
  requested_reason: string;
  requested_at: string;
  ready_at?: string;
  finished_at?: string;
  artifact_id?: string;
  deploy_status?: Status;
  verify_status?: Status;
  overall_health_status?: Status;
}

export interface PreviewSummary {
  preview: PreviewRecord;
  latest_generation?: PreviewGenerationRecord | null;
  recent_generations: PreviewGenerationRecord[];
  provenance: DataProvenance;
}

export interface DriverView {
  driver_id: string;
  descriptor: DriverDescriptor;
  available_actions: DriverActionDescriptor[];
  lane_summary?: LaneSummary | null;
  preview_summaries: PreviewSummary[];
  preview_inventory_provenance?: DataProvenance | null;
}

export interface DriverContextView {
  schema_version: number;
  context: string;
  instance: string;
  drivers: DriverView[];
}

export type DriverListPayload = GeneratedDriverDescriptorsResponse;

export type DriverViewPayload = GeneratedDriverContextViewResponse;

export type AuthIdentity = GeneratedGitHubHumanIdentityResponse;

export type AuthSessionPayload = GeneratedAuthSessionResponse;

export interface LogoutPayload {
  status: "ok";
  trace_id: string;
}

export type ApiErrorPayload = GeneratedLaunchplaneErrorResponse;

export type ProductActionAvailability = GeneratedProductActionAvailability;

export type ProductEnvironmentSummary = GeneratedProductEnvironmentSummary;

export type ProductPreviewOverview = GeneratedProductPreviewSummary;

export type ProductSiteOverview = GeneratedProductSiteOverview;

export type ProductListPayload = GeneratedProductEnvironmentListResponse;

export type EveryCodeWorkRequestState =
  | "queued"
  | "claimed"
  | "running"
  | "done"
  | "blocked";

export interface EveryCodeWorkRequestRecord {
  schema_version: number;
  request_id: string;
  source: "github_issue_label" | "manual" | "reconciliation" | string;
  state: EveryCodeWorkRequestState;
  repository: string;
  issue_number: number;
  issue_url: string;
  issue_title: string;
  trigger_label: string;
  trigger_actor: string;
  github_delivery_id: string;
  queued_at: string;
  updated_at: string;
  claimed_at: string;
  claimed_by_host: string;
  started_at: string;
  finished_at: string;
  result_pr_url: string;
  result_summary: string;
  error_message: string;
}

export type EveryCodeWorkRequestListPayload = GeneratedEveryCodeWorkRequestRecordsResponse;

export interface EveryCodeWorkRequestSummary {
  request_id: string;
  repository: string;
  issue_number: number;
  issue_url: string;
  issue_title: string;
  state: EveryCodeWorkRequestRecord["state"];
  summary_status: "active" | "stuck" | "complete" | "rerunnable";
  source: string;
  trigger_label: string;
  trigger_actor: string;
  claimed_by_host: string;
  queued_at: string;
  updated_at: string;
  claimed_at: string;
  started_at: string;
  finished_at: string;
  result_pr_url: string;
  result_summary: string;
  safe_to_rerun: boolean;
  next_action: string;
  provenance: AgentContextProvenance;
  evidence: AgentContextEvidence[];
}

export type EveryCodeSummaryPayload = GeneratedEveryCodeSummaryResponse;

export interface PreviewReadinessItem {
  gate_id: string;
  request_id: string;
  repository: string;
  issue_number: number;
  issue_url: string;
  pr_number: number;
  pr_url: string;
  head_sha: string;
  gate_status: "pending" | "ready" | "blocked" | "labeled" | "cancelled";
  readiness_status:
    | "waiting_on_checks"
    | "ready"
    | "needs_attention"
    | "cancelled";
  freshness_status:
    | "verified"
    | "recorded"
    | "stale"
    | "missing"
    | "unsupported";
  provenance: string;
  updated_at: string;
  last_checked_at: string;
  ready_at: string;
  terminal_at: string;
  detail: string;
  check_summary: string;
  source_of_truth_url: string;
  safe_to_request_preview: boolean;
  needs_operator_attention: boolean;
  context_provenance: AgentContextProvenance;
  evidence: AgentContextEvidence[];
}

export type PreviewReadinessPayload = GeneratedPreviewReadinessResponse;

export type WorkGraphRepoClassification =
  | "managed_runtime"
  | "active_awareness"
  | "support_dependency"
  | "out_of_scope";
export type WorkGraphFocus =
  | "Now"
  | "Next"
  | "Waiting"
  | "Later"
  | "Done"
  | "Unknown";
export type WorkGraphState =
  | "ready"
  | "waiting"
  | "blocked"
  | "done"
  | "hidden";
export type WorkGraphRecommendation =
  | "quick_win"
  | "deep_work"
  | "switch_projects"
  | "blocked_cleanup"
  | "attention_needed"
  | "watch";

export interface WorkGraphRepoSnapshot {
  repository: string;
  classification: WorkGraphRepoClassification;
  product?: string;
  display_name?: string;
}

export interface WorkGraphIssueSnapshot {
  repository: string;
  number: number;
  title: string;
  url: string;
  state?: "open" | "closed";
  focus?: WorkGraphFocus;
  manager?: string;
  finish_line?: string;
  labels?: string[];
  blocked_by?: number;
  blocking?: number;
  subissues_total?: number;
  subissues_completed?: number;
  updated_at?: string;
  is_pull_request?: boolean;
  check_state?: "success" | "pending" | "failure" | "unknown";
  deploy_state?: "success" | "pending" | "failure" | "unknown";
}

export type WorkGraphSnapshot = GeneratedWorkGraphSnapshot;

export type WorkGraphSnapshotPayload = GeneratedWorkGraphSnapshotResponse;

export interface RepoProductMappingEntry {
  repository: string;
  classification: WorkGraphRepoClassification;
  product: string;
  display_name: string;
  driver_id: string;
  contexts: string[];
  environments: string[];
  preview_context: string;
  source: "product_profile" | "every_code_work_request" | "explicit";
  updated_at: string;
}

export type RepoProductMappingPayload = GeneratedRepoProductMappingResponse;

export interface WorkGraphQueueItem {
  repository: string;
  repo_classification: WorkGraphRepoClassification;
  product: string;
  product_display_name: string;
  number: number;
  title: string;
  url: string;
  focus: WorkGraphFocus;
  manager: string;
  finish_line: string;
  state: WorkGraphState;
  recommendation: WorkGraphRecommendation;
  score: number;
  updated_at: string;
  safe_to_start: boolean;
  next_action: string;
  why_now: string;
  blocked_by_count: number;
  source_of_truth_url: string;
  handoff_url: string;
  evidence: Array<{
    code: string;
    state: "verified" | "recorded" | "stale" | "missing" | "unsupported";
    detail: string;
    source_url: string;
  }>;
  reasons: Array<{ code: string; detail: string }>;
}

export type WorkGraphRankPayload = GeneratedRankWorkGraphSnapshotResponse;

export type GitHubIssueInboxProjectStatus =
  | "present"
  | "missing"
  | "stale"
  | "closed"
  | "unconfigured";

export interface GitHubIssueInboxIssue {
  key: string;
  repository: string;
  number: number;
  title: string;
  url: string;
  state: string;
  labels: string[];
  author: string;
  created_at: string;
  updated_at: string;
  project_status: GitHubIssueInboxProjectStatus;
  present_in_project: boolean | null;
}

export interface GitHubIssueInboxRepositoryGroup {
  repository: string;
  issue_count: number;
  present_in_project_count: number;
  missing_from_project_count: number;
  issues: GitHubIssueInboxIssue[];
}

export interface GitHubIssueInbox {
  schema_version: number;
  generated_at: string;
  project_configured: boolean;
  repository_count: number;
  issue_count: number;
  stale_project_item_count: number;
  repositories: GitHubIssueInboxRepositoryGroup[];
}

export type GitHubIssueInboxPayload = GeneratedWorkGraphIssueInboxResponse;

export type GitHubIssueInboxReconcileMode = "dry_run" | "apply";
export type GitHubIssueInboxReconcileAction =
  | "would_add"
  | "added"
  | "already_present"
  | "failed"
  | "skipped";

export interface GitHubIssueInboxReconcileItem {
  key: string;
  repository: string;
  number: number;
  title: string;
  url: string;
  action: GitHubIssueInboxReconcileAction;
  detail: string;
  error_code: string;
  error_correlation_id: string;
}

export interface GitHubIssueInboxReconcileSummary {
  schema_version: number;
  generated_at: string;
  mode: GitHubIssueInboxReconcileMode;
  repository_count: number;
  issue_count: number;
  added_count: number;
  already_present_count: number;
  skipped_count: number;
  failed_count: number;
  would_add_count: number;
  items: GitHubIssueInboxReconcileItem[];
}

export interface GitHubIssueInboxReconcilePayload {
  status: "accepted";
  trace_id: string;
  records: Record<string, string>;
  result: {
    reconcile: GitHubIssueInboxReconcileSummary;
  };
}

export interface MergeTrainAdmissionDecision {
  schema_version: number;
  repository: string;
  base_branch: string;
  status: "admitted" | "deferred" | string;
  reason_code: string;
  requested_at: string;
  next_allowed_at: string;
  latest_run_id: string;
  latest_run_status: string;
  latest_run_recorded_at: string;
  controller_action: string;
  controller_reason: string;
  controller_candidate_record_id: string;
  controller_landing_plan_record_id: string;
  controller_stack_collapse_plan_record_id: string;
  detail: string;
}

export type MergeTrainRunRecord = GeneratedMergeTrainRunRecord;

export interface MergeTrainDryRunQueueEntrySummary {
  pull_request_number: number;
  title: string;
  url: string;
  eligible: boolean;
  ineligible_reasons: string[];
  mergeable: string;
  required_checks_status: string;
}

export interface MergeTrainLatestDryRunSummary {
  intended_next_action: string;
  next_action_detail: string;
  queue_count: number;
  eligible_count: number;
  selected_pr_number: number | null;
  queue_entries: MergeTrainDryRunQueueEntrySummary[];
}

export interface MergeTrainControllerRecordSummary {
  record_id: string;
  record_type: string;
  status: string;
  updated_at: string;
  policy_key: string;
  policy_sha256: string;
  policy_status: "current" | "stale" | "unchecked" | string;
  stale_reason: string;
  batch_id: string;
  pull_request_numbers: number[];
  candidate_sha: string;
  required_checks_status: string;
  planned_count: number;
  merged_count: number;
  blocked_count: number;
  stale_count: number;
  skipped_count: number;
}

export interface MergeTrainControllerStatus {
  schema_version: number;
  repository: string;
  base_branch: string;
  generated_at: string;
  current_policy_key: string;
  current_policy_sha256: string;
  admission: MergeTrainAdmissionDecision;
  latest_run: MergeTrainRunRecord | null;
  latest_dry_run: MergeTrainLatestDryRunSummary | null;
  controller_records: MergeTrainControllerRecordSummary[];
}

export type MergeTrainControllerStatusPayload =
  GeneratedMergeTrainControllerStatusResponse;

export interface MergeTrainPolicyTarget {
  repository: string;
  base_branch: string;
  policy_key: string;
  service_authz: {
    action: string;
    product: string;
    context: string;
  };
}

export type MergeTrainPolicyTargetsPayload = GeneratedMergeTrainPolicyTargetsResponse;
