from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

AUTHZ_COMPATIBILITY_FLOOR_REVISION = "f3b5d7e9a1c2"
EXPECTED_ALEMBIC_HEAD_REVISION = "e0f2a4c6d8b1"
RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS = (EXPECTED_ALEMBIC_HEAD_REVISION,)
ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE = "launchplane_ordinary_agent_delivery_activations"
ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE = (
    "launchplane_ordinary_agent_delivery_activation_events"
)
_AUTHZ_POLICY_TABLE = "launchplane_authz_policies"
_AUTHZ_POLICY_WRITE_FENCE_TRIGGER = "launchplane_authz_policy_write_fence"
_AUTHZ_POLICY_WRITE_FENCE_FUNCTION = "launchplane_fence_authz_policy_write"
_MERGE_TRAIN_POLICY_TABLE = "launchplane_merge_train_policies"
_MERGE_TRAIN_POLICY_WRITE_FENCE_TRIGGER = "launchplane_merge_train_policy_write_fence"
_MERGE_TRAIN_POLICY_WRITE_FENCE_FUNCTION = "launchplane_fence_merge_train_policy_write"


class SchemaInspectorProtocol(Protocol):
    def get_indexes(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_columns(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_pk_constraint(self, table_name: str) -> Mapping[str, object]:
        raise NotImplementedError


class SchemaCheckInspectorProtocol(SchemaInspectorProtocol, Protocol):
    def get_check_constraints(self, table_name: str) -> Sequence[Mapping[str, object]]:
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


@dataclass(frozen=True)
class CriticalCheckConstraint:
    table_name: str
    constraint_name: str
    expression: str


ORDINARY_AGENT_ACTIVATION_COLUMN_NAMES: Mapping[str, tuple[str, ...]] = {
    ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE: (
        "activation_id",
        "repository_id",
        "repository",
        "base_branch",
        "managed_set_id",
        "managed_rule_id",
        "source_setup_operation_id",
        "desired_state",
        "effective_state",
        "activation_expires_at",
        "revision",
        "installed_at",
        "updated_at",
        "revoked_at",
        "superseded_by_activation_id",
        "superseded_at",
        "activation_sha256",
        "payload",
    ),
    ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE: (
        "event_id",
        "activation_id",
        "sequence",
        "action",
        "previous_revision",
        "previous_activation_sha256",
        "resulting_revision",
        "resulting_activation_sha256",
        "resulting_desired_state",
        "resulting_effective_state",
        "occurred_at",
        "source_operation_id",
        "payload",
    ),
}

ORDINARY_AGENT_ACTIVATION_POSTGRES_COLUMN_TYPES: tuple[CriticalColumnType, ...] = (
    CriticalColumnType(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE, "repository_id", ("bigint", "int8")
    ),
    CriticalColumnType(ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE, "revision", ("bigint", "int8")),
    CriticalColumnType(ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE, "payload", ("jsonb",)),
    CriticalColumnType(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE, "sequence", ("bigint", "int8")
    ),
    CriticalColumnType(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "previous_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "resulting_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE, "payload", ("jsonb",)),
)


CRITICAL_POSTGRES_COLUMN_TYPES: tuple[CriticalColumnType, ...] = (
    *ORDINARY_AGENT_ACTIVATION_POSTGRES_COLUMN_TYPES,
    CriticalColumnType("launchplane_ordinary_agent_effects", "binding_revision", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_effects", "action_ordinal", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_effects", "revision", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_effects", "payload", ("jsonb",)),
    CriticalColumnType(
        "launchplane_ordinary_agent_semantic_dispatches", "semantic_ordinal", ("bigint",)
    ),
    CriticalColumnType("launchplane_ordinary_agent_semantic_dispatches", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_semantic_outcomes", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_effect_reconciliations", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_effect_completions", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_effect_custody", "payload", ("jsonb",)),
    CriticalColumnType(
        "launchplane_ordinary_agent_landing_preparations", "attempt_ordinal", ("bigint",)
    ),
    CriticalColumnType("launchplane_ordinary_agent_landing_preparations", "payload", ("jsonb",)),
    CriticalColumnType(
        "launchplane_ordinary_agent_landing_bindings", "dispatch_ordinal", ("bigint",)
    ),
    CriticalColumnType("launchplane_ordinary_agent_landing_bindings", "payload", ("jsonb",)),
    CriticalColumnType(
        "launchplane_ordinary_agent_provider_waits", "retry_not_before", ("bigint",)
    ),
    CriticalColumnType("launchplane_ordinary_agent_provider_waits", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_job_claims", "generation", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_job_claims", "claim_expires_at", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_job_claims", "next_due_at", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_job_claims", "released_controller", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_read_attempts", "binding_revision", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_read_attempts", "attempt_ordinal", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_read_attempts", "revision", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_read_attempts", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_read_custody", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_read_outcomes", "payload", ("jsonb",)),
    CriticalColumnType(
        "launchplane_ordinary_agent_candidate_check_observations", "binding_revision", ("bigint",)
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_candidate_check_observations",
        "observation_ordinal",
        ("bigint",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_candidate_check_observations", "payload", ("jsonb",)
    ),
    CriticalColumnType("launchplane_ordinary_agent_sessions", "credential_version", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_leases", "revision", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_sessions", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_leases", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_finite_requests", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_session_operations", "payload", ("jsonb",)),
    CriticalColumnType("launchplane_ordinary_agent_deliveries", "delivery_expires_at", ("bigint",)),
    CriticalColumnType("launchplane_ordinary_agent_deliveries", "credential_version", ("bigint",)),
    CriticalColumnType(
        "launchplane_every_code_feedback_acceptances",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_feedback_resume_intents",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_feedback_resume_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_feedback_resume_receipts",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_feedback_resume_recovery_evidence",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_pull_request_closures",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_merge_train_policies",
        "payload",
        ("jsonb",),
    ),
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
        "launchplane_repository_inventory_records",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_production_backup_targets",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_production_backup_targets",
        "target_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_production_backup_policies",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_production_backup_policies",
        "policy_revision",
        ("bigint", "int8"),
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
        "launchplane_privileged_operations",
        "requester_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_privileged_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_privileged_operation_events",
        "sequence",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_privileged_operation_events",
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
    CriticalColumnType(
        "launchplane_change_impact_policies",
        "audit_payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_control_channel_sessions",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_control_enrollment_provenance",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_control_issued_challenges",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_control_shadow_verification_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_owner_control_challenge_lifecycle_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_administrator_enrollments",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_custody_issue_attempts",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_custody_issue_attempts",
        "repository_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_administrator_enrollments",
        "proposer_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_administrator_enrollments",
        "candidate_github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_administrator_enrollments",
        "enrolled_policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_administrator_enrollments",
        "authorizes_policy",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "active_policy_revision",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "candidate_administrator_quorum",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "candidate_distinct_human_administrator_count",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "github_id",
        ("bigint", "int8"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "authorizes_policy",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmations",
        "secret_sha256",
        ("character varying", "varchar", "text"),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmation_events",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_solo_administration_confirmation_events",
        "authorizes_policy",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_principals",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_principals",
        "is_current",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_authentication_credentials",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_authentication_credentials",
        "is_current",
        ("boolean", "bool"),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_credential_custody",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_ordinary_agent_lifecycle_audits",
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

ORDINARY_AGENT_ACTIVATION_SCHEMA_INDEXES: tuple[CriticalIndex, ...] = (
    CriticalIndex(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_setup_operation_uidx",
        ("source_setup_operation_id",),
        unique=True,
    ),
    CriticalIndex(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_current_scope_uidx",
        (
            "repository_id",
            "base_branch",
            "managed_set_id",
            "managed_rule_id",
        ),
        unique=True,
        predicate_expression="revoked_at is null and superseded_at is null",
    ),
    CriticalIndex(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_scope_history_idx",
        (
            "repository_id",
            "base_branch",
            "managed_set_id",
            "managed_rule_id",
            "installed_at",
        ),
    ),
    CriticalIndex(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_ordinary_agent_activation_event_sequence_uidx",
        ("activation_id", "sequence"),
        unique=True,
    ),
    CriticalIndex(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_agent_activation_event_source_idx",
        ("source_operation_id", "action"),
    ),
)

ORDINARY_AGENT_ACTIVATION_PRIMARY_KEYS: tuple[CriticalPrimaryKey, ...] = (
    CriticalPrimaryKey(ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE, ("activation_id",)),
    CriticalPrimaryKey(ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE, ("event_id",)),
)

ORDINARY_AGENT_ACTIVATION_CHECK_CONSTRAINTS: tuple[CriticalCheckConstraint, ...] = (
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_desired_state_ck",
        "desired_state in ('guarded', 'revoked')",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_effective_state_ck",
        "effective_state in ('qualification_only', 'guarded', 'revoked')",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_revision_ck",
        "revision >= 1",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_state_ck",
        "((desired_state = 'guarded' and effective_state in "
        "('qualification_only', 'guarded') and revoked_at is null) or "
        "(desired_state = 'revoked' and effective_state = 'revoked' "
        "and revoked_at is not null))",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_TABLE,
        "launchplane_ordinary_agent_activation_supersession_ck",
        "((superseded_by_activation_id is null and superseded_at is null) or "
        "(superseded_by_activation_id is not null and superseded_at is not null "
        "and revoked_at is null))",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_ordinary_agent_activation_event_action_ck",
        "action in ('installed', 'guarded_derived', 'readiness_lost', 'revoked', 'superseded')",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_ordinary_agent_activation_event_revision_floor_ck",
        "sequence >= 1 and previous_revision >= 0 and resulting_revision >= 1",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_ordinary_agent_activation_event_transition_ck",
        "((action = 'installed' and sequence = 1 and previous_revision = 0 "
        "and previous_activation_sha256 is null and resulting_revision = 1) or "
        "(action <> 'installed' and previous_revision >= 1 "
        "and previous_activation_sha256 is not null "
        "and resulting_revision = previous_revision + 1))",
    ),
    CriticalCheckConstraint(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_EVENT_TABLE,
        "launchplane_ordinary_agent_activation_event_source_ck",
        "((action in ('installed', 'revoked', 'superseded') "
        "and source_operation_id is not null) or "
        "(action in ('guarded_derived', 'readiness_lost') "
        "and source_operation_id is null))",
    ),
)


CRITICAL_SCHEMA_INDEXES: tuple[CriticalIndex, ...] = (
    *ORDINARY_AGENT_ACTIVATION_SCHEMA_INDEXES,
    CriticalIndex(
        "launchplane_ordinary_agent_effects",
        "ordinary_effect_charge_uq",
        ("lease_id", "action_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_effects",
        "ordinary_effect_semantic_uq",
        ("request_id", "binding_revision", "semantic_key"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_semantic_dispatches",
        "ordinary_dispatch_ordinal_uq",
        ("effect_id", "semantic_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_landing_preparations",
        "ordinary_landing_entry_attempt_uq",
        ("request_id", "binding_revision", "pull_request_number", "attempt_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_landing_preparations",
        "ordinary_landing_predecessor_uq",
        ("predecessor_preparation_id",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_landing_preparations",
        "ordinary_landing_root_charge_uq",
        ("lease_id", "action_ordinal"),
        unique=True,
        predicate_expression="predecessor_preparation_id IS NULL",
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_landing_bindings",
        "ordinary_landing_effect_dispatch_uq",
        ("effect_id", "dispatch_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_landing_bindings",
        "ix_launchplane_ordinary_agent_landing_bindings_effect_id",
        ("effect_id",),
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_read_attempts",
        "ordinary_read_attempt_ordinal_uq",
        ("request_id", "binding_revision", "purpose", "candidate_sha", "attempt_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_read_attempts",
        "ordinary_read_active_uq",
        ("request_id", "binding_revision", "purpose"),
        unique=True,
        predicate_tokens=("reserved", "reading"),
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_candidate_check_observations",
        "ordinary_candidate_observation_uq",
        ("request_id", "binding_revision", "candidate_sha", "observation_ordinal"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_finite_requests",
        "ordinary_finite_request_idempotency_uq",
        ("principal_id", "idempotency_key"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_sessions",
        "ordinary_session_operation_uq",
        ("operation_id", "principal_id", "credential_id", "credential_version"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_deliveries",
        "ordinary_agent_delivery_version_uq",
        ("credential_id", "credential_version"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_resume_intents",
        "launchplane_every_code_feedback_resume_intent_snapshot_uidx",
        ("acceptance_id", "expected_lifecycle_id", "expected_fencing_token"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_acceptances",
        "launchplane_every_code_feedback_acceptance_revision_uidx",
        ("repository_id", "feedback_kind", "feedback_object_id", "revision_digest"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_acceptances",
        "launchplane_every_code_feedback_acceptance_revision_time_uidx",
        ("repository_id", "feedback_kind", "feedback_object_id", "provider_updated_at"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_resume_operations",
        "launchplane_every_code_feedback_resume_operation_intent_uidx",
        ("intent_id",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_resume_operations",
        "launchplane_every_code_feedback_resume_operation_lifecycle_uidx",
        ("request_id", "lifecycle_id"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_feedback_resume_receipts",
        "launchplane_every_code_feedback_resume_receipt_kind_uidx",
        ("operation_id", "receipt_kind"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_every_code_pull_request_closures",
        "launchplane_every_code_pull_request_closure_event_uidx",
        ("request_id", "repository_id", "pr_number", "closed_at"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_administrator_enrollments",
        "launchplane_administrator_enrollment_challenge_uq",
        ("challenge_sha256",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_custody_issue_attempts",
        "launchplane_ordinary_agent_custody_active_fence_uidx",
        ("principal_id", "repository_id"),
        unique=True,
        predicate_expression=("state IN ('minting','issued','issue_unknown','cleanup_unknown')"),
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_custody_issue_attempts",
        "launchplane_ordinary_agent_custody_state_residual_idx",
        ("state", "residual_expires_at"),
    ),
    CriticalIndex(
        "launchplane_administrator_enrollments",
        "launchplane_administrator_enrollment_state_expiry_idx",
        ("state", "expires_at"),
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmations",
        "launchplane_solo_administration_confirmation_issued_binding_uq",
        (
            "reviewed_plan_sha256",
            "human_session_id_sha256",
            "idempotency_scope_sha256",
            "idempotency_key_sha256",
        ),
        unique=True,
        predicate_expression="state='issued'",
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmations",
        "launchplane_solo_administration_confirmation_state_expiry_idx",
        ("state", "expires_at"),
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmations",
        "launchplane_solo_administration_confirmation_session_idx",
        ("human_session_id_sha256", "created_at"),
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmations",
        "lp_solo_admin_confirmation_consumed_recovery_idx",
        ("candidate_policy_sha256", "github_id", "idempotency_scope_sha256", "state"),
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmation_events",
        "lp_solo_admin_confirmation_event_transition_uq",
        ("confirmation_id", "event_type"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_solo_administration_confirmation_events",
        "lp_solo_admin_confirmation_event_confirmation_idx",
        ("confirmation_id", "occurred_at"),
    ),
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
        "launchplane_merge_train_policies",
        "launchplane_merge_train_policies_active_uidx",
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
        "launchplane_privileged_operations",
        "launchplane_privileged_operations_status_idx",
        ("status", "created_at"),
    ),
    CriticalIndex(
        "launchplane_privileged_operations",
        "launchplane_privileged_operations_descriptor_idx",
        ("descriptor_id", "created_at"),
    ),
    CriticalIndex(
        "launchplane_privileged_operation_events",
        "launchplane_privileged_operation_events_operation_sequence_uidx",
        ("operation_id", "sequence"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_privileged_operation_events",
        "launchplane_privileged_operation_events_occurred_idx",
        ("occurred_at",),
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
        "launchplane_repository_inventory_records",
        "launchplane_repository_inventory_revision_uidx",
        ("repository_id", "inventory_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_repository_inventory_records",
        "launchplane_repository_inventory_current_idx",
        ("repository_id", "inventory_revision"),
    ),
    CriticalIndex(
        "launchplane_production_backup_targets",
        "launchplane_production_backup_target_revision_uidx",
        ("target_id", "target_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_production_backup_targets",
        "launchplane_production_backup_target_active_uidx",
        ("target_id",),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_production_backup_targets",
        "launchplane_production_backup_target_current_idx",
        ("target_id", "status", "target_revision"),
    ),
    CriticalIndex(
        "launchplane_production_backup_policies",
        "launchplane_production_backup_policy_revision_uidx",
        ("policy_id", "policy_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_production_backup_policies",
        "launchplane_production_backup_policy_active_uidx",
        ("product", "context", "instance", "promotion_action"),
        unique=True,
        predicate_expression="status='active'",
    ),
    CriticalIndex(
        "launchplane_production_backup_policies",
        "launchplane_production_backup_policy_current_idx",
        (
            "product",
            "context",
            "instance",
            "promotion_action",
            "status",
            "policy_revision",
        ),
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
    CriticalIndex(
        "launchplane_owner_control_channel_sessions",
        "launchplane_owner_control_session_status_idx",
        ("status", "session_expires_at"),
    ),
    CriticalIndex(
        "launchplane_owner_control_issued_challenges",
        "launchplane_owner_control_challenge_session_idx",
        ("channel_session_id", "expires_at"),
    ),
    CriticalIndex(
        "launchplane_owner_control_issued_challenges",
        "launchplane_owner_control_challenge_state_idx",
        ("state", "expires_at"),
    ),
    CriticalIndex(
        "launchplane_owner_control_issued_challenges",
        "launchplane_owner_control_challenge_active_operation_uidx",
        ("operation_id",),
        unique=True,
        predicate_expression="state='issued'",
    ),
    CriticalIndex(
        "launchplane_owner_control_shadow_verification_events",
        "launchplane_owner_control_shadow_event_challenge_idx",
        ("challenge_nonce", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_owner_control_shadow_verification_events",
        "launchplane_owner_control_shadow_event_session_idx",
        ("channel_session_id", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_owner_control_challenge_lifecycle_events",
        "launchplane_owner_control_lifecycle_event_challenge_idx",
        ("challenge_nonce", "occurred_at"),
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_principals",
        "launchplane_ordinary_agent_principal_revision_uidx",
        ("principal_id", "principal_revision"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_principals",
        "launchplane_ordinary_agent_principal_current_uidx",
        ("principal_id",),
        unique=True,
        predicate_expression="is_current",
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_authentication_credentials",
        "launchplane_ordinary_agent_auth_credential_version_uidx",
        ("credential_id", "credential_version"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_authentication_credentials",
        "launchplane_ordinary_agent_auth_credential_current_uidx",
        ("principal_id",),
        unique=True,
        predicate_expression="is_current",
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_credential_custody",
        "launchplane_ordinary_agent_custody_credential_version_uidx",
        ("credential_id", "credential_version"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_ordinary_agent_lifecycle_audits",
        "launchplane_ordinary_agent_lifecycle_audit_operation_uidx",
        ("operation_id",),
        unique=True,
    ),
)

CRITICAL_PRIMARY_KEYS: tuple[CriticalPrimaryKey, ...] = (
    *ORDINARY_AGENT_ACTIVATION_PRIMARY_KEYS,
    CriticalPrimaryKey("launchplane_ordinary_agent_effects", ("effect_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_semantic_dispatches", ("child_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_semantic_outcomes", ("child_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_effect_reconciliations", ("observation_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_effect_completions", ("effect_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_effect_custody", ("attempt_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_landing_preparations", ("preparation_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_landing_bindings", ("preparation_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_provider_waits", ("quota_key_sha256",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_job_claims", ("request_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_read_attempts", ("attempt_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_read_custody", ("custody_attempt_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_read_outcomes", ("attempt_id",)),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_candidate_check_observations", ("observation_id",)
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_session_operations", ("principal_id", "operation_id")
    ),
    CriticalPrimaryKey("launchplane_ordinary_agent_deliveries", ("operation_id",)),
    CriticalPrimaryKey("launchplane_ordinary_agent_delivery_audits", ("event_id",)),
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
        "launchplane_privileged_operations",
        ("operation_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_privileged_operation_events",
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
        "launchplane_repository_inventory_records",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_principals",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_authentication_credentials",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_credential_custody",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_lifecycle_audits",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_production_backup_targets",
        ("record_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_production_backup_policies",
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
    CriticalPrimaryKey(
        "launchplane_owner_control_channel_sessions",
        ("channel_session_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_control_enrollment_provenance",
        ("channel_session_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_control_issued_challenges",
        ("challenge_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_control_shadow_verification_events",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_owner_control_challenge_lifecycle_events",
        ("event_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_administrator_enrollments",
        ("enrollment_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_ordinary_agent_custody_issue_attempts",
        ("attempt_id",),
    ),
    CriticalPrimaryKey(
        "launchplane_solo_administration_confirmations",
        ("confirmation_id",),
    ),
)


def ordinary_agent_delivery_activation_schema_invariants_sha256() -> str:
    payload = {
        "revision": EXPECTED_ALEMBIC_HEAD_REVISION,
        "columns": {
            table_name: list(column_names)
            for table_name, column_names in sorted(ORDINARY_AGENT_ACTIVATION_COLUMN_NAMES.items())
        },
        "postgres_types": [
            {
                "table_name": item.table_name,
                "column_name": item.column_name,
                "accepted_type_tokens": list(item.accepted_type_tokens),
            }
            for item in ORDINARY_AGENT_ACTIVATION_POSTGRES_COLUMN_TYPES
        ],
        "indexes": [
            {
                "table_name": item.table_name,
                "index_name": item.index_name,
                "column_names": list(item.column_names),
                "unique": item.unique,
                "predicate_expression": item.predicate_expression,
            }
            for item in ORDINARY_AGENT_ACTIVATION_SCHEMA_INDEXES
        ],
        "primary_keys": [
            {"table_name": item.table_name, "column_names": list(item.column_names)}
            for item in ORDINARY_AGENT_ACTIVATION_PRIMARY_KEYS
        ],
        "checks": [
            {
                "table_name": item.table_name,
                "constraint_name": item.constraint_name,
                "expression": item.expression,
            }
            for item in ORDINARY_AGENT_ACTIVATION_CHECK_CONSTRAINTS
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def ordinary_agent_delivery_activation_schema_invariant_errors(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    required_tables = set(ORDINARY_AGENT_ACTIVATION_COLUMN_NAMES)
    missing_tables = sorted(required_tables - table_names)
    errors = [f"missing required activation table {table_name}" for table_name in missing_tables]
    present_tables = required_tables & table_names
    for table_name in sorted(present_tables):
        observed_columns = {
            _schema_metadata_text(column.get("name", ""))
            for column in inspector.get_columns(table_name)
        }
        expected_columns = set(ORDINARY_AGENT_ACTIVATION_COLUMN_NAMES[table_name])
        if observed_columns != expected_columns:
            errors.append(
                f"{table_name} columns are {', '.join(sorted(observed_columns)) or '<none>'}; "
                f"expected {', '.join(sorted(expected_columns))}"
            )
    if engine.url.get_backend_name() == "postgresql":
        errors.extend(
            critical_column_type_errors(
                inspector,
                table_names=present_tables,
                expected_types=ORDINARY_AGENT_ACTIVATION_POSTGRES_COLUMN_TYPES,
            )
        )
        index_definitions = postgres_index_definitions(engine)
    else:
        index_definitions = None
    errors.extend(
        critical_index_errors(
            inspector=inspector,
            table_names=present_tables,
            expected_indexes=ORDINARY_AGENT_ACTIVATION_SCHEMA_INDEXES,
            index_definitions=index_definitions,
        )
    )
    errors.extend(
        critical_primary_key_errors(
            inspector,
            table_names=present_tables,
            expected_keys=ORDINARY_AGENT_ACTIVATION_PRIMARY_KEYS,
        )
    )
    errors.extend(
        critical_check_constraint_errors(
            inspector,
            table_names=present_tables,
            expected_checks=ORDINARY_AGENT_ACTIVATION_CHECK_CONSTRAINTS,
        )
    )
    return errors


def ordinary_agent_delivery_activation_schema_capability(
    engine: Engine,
) -> tuple[str, str, bool]:
    expected_digest = ordinary_agent_delivery_activation_schema_invariants_sha256()
    if engine.url.get_backend_name() == "sqlite":
        observed_revision = EXPECTED_ALEMBIC_HEAD_REVISION
        revision_valid = True
    else:
        try:
            observed_revision = _verify_alembic_head(engine)
            revision_valid = True
        except RuntimeError:
            revision_valid = False
            try:
                with engine.connect() as connection:
                    rows = connection.execute(text("select version_num from alembic_version"))
                    revisions = tuple(str(row[0]).strip() for row in rows if str(row[0]).strip())
                observed_revision = revisions[0] if len(revisions) == 1 else "incompatible"
            except SQLAlchemyError:
                observed_revision = "missing"
    errors = ordinary_agent_delivery_activation_schema_invariant_errors(engine)
    return observed_revision, expected_digest, revision_valid and not errors


def verify_postgres_schema_invariants(engine: Engine) -> None:
    backend_name = engine.url.get_backend_name()
    if backend_name != "postgresql":
        raise RuntimeError(
            "Launchplane shared storage requires PostgreSQL for hosted service startup; "
            f"got {backend_name!r}."
        )
    _verify_alembic_head(engine)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    errors = [
        *critical_column_type_errors(inspector),
        *critical_index_errors(
            inspector=inspector,
            table_names=table_names,
            index_definitions=postgres_index_definitions(engine),
        ),
        *critical_primary_key_errors(
            inspector,
            table_names=table_names,
        ),
        *critical_check_constraint_errors(
            inspector,
            table_names=table_names,
            expected_checks=ORDINARY_AGENT_ACTIVATION_CHECK_CONSTRAINTS,
        ),
        *authz_policy_write_fence_errors(engine),
        *merge_train_policy_write_fence_errors(engine),
        *ordinary_effect_write_fence_errors(engine),
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
    expected_keys: tuple[CriticalPrimaryKey, ...] = CRITICAL_PRIMARY_KEYS,
) -> list[str]:
    errors: list[str] = []
    for expected_key in expected_keys:
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


def critical_check_constraint_errors(
    inspector: SchemaCheckInspectorProtocol,
    *,
    table_names: set[str],
    expected_checks: tuple[CriticalCheckConstraint, ...],
) -> list[str]:
    errors: list[str] = []
    for expected_check in expected_checks:
        if expected_check.table_name not in table_names:
            continue
        checks_by_name = {
            _schema_metadata_text(check.get("name", "")): check
            for check in inspector.get_check_constraints(expected_check.table_name)
        }
        observed = checks_by_name.get(expected_check.constraint_name)
        if observed is None:
            errors.append(
                f"{expected_check.table_name} missing required check "
                f"{expected_check.constraint_name}"
            )
            continue
        observed_expression = _canonical_predicate_expression(
            _schema_metadata_text(observed.get("sqltext", ""))
        )
        expected_expression = _canonical_predicate_expression(expected_check.expression)
        if observed_expression != expected_expression:
            errors.append(
                f"{expected_check.constraint_name} has expression "
                f"{observed_expression or '<none>'}; expected {expected_expression}"
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
    return _postgres_write_fence_errors(
        engine=engine,
        table_name=_AUTHZ_POLICY_TABLE,
        trigger_name=_AUTHZ_POLICY_WRITE_FENCE_TRIGGER,
        function_name=_AUTHZ_POLICY_WRITE_FENCE_FUNCTION,
        trigger_fragments=("before insert or update of status",),
        function_fragments=(
            "pg_advisory_xact_lock",
            "new.revision is null",
            "new.status = 'active'",
            "jsonb_set",
        ),
    )


def merge_train_policy_write_fence_errors(engine: Engine) -> list[str]:
    return _postgres_write_fence_errors(
        engine=engine,
        table_name=_MERGE_TRAIN_POLICY_TABLE,
        trigger_name=_MERGE_TRAIN_POLICY_WRITE_FENCE_TRIGGER,
        function_name=_MERGE_TRAIN_POLICY_WRITE_FENCE_FUNCTION,
        trigger_fragments=("before insert or update of status",),
        function_fragments=(
            "pg_advisory_xact_lock",
            "launchplane:active-merge-train-policy",
            "new.status = 'active'",
            "record_id <> new.record_id",
            "jsonb_set",
        ),
    )


def ordinary_effect_write_fence_errors(engine: Engine) -> list[str]:
    errors = _postgres_write_fence_errors(
        engine=engine,
        table_name="launchplane_ordinary_agent_effects",
        trigger_name="ordinary_effect_identity",
        function_name="launchplane_ordinary_effect_identity_guard",
        trigger_fragments=("before insert or delete or update",),
        function_fragments=(
            "new.action_ordinal",
            "old.action_ordinal",
            "ordinary effect identity is immutable",
            "ordinary effect payload identity mismatch",
        ),
    )
    for suffix in (
        "semantic_dispatches",
        "semantic_outcomes",
        "effect_reconciliations",
        "effect_completions",
        "effect_custody",
        "read_custody",
        "read_outcomes",
        "candidate_check_observations",
        "delivery_activation_events",
    ):
        errors.extend(
            _postgres_write_fence_errors(
                engine=engine,
                table_name="launchplane_ordinary_agent_" + suffix,
                trigger_name="ordinary_append_only",
                function_name="launchplane_ordinary_append_only_guard",
                trigger_fragments=("before delete or update",),
                function_fragments=("raise exception", "append-only"),
            )
        )
    for suffix, trigger_name, function_name, fragment in (
        (
            "read_attempts",
            "ordinary_read_identity",
            "launchplane_ordinary_read_identity_guard",
            "ordinary read identity is immutable",
        ),
        (
            "provider_waits",
            "ordinary_provider_wait",
            "launchplane_ordinary_provider_wait_guard",
            "ordinary provider wait cannot move backwards",
        ),
    ):
        errors.extend(
            _postgres_write_fence_errors(
                engine=engine,
                table_name="launchplane_ordinary_agent_" + suffix,
                trigger_name=trigger_name,
                function_name=function_name,
                trigger_fragments=("before insert or delete or update",),
                function_fragments=(fragment,),
            )
        )
    return errors


def _postgres_write_fence_errors(
    *,
    engine: Engine,
    table_name: str,
    trigger_name: str,
    function_name: str,
    trigger_fragments: tuple[str, ...],
    function_fragments: tuple[str, ...],
) -> list[str]:
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
                    "table_name": table_name,
                    "trigger_name": trigger_name,
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
                {"function_name": function_name},
            )
            .mappings()
            .one_or_none()
        )
    errors: list[str] = []
    if trigger_row is None:
        errors.append(f"{table_name} missing required trigger {trigger_name}")
    else:
        if str(trigger_row["function_name"]) != function_name:
            errors.append(
                f"{trigger_name} invokes {trigger_row['function_name']}; expected {function_name}"
            )
        if str(trigger_row["enabled"]) not in {"O", "A"}:
            errors.append(f"{trigger_name} is disabled")
        trigger_definition = " ".join(str(trigger_row["definition"]).lower().split())
        for expected_fragment in (*trigger_fragments, f"execute function {function_name}()"):
            if expected_fragment not in trigger_definition:
                errors.append(f"{trigger_name} definition is missing {expected_fragment!r}")
    if function_row is None:
        errors.append(f"missing required function {function_name}()")
    else:
        function_definition = " ".join(str(function_row["definition"]).lower().split())
        for expected_fragment in function_fragments:
            if expected_fragment not in function_definition:
                errors.append(f"{function_name}() is missing {expected_fragment!r}")
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
    normalized = re.sub(r"::character\s+varying(?:\[\])?", "", normalized)
    normalized = re.sub(r"::[a-z0-9_]+(?:\[\])?", "", normalized)
    normalized = re.sub(
        r"\b([a-z_][a-z0-9_.]*)\s*=\s*any\s*\(\s*array\s*\[(.*?)\]\s*\)",
        lambda match: f"{match.group(1)} in ({match.group(2)})",
        normalized,
    )
    normalized = normalized.replace("(", "").replace(")", "")
    compact = "".join(normalized.split())
    return re.sub(
        r"\b([a-z_][a-z0-9_.]*)=anyarray\[(.*?)\](?:\[\])?",
        lambda match: f"{match.group(1)}in{match.group(2)}",
        compact,
    )
