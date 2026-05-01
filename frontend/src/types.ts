export type Safety = "read" | "safe_write" | "mutation" | "destructive";
export type Status =
  | "pass"
  | "fail"
  | "pending"
  | "skipped"
  | "unknown"
  | "blocked";
export type FreshnessStatus =
  | "verified"
  | "recorded"
  | "stale"
  | "missing"
  | "unsupported";

export interface DataProvenance {
  source_kind: "record" | "provider" | "descriptor" | "unsupported";
  source_record_id: string;
  recorded_at: string;
  refreshed_at: string;
  freshness_status: FreshnessStatus;
  stale_after: string;
  detail: string;
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

export interface LaneSummary {
  context: string;
  instance: string;
  inventory?: EnvironmentInventory | null;
  release_tuple?: ReleaseTupleRecord | null;
  latest_deployment?: DeploymentRecord | null;
  latest_promotion?: PromotionRecord | null;
  latest_backup_gate?: BackupGateRecord | null;
  odoo_instance_override?: unknown | null;
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

export interface DriverListPayload {
  status: "ok";
  trace_id: string;
  drivers: DriverDescriptor[];
}

export interface DriverViewPayload {
  status: "ok";
  trace_id: string;
  view: DriverContextView;
}

export interface AuthIdentity {
  provider: "github";
  login: string;
  github_id: number;
  name: string;
  email: string;
  organizations: string[];
  teams: string[];
  role: "read_only" | "admin";
}

export interface AuthSessionPayload {
  status: "ok";
  trace_id: string;
  identity: AuthIdentity;
}

export interface LogoutPayload {
  status: "ok";
  trace_id: string;
}

export interface ApiErrorPayload {
  status: "rejected";
  trace_id?: string;
  error?: {
    code?: string;
    message?: string;
  };
}

export type ProductConfigMode = "dry-run" | "apply";
export type ProductConfigRuntimeScope = "global" | "context" | "instance";
export type ProductConfigSecretScope =
  | "global"
  | "context"
  | "context_instance";

export interface ProductConfigRuntimeInput {
  scope?: ProductConfigRuntimeScope;
  context?: string;
  instance?: string;
  env: Record<string, string | number | boolean>;
}

export interface ProductConfigSecretInput {
  scope?: ProductConfigSecretScope;
  context?: string;
  instance?: string;
  integration?: string;
  name: string;
  binding_key: string;
  value: string;
  description?: string;
}

export interface ProductConfigApplyRequest {
  schema_version: 1;
  mode: ProductConfigMode;
  product: string;
  context: string;
  instance: string;
  source_label?: string;
  runtime_env?: ProductConfigRuntimeInput;
  secrets?: ProductConfigSecretInput[];
}

export interface ProductConfigRuntimeResult {
  action: "skipped" | "created" | "updated" | "unchanged";
  scope: ProductConfigRuntimeScope | string;
  context: string;
  instance: string;
  keys: string[];
  changed_keys: string[];
  unchanged_keys: string[];
  env_value_count_after: number;
  record?: {
    scope: ProductConfigRuntimeScope | string;
    context: string;
    instance: string;
    updated_at: string;
    source_label: string;
    env_keys: string[];
    env_value_count: number;
  };
}

export interface ProductConfigSecretResult {
  action: "created" | "rotated" | "unchanged";
  scope: ProductConfigSecretScope | string;
  context: string;
  instance: string;
  integration: string;
  name: string;
  binding_key: string;
  secret_id: string;
  description: string;
  value_present: boolean;
}

export interface ProductConfigApplyPayload {
  status: "ok";
  mode: ProductConfigMode;
  product: string;
  context: string;
  instance: string;
  actor: string;
  source_label: string;
  runtime_environment: ProductConfigRuntimeResult;
  secrets: ProductConfigSecretResult[];
  summary: {
    runtime_changed_key_count: number;
    secret_change_count: number;
  };
}

export interface ProductProfileRecord {
  schema_version: number;
  product: string;
  display_name: string;
  repository: string;
  driver_id: string;
  health_path: string;
  lanes: Array<{
    instance: string;
    context: string;
    base_url: string;
    health_url: string;
  }>;
}

export interface ProductProfileListPayload {
  status: "ok";
  trace_id: string;
  driver_id: string;
  profiles: ProductProfileRecord[];
}

export interface GenericWebProdPromotionRequest {
  schema_version: 1;
  product: string;
  promotion: {
    schema_version: 1;
    product: string;
    artifact_id: string;
    source_git_ref: string;
    from_instance: "testing";
    to_instance: "prod";
    timeout_seconds: number;
    health_timeout_seconds: number;
    dry_run: true;
  };
}

export interface GenericWebProdPromotionPayload {
  status: "accepted";
  trace_id: string;
  records: {
    promotion_record_id?: string;
    deployment_record_id?: string;
    backup_record_id?: string;
    inventory_record_id?: string;
  };
  result: {
    promotion_record_id: string;
    deployment_record_id: string;
    backup_record_id: string;
    inventory_record_id: string;
    promotion_status: Status;
    deployment_status: Status;
    source_health_status: Status;
    destination_health_status: Status;
    backup_status: Status;
    dry_run: boolean;
  };
}
