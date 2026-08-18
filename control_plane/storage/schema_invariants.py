from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any, Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

AUTHZ_COMPATIBILITY_FLOOR_REVISION = "f3b5d7e9a1c2"
EXPECTED_ALEMBIC_HEAD_REVISION = "a2c4e6f8b0d3"
RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS = (EXPECTED_ALEMBIC_HEAD_REVISION,)
_AUTHZ_POLICY_TABLE = "launchplane_authz_policies"
_AUTHZ_POLICY_WRITE_FENCE_TRIGGER = "launchplane_authz_policy_write_fence"
_AUTHZ_POLICY_WRITE_FENCE_FUNCTION = "launchplane_fence_authz_policy_write"


class SchemaInspectorProtocol(Protocol):
    def get_indexes(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_columns(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_pk_constraint(self, table_name: str) -> Mapping[str, object]:
        raise NotImplementedError


def _schema_metadata_text(value: Any) -> str:
    return str(value)


@dataclass(frozen=True)
class CriticalColumnType:
    table_name: str
    column_name: str
    accepted_type_tokens: tuple[str, ...]


@dataclass(frozen=True)
class CriticalIndex:
    table_name: str
    index_name: str
    column_names: tuple[str, ...]
    unique: bool = False
    predicate_tokens: tuple[str, ...] = ()
    predicate_expression: str = ""


@dataclass(frozen=True)
class CriticalPrimaryKey:
    table_name: str
    column_names: tuple[str, ...]


CRITICAL_POSTGRES_COLUMN_TYPES: tuple[CriticalColumnType, ...] = (
    CriticalColumnType(
        "launchplane_merge_admissions",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_merge_admissions",
        "attempt_sequence",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_merge_landing_outcomes",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_merge_landing_outcomes",
        "observation_sequence",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_detached_application_retirements",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_product_retirements",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_idempotency_records",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_idempotency_records",
        "response_status_code",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_idempotency_records",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "fencing_token",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_engineering_review_authorities",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_engineering_review_runs",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_engineering_review_runs",
        "fencing_token",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_engineering_review_decisions",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_bootstrap_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_bootstrap_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_target_replacement_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_target_replacement_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_odoo_prod_backup_restore_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_prod_backup_restore_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_odoo_prod_retained_volume_backup_import_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_prod_retained_volume_backup_import_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_verireel_prod_backup_gate_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_verireel_prod_backup_gate_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_route_bindings",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "max_attempts",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_public_ingress_incidents",
        "state_version",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_public_ingress_incident_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_manager_preview_approval_events",
        "manager_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_manager_preview_approval_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "owner_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "pr_number",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "review_max_age_seconds",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "subject_sequence",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "self_review",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_subject_sequences",
        "pr_number",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_owner_acceptance_subject_sequences",
        "last_sequence",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_public_ingress_incident_reminders",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_merge_train_controller_states",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_authz_policies",
        "revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_authz_policies",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_authz_denials",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_tenant_repository_classifications",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_repository_human_role_policies",
        "role_policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_repository_human_role_policies",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "pull_request_number",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "classification_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "role_policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "authz_policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "author_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_tenant_technical_human_waiver_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_policies",
        "policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_policies",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "pull_request_number",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "classification_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "pr_author_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "sender_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_trusted_maintenance_evidence",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_product_owner_policies",
        "policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_product_owner_policies",
        "quorum",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_product_owner_policies",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_product_owner_requirements",
        "requirement_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_product_owner_requirements",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_product_owner_routing",
        "routing_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_product_owner_routing",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_change_impact_policies",
        "policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_change_impact_policies",
        "payload",
        ("jsonb",),
    ),
)

_ACTIVE_OPERATION_PREDICATE_TOKENS = ("status", "pending", "running")
_ODOO_STABLE_ACTIVE_OPERATION_PREDICATE_TOKENS = (
    "status",
    "pending",
    "running",
    "reconciliation_required",
)

CRITICAL_SCHEMA_INDEXES: tuple[CriticalIndex, ...] = (
    CriticalIndex(
        "launchplane_merge_admissions",
        "launchplane_merge_admissions_attempt_uidx",
        ("attempt_id",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_merge_admissions",
        "launchplane_merge_admissions_binding_uidx",
        ("admission_binding_sha256",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_merge_landing_outcomes",
        "launchplane_merge_landing_outcomes_observation_uidx",
        ("admission_id", "observation_sequence"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_merge_landing_outcomes",
        "launchplane_merge_landing_outcomes_binding_uidx",
        ("outcome_binding_sha256",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_detached_application_retirements",
        "launchplane_detached_app_retirements_plan_idempotency_unique",
        ("candidate_target_sha256", "actor", "idempotency_key"),
        unique=True,
        predicate_expression="mode='plan'",
    ),
    CriticalIndex(
        "launchplane_authz_policies",
        "launchplane_authz_policies_revision_uidx",
        ("revision",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_authz_policies",
        "launchplane_authz_policies_active_uidx",
        ("status",),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_authz_denials",
        "launchplane_authz_denials_recorded_idx",
        ("recorded_at",),
    ),
    CriticalIndex(
        "launchplane_authz_denials",
        "launchplane_authz_denials_expires_idx",
        ("expires_at",),
    ),
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_scope_route_key_idx",
        ("scope", "route_path", "idempotency_key"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_state_lease_idx",
        ("state", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_active_reconciliation_idx",
        ("provider_target_key",),
        unique=True,
        predicate_tokens=("provider_target_key", "running", "reconcile_required"),
    ),
    CriticalIndex(
        "launchplane_every_code_work_requests",
        "launchplane_every_code_work_requests_lease_idx",
        ("state", "lease_expires_at"),
    ),
    CriticalIndex(
        "launchplane_engineering_review_authorities",
        "launchplane_eng_review_authority_active_uidx",
        ("repository",),
        unique=True,
        predicate_tokens=("status", "active"),
    ),
    CriticalIndex(
        "launchplane_engineering_review_runs",
        "launchplane_eng_review_runs_state_lease_idx",
        ("state", "lease_expires_at"),
    ),
    CriticalIndex(
        "launchplane_engineering_review_runs",
        "launchplane_eng_review_runs_assignment_uidx",
        ("assignment_fingerprint",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_engineering_review_runs",
        "launchplane_eng_review_runs_credential_uidx",
        ("credential_hash",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_engineering_review_decisions",
        "launchplane_eng_review_decisions_binding_uidx",
        ("decision_binding_sha256",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_operation_idempotency_idx",
        ("idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ODOO_STABLE_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_operation_idempotency_idx",
        ("idempotency_scope", "idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ODOO_STABLE_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_prod_backup_restore_operations",
        "launchplane_odoo_restore_operation_idempotency_idx",
        ("idempotency_scope", "idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_prod_backup_restore_operations",
        "launchplane_odoo_restore_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ODOO_STABLE_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_prod_backup_restore_operations",
        "launchplane_odoo_restore_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_prod_retained_volume_backup_import_operations",
        "launchplane_odoo_retained_import_operation_idempotency_idx",
        ("operation_kind", "idempotency_scope", "idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_prod_retained_volume_backup_import_operations",
        "launchplane_odoo_retained_import_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ODOO_STABLE_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_prod_retained_volume_backup_import_operations",
        "launchplane_odoo_retained_import_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_verireel_prod_backup_gate_operations",
        "launchplane_verireel_backup_gate_active_record_uidx",
        ("backup_record_id",),
        unique=True,
        predicate_tokens=_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_verireel_prod_backup_gate_operations",
        "launchplane_verireel_backup_gate_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_route_bindings",
        "launchplane_route_bindings_lookup_idx",
        ("product", "context", "status", "instance"),
    ),
    CriticalIndex(
        "launchplane_route_bindings",
        "launchplane_route_bindings_updated_idx",
        ("updated_at",),
    ),
    CriticalIndex(
        "launchplane_outbox_deliveries",
        "launchplane_outbox_deliveries_dedupe_uidx",
        ("dedupe_key",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_outbox_deliveries",
        "launchplane_outbox_deliveries_claim_idx",
        ("state", "next_attempt_at", "lease_expires_at", "created_at"),
    ),
    CriticalIndex(
        "launchplane_public_ingress_observations",
        "launchplane_public_ingress_observations_incident_idx",
        ("incident_id", "observed_at"),
    ),
    CriticalIndex(
        "launchplane_public_ingress_observations",
        "launchplane_public_ingress_observations_check_idx",
        ("product", "context", "instance", "check_token", "check_kind", "observed_at"),
    ),
    CriticalIndex(
        "launchplane_public_ingress_incidents",
        "launchplane_public_ingress_incidents_open_uidx",
        ("product", "context", "instance", "check_token", "check_kind"),
        unique=True,
        predicate_expression="status='open'",
    ),
    CriticalIndex(
        "launchplane_public_ingress_incident_events",
        "launchplane_pi_incident_events_incident_idx",
        ("incident_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_public_ingress_incident_reminders",
        "launchplane_pi_incident_reminders_due_idx",
        ("status", "next_reminder_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_repository_base_idx",
        ("repository", "base_branch", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_status_idx",
        ("status", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_lease_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_manager_preview_approval_events",
        "launchplane_manager_preview_approval_events_subject_idx",
        ("product", "context", "repository", "pr_number", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_manager_preview_approval_events",
        "launchplane_manager_preview_approval_events_preview_idx",
        ("preview_id", "serving_generation_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_manager_preview_approval_events",
        "launchplane_manager_preview_approval_events_approval_idx",
        ("approval_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_owner_acceptance_events",
        "launchplane_owner_acceptance_events_subject_idx",
        (
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            "environment",
            "subject_sequence",
        ),
    ),
    CriticalIndex(
        "launchplane_owner_acceptance_events",
        "launchplane_owner_acceptance_events_subject_sequence_uidx",
        (
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            "environment",
            "subject_sequence",
        ),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_owner_acceptance_events",
        "launchplane_owner_acceptance_events_binding_idx",
        ("binding_sha256", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_owner_acceptance_events",
        "launchplane_owner_acceptance_events_acceptance_idx",
        ("acceptance_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_tenant_repository_classifications",
        "launchplane_tenant_repo_class_revision_uidx",
        ("repository_id", "classification_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_tenant_repository_classifications",
        "launchplane_tenant_repo_class_current_idx",
        ("repository_id", "classification_revision"),
    ),
    CriticalIndex(
        "launchplane_repository_human_role_policies",
        "launchplane_repo_human_role_revision_uidx",
        ("repository_id", "product", "context", "role_policy_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_repository_human_role_policies",
        "launchplane_repo_human_role_active_uidx",
        ("repository_id", "product", "context"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_repository_human_role_policies",
        "launchplane_repo_human_role_current_idx",
        ("repository_id", "product", "context", "status", "role_policy_revision"),
    ),
    CriticalIndex(
        "launchplane_tenant_technical_human_waiver_events",
        "launchplane_tenant_human_waiver_exact_head_idx",
        ("repository_id", "pull_request_number", "head_sha", "occurred_at", "event_id"),
    ),
    CriticalIndex(
        "launchplane_tenant_technical_human_waiver_events",
        "launchplane_tenant_human_waiver_binding_idx",
        ("binding_sha256", "occurred_at", "event_id"),
    ),
    CriticalIndex(
        "launchplane_tenant_technical_human_waiver_events",
        "launchplane_tenant_human_waiver_waiver_idx",
        ("waiver_id", "occurred_at", "event_id"),
    ),
    CriticalIndex(
        "launchplane_tenant_technical_human_waiver_events",
        "launchplane_tenant_human_waiver_policy_idx",
        ("role_policy_record_id", "authz_policy_record_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_policies",
        "launchplane_trusted_maintenance_policy_revision_uidx",
        ("repository_id", "product", "context", "policy_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_policies",
        "launchplane_trusted_maintenance_policy_active_uidx",
        ("repository_id", "product", "context"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_policies",
        "launchplane_trusted_maintenance_policy_current_idx",
        ("repository_id", "product", "context", "status", "policy_revision"),
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_evidence",
        "launchplane_trusted_maintenance_exact_head_idx",
        ("repository_id", "pull_request_number", "head_sha", "occurred_at", "evidence_id"),
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_evidence",
        "launchplane_trusted_maintenance_binding_idx",
        ("binding_sha256", "occurred_at", "evidence_id"),
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_evidence",
        "launchplane_trusted_maintenance_policy_idx",
        ("policy_record_id", "classification_digest", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_trusted_maintenance_evidence",
        "launchplane_trusted_maintenance_actor_event_idx",
        (
            "pr_author_github_id",
            "sender_github_id",
            "event_name",
            "event_action",
            "occurred_at",
        ),
    ),
    CriticalIndex(
        "launchplane_product_owner_policies",
        "launchplane_product_owner_policy_revision_uidx",
        ("product", "system", "policy_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_product_owner_policies",
        "launchplane_product_owner_policy_active_uidx",
        ("product", "system"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_product_owner_policies",
        "launchplane_product_owner_policy_current_idx",
        ("product", "system", "status", "policy_revision"),
    ),
    CriticalIndex(
        "launchplane_product_owner_requirements",
        "launchplane_product_owner_requirement_revision_uidx",
        ("product", "system", "requirement_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_product_owner_requirements",
        "launchplane_product_owner_requirement_active_uidx",
        ("product", "system"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_product_owner_requirements",
        "launchplane_product_owner_requirement_current_idx",
        ("product", "system", "status", "requirement_revision"),
    ),
    CriticalIndex(
        "launchplane_product_owner_routing",
        "launchplane_product_owner_routing_revision_uidx",
        ("product", "system", "routing_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_product_owner_routing",
        "launchplane_product_owner_routing_active_uidx",
        ("product", "system"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_product_owner_routing",
        "launchplane_product_owner_routing_current_idx",
        ("product", "system", "status", "routing_revision"),
    ),
    CriticalIndex(
        "launchplane_change_impact_policies",
        "launchplane_change_impact_policy_revision_uidx",
        ("repository_id", "policy_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_change_impact_policies",
        "launchplane_change_impact_policy_active_uidx",
        ("repository_id",),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_change_impact_policies",
        "launchplane_change_impact_policy_current_idx",
        ("repository_id", "status", "policy_revision"),
    ),
)

CRITICAL_PRIMARY_KEYS: tuple[CriticalPrimaryKey, ...] = (
    CriticalPrimaryKey(
        "launchplane_merge_admissions",
        ("admission_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_merge_landing_outcomes",
        ("outcome_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_detached_application_retirements",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_route_bindings",
        ("product", "context", "instance"),
    ),
    CriticalPrimaryKey(
        "launchplane_manager_preview_approval_events",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_acceptance_events",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_acceptance_subject_sequences",
        (
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            "environment",
        ),
    ),
    CriticalPrimaryKey(
        "launchplane_tenant_repository_classifications",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_repository_human_role_policies",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_tenant_technical_human_waiver_events",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_trusted_maintenance_policies",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_trusted_maintenance_evidence",
        ("evidence_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_product_owner_policies",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_product_owner_requirements",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_product_owner_routing",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_change_impact_policies",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_engineering_review_authorities",
        ("authority_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_engineering_review_runs",
        ("run_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_engineering_review_decisions",
        ("decision_id",),
    ),
)


def verify_postgres_schema_invariants(engine: Engine) -> None:
    backend_name = engine.url.get_backend_name()
    if backend_name != "postgresql":
        raise RuntimeError(
            "Launchplane shared storage requires PostgreSQL for hosted service startup; "
            f"got {backend_name!r}."
        )
    _verify_alembic_head(engine)
    inspector = inspect(engine)
    errors = [
        *critical_column_type_errors(inspector),
        *critical_index_errors(
            inspector=inspector,
            table_names=set(inspector.get_table_names()),
            index_definitions=postgres_index_definitions(engine),
        ),
        *critical_primary_key_errors(
            inspector,
            table_names=set(inspector.get_table_names()),
        ),
        *authz_policy_write_fence_errors(engine),
    ]
    if errors:
        joined_errors = "; ".join(errors)
        raise RuntimeError(
            "Launchplane shared storage schema is missing required PostgreSQL "
            f"invariant(s): {joined_errors}. Run Alembic migrations before "
            "starting the hosted service."
        )


def critical_column_type_errors(
    inspector: SchemaInspectorProtocol,
    *,
    table_names: set[str] | None = None,
    expected_types: tuple[CriticalColumnType, ...] = CRITICAL_POSTGRES_COLUMN_TYPES,
) -> list[str]:
    errors: list[str] = []
    for expected_type in expected_types:
        if table_names is not None and expected_type.table_name not in table_names:
            continue
        columns = {
            _schema_metadata_text(column.get("name", "")): column
            for column in inspector.get_columns(expected_type.table_name)
        }
        column = columns.get(expected_type.column_name)
        if column is None:
            errors.append(
                f"{expected_type.table_name}.{expected_type.column_name} missing type metadata"
            )
            continue
        observed_type = _normalized_type_name(column.get("type"))
        if not any(token in observed_type for token in expected_type.accepted_type_tokens):
            errors.append(
                f"{expected_type.table_name}.{expected_type.column_name} has type "
                f"{observed_type or '<unknown>'}; expected one of "
                f"{', '.join(expected_type.accepted_type_tokens)}"
            )
    return errors


def critical_primary_key_errors(
    inspector: SchemaInspectorProtocol,
    *,
    table_names: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for expected_key in CRITICAL_PRIMARY_KEYS:
        if table_names is not None and expected_key.table_name not in table_names:
            continue
        constraint = inspector.get_pk_constraint(expected_key.table_name)
        observed_columns = tuple(
            _schema_metadata_text(column_name)
            for column_name in _object_sequence(constraint.get("constrained_columns"))
            if _schema_metadata_text(column_name)
        )
        if observed_columns != expected_key.column_names:
            observed_summary = ", ".join(observed_columns) or "<none>"
            expected_summary = ", ".join(expected_key.column_names)
            errors.append(
                f"{expected_key.table_name} has primary key ({observed_summary}); "
                f"expected ({expected_summary})"
            )
    return errors


def critical_index_errors(
    *,
    inspector: SchemaInspectorProtocol,
    table_names: set[str],
    expected_indexes: tuple[CriticalIndex, ...] = CRITICAL_SCHEMA_INDEXES,
    index_definitions: Mapping[tuple[str, str], str] | None = None,
) -> list[str]:
    errors: list[str] = []
    resolved_index_definitions = index_definitions or {}
    for expected_index in expected_indexes:
        if expected_index.table_name not in table_names:
            continue
        indexes_by_name = {
            _schema_metadata_text(index.get("name", "")): index
            for index in inspector.get_indexes(expected_index.table_name)
        }
        observed_index = indexes_by_name.get(expected_index.index_name)
        if observed_index is None:
            errors.append(
                f"{expected_index.table_name} missing required index {expected_index.index_name}"
            )
            continue
        observed_unique = bool(observed_index.get("unique", False))
        if observed_unique != expected_index.unique:
            expected_unique = "unique" if expected_index.unique else "non-unique"
            observed_unique_label = "unique" if observed_unique else "non-unique"
            errors.append(
                f"{expected_index.index_name} is {observed_unique_label}; "
                f"expected {expected_unique}"
            )
        observed_columns = _observed_index_columns(observed_index)
        if observed_columns != expected_index.column_names:
            errors.append(
                f"{expected_index.index_name} covers {', '.join(observed_columns) or '<none>'}; "
                f"expected {', '.join(expected_index.column_names)}"
            )
        if expected_index.predicate_tokens:
            predicate_text = _normalized_predicate_text(
                observed_index,
                resolved_index_definitions.get(
                    (expected_index.table_name, expected_index.index_name), ""
                ),
            )
            missing_tokens = [
                token for token in expected_index.predicate_tokens if token not in predicate_text
            ]
            if missing_tokens:
                errors.append(
                    f"{expected_index.index_name} has predicate "
                    f"{predicate_text or '<none>'}; expected tokens "
                    f"{', '.join(expected_index.predicate_tokens)}"
                )
        if expected_index.predicate_expression:
            predicate_text = _normalized_predicate_text(
                observed_index,
                resolved_index_definitions.get(
                    (expected_index.table_name, expected_index.index_name), ""
                ),
            )
            observed_expression = _canonical_predicate_expression(predicate_text)
            expected_expression = _canonical_predicate_expression(
                expected_index.predicate_expression
            )
            if observed_expression != expected_expression:
                errors.append(
                    f"{expected_index.index_name} has predicate "
                    f"{observed_expression or '<none>'}; expected {expected_expression}"
                )
    return errors


def _verify_alembic_head(engine: Engine) -> str:
    try:
        with engine.connect() as connection:
            version_rows = connection.execute(
                text("select version_num from alembic_version")
            ).fetchall()
    except SQLAlchemyError as error:
        raise RuntimeError(
            "Launchplane shared storage schema is missing Alembic version metadata. "
            "Run Alembic migrations before starting the hosted service."
        ) from error
    version_numbers = tuple(str(row[0]).strip() for row in version_rows if str(row[0]).strip())
    if len(version_numbers) != 1 or version_numbers[0] not in RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS:
        observed = ", ".join(version_numbers) if version_numbers else "<none>"
        raise RuntimeError(
            "Launchplane shared storage schema is not at a compatible Alembic revision: "
            f"observed {observed}; expected one of "
            f"{', '.join(RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS)}. "
            "Run the serialized Launchplane schema migration before starting the hosted service."
        )
    return version_numbers[0]


def authz_policy_write_fence_errors(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        trigger_row = (
            connection.execute(
                text(
                    "select p.proname as function_name, t.tgenabled as enabled, "
                    "pg_get_triggerdef(t.oid) as definition "
                    "from pg_trigger t "
                    "join pg_class c on c.oid = t.tgrelid "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "join pg_proc p on p.oid = t.tgfoid "
                    "where n.nspname = current_schema() "
                    "and c.relname = :table_name "
                    "and t.tgname = :trigger_name "
                    "and not t.tgisinternal"
                ),
                {
                    "table_name": _AUTHZ_POLICY_TABLE,
                    "trigger_name": _AUTHZ_POLICY_WRITE_FENCE_TRIGGER,
                },
            )
            .mappings()
            .one_or_none()
        )
        function_row = (
            connection.execute(
                text(
                    "select pg_get_functiondef(p.oid) as definition "
                    "from pg_proc p "
                    "join pg_namespace n on n.oid = p.pronamespace "
                    "where n.nspname = current_schema() "
                    "and p.proname = :function_name "
                    "and pg_get_function_identity_arguments(p.oid) = ''"
                ),
                {"function_name": _AUTHZ_POLICY_WRITE_FENCE_FUNCTION},
            )
            .mappings()
            .one_or_none()
        )
    errors: list[str] = []
    if trigger_row is None:
        errors.append(
            f"{_AUTHZ_POLICY_TABLE} missing required trigger {_AUTHZ_POLICY_WRITE_FENCE_TRIGGER}"
        )
    else:
        if str(trigger_row["function_name"]) != _AUTHZ_POLICY_WRITE_FENCE_FUNCTION:
            errors.append(
                f"{_AUTHZ_POLICY_WRITE_FENCE_TRIGGER} invokes "
                f"{trigger_row['function_name']}; expected {_AUTHZ_POLICY_WRITE_FENCE_FUNCTION}"
            )
        if str(trigger_row["enabled"]) not in {"O", "A"}:
            errors.append(f"{_AUTHZ_POLICY_WRITE_FENCE_TRIGGER} is disabled")
        trigger_definition = " ".join(str(trigger_row["definition"]).lower().split())
        for expected_fragment in (
            "before insert or update of status",
            f"execute function {_AUTHZ_POLICY_WRITE_FENCE_FUNCTION}()",
        ):
            if expected_fragment not in trigger_definition:
                errors.append(
                    f"{_AUTHZ_POLICY_WRITE_FENCE_TRIGGER} definition is missing "
                    f"{expected_fragment!r}"
                )
    if function_row is None:
        errors.append(f"missing required function {_AUTHZ_POLICY_WRITE_FENCE_FUNCTION}()")
    else:
        function_definition = " ".join(str(function_row["definition"]).lower().split())
        for expected_fragment in (
            "pg_advisory_xact_lock",
            "new.revision is null",
            "new.status = 'active'",
            "jsonb_set",
        ):
            if expected_fragment not in function_definition:
                errors.append(
                    f"{_AUTHZ_POLICY_WRITE_FENCE_FUNCTION}() is missing {expected_fragment!r}"
                )
    return errors


def postgres_index_definitions(engine: Engine) -> dict[tuple[str, str], str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select tablename, indexname, indexdef "
                "from pg_indexes where schemaname = current_schema()"
            )
        ).mappings()
        return {
            (str(row["tablename"]), str(row["indexname"])): str(row["indexdef"]) for row in rows
        }


def _normalized_type_name(type_value: object) -> str:
    tokens = {
        type(type_value).__name__.lower(),
        _schema_metadata_text(type_value).lower(),
    }
    return " ".join(sorted(tokens))


def _observed_index_columns(index: Mapping[str, object]) -> tuple[str, ...]:
    column_names = _object_sequence(index.get("column_names"))
    expressions = _object_sequence(index.get("expressions"))
    observed_columns: list[str] = []
    for position, column_name in enumerate(column_names):
        normalized_column = _normalize_index_column(column_name)
        if not normalized_column and position < len(expressions):
            normalized_column = _normalize_index_column(expressions[position])
        if normalized_column:
            observed_columns.append(normalized_column)
    if not observed_columns:
        for expression in expressions:
            normalized_expression = _normalize_index_column(expression)
            if normalized_expression:
                observed_columns.append(normalized_expression)
    return tuple(observed_columns)


def _object_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _normalize_index_column(value: object) -> str:
    if value is None:
        return ""
    normalized = _schema_metadata_text(value).strip().strip('"').lower()
    if not normalized:
        return ""
    normalized = normalized.split()[0].strip('"')
    return normalized.split("::", maxsplit=1)[0].strip('"')


def _normalized_predicate_text(index: Mapping[str, object], index_definition: str) -> str:
    predicate_parts: list[str] = []
    dialect_options = index.get("dialect_options")
    if isinstance(dialect_options, Mapping):
        for key, value in dialect_options.items():
            if str(key).endswith("_where") and value is not None:
                predicate_parts.append(str(value))
    if not predicate_parts:
        predicate_parts.append(index_definition)
    return " ".join(predicate_parts).lower().replace('"', "")


def _canonical_predicate_expression(value: str) -> str:
    normalized = value.lower().replace('"', "")
    if " where " in normalized:
        normalized = normalized.split(" where ", maxsplit=1)[1]
    normalized = re.sub(r"::[a-z0-9_]+", "", normalized)
    normalized = normalized.replace("(", "").replace(")", "")
    return "".join(normalized.split())
