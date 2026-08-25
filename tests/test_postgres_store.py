import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import MagicMock, Mock, patch

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, insert, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.schema import Index

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.agent_write_intent import (
    AgentWriteIntentEvaluation,
    AgentWriteIntentRecord,
    AgentWriteIntentRequest,
    build_agent_write_intent_record_id,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deploy_target import (
    DeployedTargetReference,
    DeployTargetCategory,
    ProviderTargetRecord,
)
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointStatus
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackDeployPlan,
    GenericWebRollbackPlanRecord,
)
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
    build_launchplane_mutation_reservation,
    complete_launchplane_mutation_reservation,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlanRecord,
    MergeTrainBatchRecordStatus,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    MergeTrainStackCollapseRecordStatus,
    build_merge_train_stack_collapse_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_run_record import (
    MergeTrainRunRecord,
    build_merge_train_run_record,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
)
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryKind,
    OutboxDeliveryRecord,
    OutboxDeliveryState,
    build_outbox_delivery_id,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
    PreviewLifecycleCleanupResult,
)
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentRecord,
    RuntimeEnvironmentScope,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditStatus
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import plan_runner_lane_registration
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyProvenance,
    RepositoryHumanRolePolicyRecord,
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    TenantTechnicalHumanWaiverAuthorization,
    TenantTechnicalHumanWaiverBinding,
    TenantTechnicalHumanWaiverEventRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceActorRule,
    TrustedMaintenanceAllowedEvent,
    TrustedMaintenanceEvidenceBinding,
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
)
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyConflictError,
    RepositoryHumanRolePolicySequenceError,
    TenantTechnicalHumanWaiverApplyEnvelope,
    TenantTechnicalHumanWaiverAuthorizationError,
    TenantTechnicalHumanWaiverExpectedAuthority,
    TenantTechnicalHumanWaiverRevokeCurrentError,
    TenantTechnicalHumanWaiverEventConflictError,
    TenantTechnicalHumanWaiverStaleAuthorityError,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    agent_authz_audit,
)
from control_plane.service_human_auth import LaunchplaneHumanSession
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationConflictError,
    TenantRepositoryClassificationSequenceError,
)
from control_plane.storage.factory import (
    PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS,
    PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS,
    PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS,
    PrivilegedOperationWorkerSchemaError,
    build_privileged_operation_worker_store,
    build_shared_record_store,
)
from control_plane.storage.privileged_operation_worker_probe import (
    PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE,
    PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE,
    run_privileged_operation_worker_schema_probe,
)
from control_plane.storage.postgres import (
    Base,
    DbOnlyMutationRequest,
    LaunchplaneIdempotencyRow,
    LaunchplaneProductProfileRow,
    LaunchplaneTenantTechnicalHumanWaiverEventRow,
    LaunchplaneTenantRepositoryClassificationRow,
    LaunchplaneTrustedMaintenanceEvidenceRow,
    MutationReservationResult,
    OutboxWithIdempotencyRequest,
    PostgresRecordStore,
    _build_engine,
)
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.trusted_maintenance import (
    TrustedMaintenanceEvidenceConflictError,
    TrustedMaintenancePolicyConflictError,
    TrustedMaintenancePolicySequenceError,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills
from tests.support.artifact_manifests import artifact_manifest_v2

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _tenant_repository_classification_record(
    *,
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
    repository: str = "example/example-product",
    product: str = "example-product",
    context: str = "example-product",
    classification_kind: str = "tenant_ui",
    classification_revision: int = 1,
    classified_at: str = "2026-07-31T10:00:00Z",
    source: str = "test-source",
    reason: str = "test-classification",
    supersedes_record_id: str | None = None,
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "product": product,
            "context": context,
            "classification_kind": classification_kind,
            "classification_revision": classification_revision,
            "classified_at": classified_at,
            "source": source,
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _repository_human_role_policy_record(
    *,
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
    repository: str = "example/example-product",
    product: str = "example-product",
    context: str = "example-product",
    status: str = "active",
    role_policy_revision: int = 1,
    repository_owner_github_ids: tuple[int, ...] = (301,),
    manager_primary_github_ids: tuple[int, ...] = (501,),
    effective_at: str = "2026-07-31T10:00:00Z",
    source: str = "test-source",
    reason: str = "test-role-policy",
    supersedes_record_id: str | None = None,
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "product": product,
            "context": context,
            "status": status,
            "role_policy_revision": role_policy_revision,
            "repository_owner_github_ids": repository_owner_github_ids,
            "manager_primary_github_ids": manager_primary_github_ids,
            "effective_at": effective_at,
            "source": source,
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _tenant_technical_human_waiver_event_record(
    *,
    source_event_id: str = "comment-1001",
    action: str = "created",
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
    repository: str = "example/example-product",
    product: str = "example-product",
    context: str = "example-product",
    pull_request_number: int = 17,
    head_sha: str = "a" * 40,
    role_policy_record_id: str = "repository-human-role-policy-1001-abc123-r1",
    role_policy_revision: int = 1,
    author_github_id: int = 301,
    occurred_at: str = "2026-07-31T10:15:00Z",
    expires_at: str = "2026-07-31T11:15:00Z",
) -> TenantTechnicalHumanWaiverEventRecord:
    classification_digest = "b" * 64
    role_policy_digest = "c" * 64
    authz_policy_digest = "d" * 64
    binding = TenantTechnicalHumanWaiverBinding(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        classification_revision=1,
        classification_digest=classification_digest,
        role_policy_record_id=role_policy_record_id,
        role_policy_revision=role_policy_revision,
        role_policy_digest=role_policy_digest,
        authz_policy_record_id="authz-policy-r1",
        authz_policy_revision=1,
        authz_policy_digest=authz_policy_digest,
    )
    provenance = RepositoryHumanRolePolicyProvenance(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
        role_policy_record_id=role_policy_record_id,
        role_policy_revision=role_policy_revision,
        role_policy_digest=role_policy_digest,
        role_policy_source="test-source",
        authority_kind="repository_owner",
        evaluated_at=occurred_at,
    )
    authorization = TenantTechnicalHumanWaiverAuthorization(
        author_github_id=author_github_id,
        author_login=f"human-{author_github_id}",
        managed_set_id="tenant-human.example",
        managed_rule_id="technical-waiver",
        authz_policy_record_id="authz-policy-r1",
        authz_policy_revision=1,
        authz_policy_digest=authz_policy_digest,
        authz_policy_source="test-authz",
        role_policy_provenance=provenance,
        authorized_at=occurred_at,
    )
    payload: dict[str, object] = {
        "binding": binding,
        "action": action,
        "occurred_at": occurred_at,
        "source_event_kind": "github_issue_comment",
        "source_event_id": source_event_id,
        "reason": "Owner approved technical handling.",
        "authorization": authorization,
    }
    if expires_at:
        payload["expires_at"] = expires_at
    return TenantTechnicalHumanWaiverEventRecord.model_validate(payload)


def _trusted_maintenance_policy_record(
    *,
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
    repository: str = "example/example-product",
    product: str = "example-product",
    context: str = "example-product",
    status: str = "active",
    policy_revision: int = 1,
    actor_github_id: int = 701,
    sender_github_ids: tuple[int, ...] = (701,),
    effective_at: str = "2026-07-31T10:00:00Z",
    source: str = "test-source",
    reason: str = "test-trusted-maintenance-policy",
    supersedes_record_id: str | None = None,
) -> TrustedMaintenancePolicyRecord:
    actor_rule = TrustedMaintenanceActorRule(
        actor_github_id=actor_github_id,
        actor_login=f"bot-{actor_github_id}",
        sender_github_ids=sender_github_ids,
        sender_logins=tuple(f"sender-{sender_id}" for sender_id in sender_github_ids),
        allowed_events=(
            TrustedMaintenanceAllowedEvent(
                event_name="pull_request",
                actions=("opened", "synchronize"),
            ),
        ),
    )
    return TrustedMaintenancePolicyRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "product": product,
            "context": context,
            "status": status,
            "policy_revision": policy_revision,
            "actor_rules": (actor_rule,),
            "effective_at": effective_at,
            "source": source,
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _trusted_maintenance_evidence_record(
    *,
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
    repository: str = "example/example-product",
    product: str = "example-product",
    context: str = "example-product",
    pull_request_number: int = 17,
    head_sha: str = "a" * 40,
    policy_record_id: str = "trusted-maintenance-policy-1001-abc123-r1",
    policy_revision: int = 1,
    matched_actor_rule_id: str = "trusted-maintenance-actor-rule-abc123",
    pr_author_github_id: int = 701,
    sender_github_id: int = 701,
    event_name: str = "pull_request",
    event_action: str = "synchronize",
    delivery_id: str = "delivery-1001",
    signed_payload_sha256: str = "d" * 64,
    occurred_at: str = "2026-07-31T10:15:00Z",
    expires_at: str = "",
) -> TrustedMaintenanceEvidenceRecord:
    classification_digest = "b" * 64
    policy_digest = "c" * 64
    binding = TrustedMaintenanceEvidenceBinding(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        classification_record_id="tenant-repository-classification-1001-r1",
        classification_revision=1,
        classification_digest=classification_digest,
        policy_record_id=policy_record_id,
        policy_revision=policy_revision,
        policy_digest=policy_digest,
        matched_actor_rule_id=matched_actor_rule_id,
        pr_author_github_id=pr_author_github_id,
        pr_author_login=f"bot-{pr_author_github_id}",
        sender_github_id=sender_github_id,
        sender_login=f"sender-{sender_github_id}",
        head_repository_id=repository_id,
        head_repository_owner_id=repository_owner_id,
        head_repository=repository,
        event_name=event_name,
        event_action=event_action,
        source="signed-event-fixture",
        delivery_id=delivery_id,
        signed_payload_sha256=signed_payload_sha256,
    )
    payload: dict[str, object] = {
        "binding": binding,
        "occurred_at": occurred_at,
    }
    if expires_at:
        payload["expires_at"] = expires_at
    return TrustedMaintenanceEvidenceRecord.model_validate(payload)


def _product_profile_record(
    *, product: str = "sellyouroutboard"
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context=product,
                base_url="https://sellyouroutboard-testing.shinycomputers.com",
                health_url="https://sellyouroutboard-testing.shinycomputers.com/api/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context=f"{product}-testing",
            slug_template="pr-{number}",
        ),
        updated_at="2026-04-30T20:00:00Z",
        source="operator:test",
    )


def _product_profile_db_only_mutation(
    *, request_fingerprint: str = "fingerprint-a"
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope="github-actions:example",
        route_path="/v1/product-profiles/preview-tls/apply",
        idempotency_key="product-preview-tls:test:apply:1",
        request_fingerprint=request_fingerprint,
        lease_owner="trace-product-preview-tls",
        response_status_code=202,
        response_trace_id="trace-product-preview-tls",
        response_payload={"status": "accepted", "trace_id": "trace-product-preview-tls"},
    )


def _role_policy_db_only_mutation(
    *,
    idempotency_key: str = "role-policy:test:apply:1",
    request_fingerprint: str = "role-policy-fingerprint",
    response_trace_id: str = "trace-role-policy",
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope="github-actions:repository-human-role-policy",
        route_path="/v1/tenant-admission/repository-human-role-policies/apply",
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=response_trace_id,
        response_status_code=202,
        response_trace_id=response_trace_id,
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _tenant_technical_human_waiver_authz_policy_record(
    *,
    github_ids: tuple[int, ...] = (301,),
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.example",
                managed_rule_id="technical-waiver",
                github_ids=github_ids,
                roles=("read_only",),
                products=("example-product",),
                contexts=("example-product",),
                actions=(TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="test:tenant-technical-human-waiver-authz",
        updated_at="2026-07-31T10:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _tenant_technical_human_waiver_envelope(
    *,
    action: str = "created",
    source_event_id: str = "comment-create",
    expected_current: dict[str, object] | None = None,
    reason: str = "Owner reviewed exact technical waiver.",
    classification: TenantRepositoryClassificationRecord | None = None,
    role_policy: RepositoryHumanRolePolicyRecord | None = None,
    authz_policy: LaunchplaneAuthzPolicyRecord | None = None,
) -> TenantTechnicalHumanWaiverApplyEnvelope:
    classification_record = classification or _tenant_repository_classification_record()
    role_policy_record = role_policy or _repository_human_role_policy_record()
    authz_policy_record = authz_policy or _tenant_technical_human_waiver_authz_policy_record()
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "apply",
        "action": action,
        "candidate": {
            "product": "example-product",
            "context": "example-product",
            "repository_id": "1001",
            "repository_owner_id": "2001",
            "repository": "example/example-product",
            "pull_request_number": 17,
            "head_sha": "a" * 40,
        },
        "expected_authority": TenantTechnicalHumanWaiverExpectedAuthority(
            classification_record_id=classification_record.record_id,
            classification_digest=classification_record.classification_digest,
            role_policy_record_id=role_policy_record.record_id,
            role_policy_digest=role_policy_record.role_policy_digest,
            authz_policy_record_id=authz_policy_record.record_id,
            authz_policy_digest=authz_policy_record.policy_sha256,
        ).model_dump(mode="json"),
        "source_event_kind": "github_issue_comment",
        "source_event_id": source_event_id,
        "reason": reason,
    }
    if expected_current is not None:
        payload["expected_current"] = expected_current
    return TenantTechnicalHumanWaiverApplyEnvelope.model_validate(payload)


def _tenant_technical_human_waiver_mutation(
    *,
    idempotency_key: str = "tenant-waiver-create",
    request_fingerprint: str = "tenant-waiver-fingerprint",
    response_trace_id: str = "trace-tenant-waiver",
    scope: str = "github-human-id|301",
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope=scope,
        route_path="/v1/tenant-admission/technical-human-waivers/apply",
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=response_trace_id,
        response_status_code=202,
        response_trace_id=response_trace_id,
        response_payload={"status": "ok", "trace_id": response_trace_id},
    )


def _tenant_technical_human_waiver_identity(
    *,
    github_id: int = 301,
    login: str = "human-301",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Human Example",
        email="human@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


def _seed_tenant_technical_human_waiver_authority(
    store: PostgresRecordStore,
    *,
    classification: TenantRepositoryClassificationRecord | None = None,
    role_policy: RepositoryHumanRolePolicyRecord | None = None,
    authz_policy: LaunchplaneAuthzPolicyRecord | None = None,
) -> tuple[
    TenantRepositoryClassificationRecord,
    RepositoryHumanRolePolicyRecord,
    LaunchplaneAuthzPolicyRecord,
]:
    classification_record = classification or _tenant_repository_classification_record()
    role_policy_record = role_policy or _repository_human_role_policy_record()
    authz_policy_record = authz_policy or _tenant_technical_human_waiver_authz_policy_record()
    store.write_tenant_repository_classification_record(classification_record)
    store.write_repository_human_role_policy_record(role_policy_record)
    store.seed_authz_policy_if_absent(authz_policy_record)
    return classification_record, role_policy_record, authz_policy_record


def _mutation_reservation(
    *,
    request_fingerprint: str = "mutation-fingerprint-a",
    lease_owner: str = "worker-a",
    idempotency_key: str = "mutation:test:1",
    lease_expires_at: str = "2026-07-12T01:05:00Z",
    reserved_at: str = "2026-07-12T01:00:00Z",
) -> LaunchplaneIdempotencyRecord:
    return build_launchplane_mutation_reservation(
        scope="github-actions:mutation-test",
        route_path="/v1/test/mutation",
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        reserved_at=reserved_at,
    )


def _outbox_delivery(
    *,
    kind: OutboxDeliveryKind = "github_workflow_dispatch",
    dedupe_key: str = "github-workflow-dispatch:example/repo:deploy.yml:main:abc123",
    state: OutboxDeliveryState = "pending",
    created_at: str = "2026-07-13T00:00:00Z",
    next_attempt_at: str = "2026-07-13T00:00:00Z",
    provider_operation_key: str = "",
    attempt: int = 0,
    lease_owner: str = "",
    lease_expires_at: str = "",
) -> OutboxDeliveryRecord:
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(kind=kind, dedupe_key=dedupe_key),
        kind=kind,
        state=state,
        aggregate_type="generic_web_promotion_workflow",
        aggregate_id="example-product:example-context",
        dedupe_key=dedupe_key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=next_attempt_at,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        attempt=attempt,
        provider_operation_key=provider_operation_key,
        payload={
            "repository": "example/repo",
            "workflow_id": "deploy.yml",
            "ref": "main",
            "inputs": {"dry_run": "false"},
        },
    )


def _reserve_mutation(
    store: PostgresRecordStore,
    reservation: LaunchplaneIdempotencyRecord,
    *,
    lease_seconds: int = 300,
) -> MutationReservationResult:
    return store.reserve_mutation(
        scope=reservation.scope,
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
        lease_owner=reservation.lease_owner,
        lease_seconds=lease_seconds,
        reconciliation_key=reservation.reconciliation_key,
    )


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _deployment_record(*, record_id: str, started_at: str, finished_at: str) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        context="opw",
        instance="testing",
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id="compose-123",
            target_name="opw-testing",
        ),
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at=started_at,
            finished_at=finished_at,
        ),
    )


def _deployment_record_with_target_id(
    *, record_id: str, target_id: str, started_at: str, finished_at: str
) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        context="opw",
        instance="testing",
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id=target_id,
            target_name="opw-testing",
        ),
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at=started_at,
            finished_at=finished_at,
        ),
    )


def _generic_web_rollback_plan_record(
    *, plan_id: str, created_at: str
) -> GenericWebRollbackPlanRecord:
    return GenericWebRollbackPlanRecord(
        plan_id=plan_id,
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="prod",
        status="ready",
        rollback_deployment_record_id="deployment-syo-prod-previous",
        artifact_identity=ArtifactIdentityReference(
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        ),
        planned_deploy=GenericWebRollbackDeployPlan(
            product="sellyouroutboard",
            instance="prod",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            source_git_ref="abc123",
        ),
        source_git_ref="abc123",
        backup_gate=BackupGateEvidence(required=False, status="skipped"),
        target_health=HealthcheckEvidence(status="pass"),
        created_at=created_at,
        summary="generic web rollback plan is ready",
    )


def _runner_host_hygiene_audit_record(
    *,
    audit_record_key: str,
    status: RunnerHostHygieneApplyAuditStatus = "planned",
    message: str = "planned runner host hygiene apply; no host mutation was executed",
) -> RunnerHostHygieneApplyAuditRecord:
    report = evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
        ),
    )
    request = RunnerHostHygieneApplyRequest(
        action="prune_docker_cache",
        host_name="chris-testing",
        mutate=True,
        retained_warm_builders=("odoo-docker-chris-testing",),
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=("chris-testing",),
            required_retained_warm_builders=("odoo-docker-chris-testing",),
            allow_docker_cache_prune=True,
        ),
        request=request,
        report=report,
    )
    return RunnerHostHygieneApplyAuditRecord(
        audit_record_key=audit_record_key,
        status=status,
        request=request,
        plan=plan,
        pre_apply_report=report,
        post_apply_report=report if status != "planned" else None,
        message=message,
    )


def _runner_lane_registration_audit_record(
    *,
    audit_record_key: str,
    status: RunnerLaneRegistrationAuditStatus = "planned",
    message: str = "planned runner lane registration; no host mutation was executed",
    repository: str = "cbusillo/odoo-tenant-cm-website",
    host_name: str = "chris-testing",
) -> RunnerLaneRegistrationAuditRecord:
    inventory = build_runner_lane_inventory(
        repository=repository,
        observed_at="2026-06-08T17:30:00Z",
        lanes=(),
    )
    request = RunnerLaneRegistrationRequest(
        repository=repository,
        host_name=host_name,
        lane_name="cm-website-runner-1",
        registration_root="/opt/actions-runners",
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=True,
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_lane_registration(
        policy=RunnerLaneRegistrationPolicy(
            allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
            approved_hosts=("chris-testing",),
            allowed_registration_roots=("/opt/actions-runners",),
        ),
        request=request,
        inventory=inventory,
    )
    return RunnerLaneRegistrationAuditRecord(
        audit_record_key=audit_record_key,
        status=status,
        request=request,
        plan=plan,
        pre_inventory=inventory,
        post_inventory=inventory if status != "planned" else None,
        message=message,
    )


def _artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-20260420-a1b2c3d4",
        source_commit="a1b2c3d4",
        enterprise_base_digest="sha256:enterprisebase123",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-opw",
            digest="sha256:image123",
        ),
    )


def _promotion_record(*, record_id: str) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        backup_record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        from_instance="testing",
        to_instance="prod",
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-2",
            status="pass",
            started_at="2026-04-20T16:05:00Z",
            finished_at="2026-04-20T16:07:00Z",
        ),
    )


def _inventory_record() -> EnvironmentInventory:
    return EnvironmentInventory(
        context="opw",
        instance="testing",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at="2026-04-20T15:30:00Z",
            finished_at="2026-04-20T15:32:00Z",
        ),
        updated_at="2026-04-20T15:33:00Z",
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
    )


def _dokploy_target_id_record(
    *, context: str = "opw", instance: str = "prod", target_id: str = "compose-123"
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _dokploy_target_record(
    *, context: str = "opw", instance: str = "prod", project_name: str = ""
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        project_name=project_name,
        target_type="compose",
        target_name=f"{context}-{instance}",
        source_git_ref="origin/main",
        source_type="git",
        custom_git_url="git@github.com:every/odoo-opw.git",
        custom_git_branch=instance,
        compose_path="./docker-compose.yml",
        domains=(f"https://{instance}.example.com",),
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _edge_endpoint_record(
    *,
    endpoint_key: str = "cm-prod-dokploy",
    upstream_host: str = "100.73.170.113",
    status: EdgeEndpointStatus = "active",
) -> EdgeEndpointRecord:
    return EdgeEndpointRecord(
        endpoint_key=endpoint_key,
        provider="dokploy",
        server_name="docker-cm-prod",
        upstream_host=upstream_host,
        upstream_host_kind="ip",
        upstream_scheme="https",
        upstream_port=443,
        status=status,
        updated_at="2026-06-07T00:00:00Z",
        source_label="test:edge-endpoint",
    )


def _private_health_endpoint_record(
    *,
    endpoint_key: str = "repairshopr-sync-prod-runtime",
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "prod",
    status: Literal["active", "disabled"] = "active",
) -> PrivateHealthEndpointRecord:
    return PrivateHealthEndpointRecord(
        endpoint_key=endpoint_key,
        product=product,
        context=context,
        instance=instance,
        url="http://10.0.0.5:8000/health",
        status=status,
        updated_at="2026-06-15T00:00:00Z",
        source_label="test:private-health-endpoint",
    )


def _route_binding_record(
    *, product: str = "example-product", context: str = "reon", instance: str = "prod"
) -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product=product,
        context=context,
        instance=instance,
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-target",
            provider_evidence={"target_record": f"{context}:{instance}"},
        ),
        ingress=RouteBindingIngress(
            provider="npmplus",
            endpoint_key="example-edge",
            termination_kind="edge",
            provider_evidence={"audit_record": "audit-example"},
        ),
        domains=(RouteBindingDomain(domain_name="app.example.test", role="primary"),),
        tls=RouteBindingTls(
            owner="launchplane",
            provider_evidence={"audit_record": "audit-example"},
        ),
        source=RouteBindingSource(
            source_kind="operator",
            source_label="test",
            source_record_ids=("operator:test",),
            refreshed_at="2026-07-12T00:00:00Z",
            freshness_status="recorded",
        ),
        updated_at="2026-07-12T00:00:00Z",
    )


def _ingress_canary_route_record(
    *,
    canary_key: str = "ingress-canary",
    status: str = "active",
) -> IngressCanaryRouteRecord:
    return IngressCanaryRouteRecord(
        canary_key=canary_key,
        product="launchplane",
        context="reon-prod",
        domain_name="ingress-canary.example.test",
        expected_host_id=78,
        edge_endpoint_key="ingress-canary-edge",
        certificate_id=47,
        status=status,  # type: ignore[arg-type]
        updated_at="2026-06-11T00:00:00Z",
        source_label="test:ingress-canary",
    )


def _provider_target_record(
    *,
    context: str = "syo",
    instance: str = "prod",
    provider_id: str = "dokploy",
    target_category: DeployTargetCategory = "application",
    target_id: str = "app-syo-prod",
    provider_target_type: str = "application",
    updated_at: str = "2026-04-21T18:35:00Z",
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance=instance,
        provider_id=provider_id,
        target_category=target_category,
        target_id=target_id,
        display_name=f"{context}-{instance}",
        provider_target_type=provider_target_type,
        provider_evidence={"project_name": f"{context}-project"},
        updated_at=updated_at,
        source_label="test:provider-target",
    )


def _every_code_work_request(
    *,
    request_id: str = "every-code-cbusillo-code-123-test",
    state: str = "queued",
    updated_at: str = "2026-05-05T22:00:00Z",
) -> EveryCodeWorkRequestRecord:
    payload = {
        "request_id": request_id,
        "source": "manual",
        "state": state,
        "repository": "cbusillo/code",
        "issue_number": 123,
        "issue_url": "https://github.com/cbusillo/code/issues/123",
        "trigger_label": "every-code",
        "queued_at": "2026-05-05T22:00:00Z",
        "updated_at": updated_at,
    }
    if state in {"claimed", "running", "done", "blocked"}:
        payload["claimed_at"] = "2026-05-05T22:01:00Z"
        payload["claimed_by_host"] = "Chris-Studio"
    if state in {"running", "done"}:
        payload["started_at"] = "2026-05-05T22:02:00Z"
    if state in {"done", "blocked"}:
        payload["finished_at"] = "2026-05-05T22:03:00Z"
    if state == "blocked":
        payload["error_message"] = "checkout missing"
    return EveryCodeWorkRequestRecord.model_validate(payload)


def _runtime_environment_record(
    *,
    scope: RuntimeEnvironmentScope = "instance",
    context: str = "opw",
    instance: str = "local",
    env: dict[str, str | int | float | bool] | None = None,
) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope=scope,
        context=context if scope != "global" else "",
        instance=instance if scope == "instance" else "",
        env=env or {"ODOO_DB_PASSWORD": "local-secret"},
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _odoo_instance_override_record(
    *, context: str = "opw", instance: str = "prod"
) -> OdooInstanceOverrideRecord:
    return OdooInstanceOverrideRecord(
        context=context,
        instance=instance,
        config_parameters=(
            OdooConfigParameterOverride(
                key="web.base.url",
                value=OdooOverrideValue(
                    source="literal", value=f"https://{context}-{instance}.example.com"
                ),
            ),
        ),
        updated_at="2026-04-21T18:30:00Z",
        source_label="test",
    )


def _preview_record(*, preview_id: str, updated_at: str, pr_number: int) -> PreviewRecord:
    return PreviewRecord(
        preview_id=preview_id,
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=pr_number,
        anchor_pr_url=f"https://github.com/every/verireel/pull/{pr_number}",
        preview_label=f"verireel/pr-{pr_number}",
        canonical_url=f"https://pr-{pr_number}.ver-preview.shinycomputers.com",
        state="active",
        created_at="2026-04-20T10:00:00Z",
        updated_at=updated_at,
        eligible_at=updated_at,
    )


def _preview_generation_record(*, generation_id: str, preview_id: str) -> PreviewGenerationRecord:
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=1,
        state="ready",
        requested_reason="external_preview_refresh",
        requested_at="2026-04-20T10:01:00Z",
        ready_at="2026-04-20T10:05:00Z",
        finished_at="2026-04-20T10:05:00Z",
        resolved_manifest_fingerprint="preview-manifest-123",
        artifact_id="ghcr.io/every/verireel-app:pr-123",
        anchor_summary=PreviewPullRequestSummary(
            repo="verireel",
            pr_number=123,
            head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
            pr_url="https://github.com/every/verireel/pull/123",
        ),
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
    )


def _preview_enablement_record(
    *, record_id: str, updated_at: str, pr_number: int
) -> PreviewEnablementRecord:
    return PreviewEnablementRecord(
        record_id=record_id,
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=pr_number,
        anchor_pr_url=f"https://github.com/every/verireel/pull/{pr_number}",
        anchor_head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        action="labeled",
        pr_state="open",
        updated_at=updated_at,
        label_enabled=True,
        action_label="preview",
        request_metadata_status="valid",
        request_metadata_baseline_channel="testing",
    )


def _backup_gate_record() -> BackupGateRecord:
    return BackupGateRecord(
        record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        instance="prod",
        created_at="2026-04-20T16:00:00Z",
        source="prod-gate",
        status="pass",
        evidence={"snapshot": "opw-predeploy-20260420-160000"},
    )


def _release_tuple_record() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="opw-testing-artifact-20260420-a1b2c3d4",
        context="opw",
        channel="testing",
        artifact_id="artifact-20260420-a1b2c3d4",
        repo_shas={"tenant-opw": "a1b2c3d4", "shared-addons": "abcdef1"},
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        provenance="ship",
        minted_at="2026-04-20T15:33:00Z",
    )


def _secret_record(*, secret_id: str, updated_at: str, current_version_id: str) -> SecretRecord:
    return SecretRecord(
        secret_id=secret_id,
        scope="context_instance",
        integration="dokploy",
        name="api_token",
        context="opw",
        instance="testing",
        description="Dokploy API token",
        current_version_id=current_version_id,
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
        updated_by="launchplane-bootstrap",
    )


def _secret_version(*, version_id: str, secret_id: str, created_at: str) -> SecretVersion:
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at=created_at,
        created_by="launchplane-bootstrap",
        ciphertext="gAAAAABo-bootstrap-ciphertext",
    )


def _secret_binding(*, binding_id: str, secret_id: str, updated_at: str) -> SecretBinding:
    return SecretBinding(
        binding_id=binding_id,
        secret_id=secret_id,
        integration="dokploy",
        binding_key="DOKPLOY_TOKEN",
        context="opw",
        instance="testing",
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
    )


def _secret_audit_event(*, event_id: str, secret_id: str, recorded_at: str) -> SecretAuditEvent:
    return SecretAuditEvent(
        event_id=event_id,
        secret_id=secret_id,
        event_type="imported",
        recorded_at=recorded_at,
        actor="launchplane-bootstrap",
        detail="Imported existing Dokploy secret",
        metadata={"source": "dokploy.env"},
    )


def _human_session(
    *,
    session_id: str = "session-1",
    csrf_generation: int = 0,
) -> LaunchplaneHumanSession:
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    return LaunchplaneHumanSession(
        session_id=session_id,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=12),
        csrf_generation=csrf_generation,
        identity=GitHubHumanIdentity(
            login="alice",
            github_id=123,
            name="Alice Operator",
            email="alice@example.com",
            organizations=frozenset({"shinycomputers"}),
            teams=frozenset({"shinycomputers/launchplane-admins"}),
            role="admin",
        ),
    )


def _agent_write_intent_record(
    *, record_id: str = "", recorded_at: str = "2026-05-08T20:55:00Z"
) -> AgentWriteIntentRecord:
    identity = GitHubActionsIdentity(
        repository="every/verireel",
        repository_owner="every",
        workflow_ref="every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
        job_workflow_ref="",
        ref="refs/heads/main",
        ref_type="branch",
        event_name="pull_request",
        environment="",
        subject="repo:every/verireel:ref:refs/heads/main",
        sha="abc123",
        raw_claims={},
    )
    request = AgentWriteIntentRequest(
        intent="every_code_rerun",
        mode="dry_run",
        product="launchplane",
        context="launchplane",
        source_url="https://github.com/cbusillo/launchplane/issues/386",
        reason="Check whether rerun can be requested safely.",
    )
    audit = agent_authz_audit(
        identity=identity,
        action="every_code_work_request.write",
        product="launchplane",
        context="launchplane",
        decision="allowed",
        reason_code="authorized",
        policy_source="test",
        policy_sha256="abc123",
    )
    evaluation = AgentWriteIntentEvaluation(
        intent="every_code_rerun",
        mode="dry_run",
        status="allowed",
        authz_action="every_code_work_request.write",
        product="launchplane",
        context="launchplane",
        source_url="https://github.com/cbusillo/launchplane/issues/386",
        safe_to_execute=False,
        next_action="Review the dry-run result before requesting apply authority.",
        reason_code="authorized",
        audit=audit,
    )
    resolved_record_id = record_id or build_agent_write_intent_record_id(
        recorded_at=recorded_at,
        trace_id="launchplane_req_test_write_intent",
        request=request,
        evaluation=evaluation,
    )
    return AgentWriteIntentRecord(
        record_id=resolved_record_id,
        recorded_at=recorded_at,
        trace_id="launchplane_req_test_write_intent",
        idempotency_key="intent-eval-1",
        request=request,
        evaluation=evaluation,
    )


def _merge_train_run_record(*, recorded_at: str = "2026-05-09T02:05:00Z") -> MergeTrainRunRecord:
    policy = build_test_merge_train_policy()
    snapshot = MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=(
            MergeTrainPullRequestSnapshot(
                number=42,
                url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                title="Ready merge train PR",
                created_at="2026-05-09T01:00:00Z",
                labels=("ready-to-merge",),
                actor_role="repo_admin",
                head_sha="head-42",
                base_ref="main",
                base_sha="base-main",
                mergeable="mergeable",
                required_checks_status="pass",
            ),
        ),
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    return build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id="launchplane_req_merge_train_test",
        policy_sha256=policy.policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
    )


def _merge_train_batch_candidate_record(
    *,
    record_id: str = "merge-train-batch-candidate-20260514T010000Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:00:00Z",
) -> MergeTrainBatchCandidateRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    base_sha = "base-main"
    entries = (
        MergeTrainBatchEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            title="First queued change",
            url="https://github.com/example/merge-train-repo/pull/10",
        ),
        MergeTrainBatchEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            title="Second queued change",
            url="https://github.com/example/merge-train-repo/pull/11",
        ),
    )
    batch_id = build_merge_train_batch_id(
        repository=repository,
        base_branch=base_branch,
        base_sha=base_sha,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidateRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        candidate=MergeTrainBatchCandidate(
            batch_id=batch_id,
            repository=repository,
            base_branch=base_branch,
            base_sha=base_sha,
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            candidate_ref=build_merge_train_batch_candidate_ref(
                repository=repository,
                base_branch=base_branch,
                batch_id=batch_id,
            ),
            candidate_sha="candidate-sha",
            status="passed",
            entries=entries,
            required_checks_status="pass",
            created_at="2026-05-14T00:59:00Z",
            updated_at=updated_at,
        ),
    )


def _merge_train_controller_state_record(
    *,
    updated_at: str = "2026-05-14T01:00:00Z",
    status: str = "running",
) -> MergeTrainControllerStateRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    return MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        ),
        repository=repository,
        base_branch=base_branch,
        policy_key=f"{repository}:{base_branch}",
        policy_sha256="policy-digest",
        status=status,  # type: ignore[arg-type]
        updated_at=updated_at,
        lease_owner=(
            "github-actions:example/merge-train-repo:run-1001" if status == "running" else ""
        ),
        lease_acquired_at="2026-05-14T00:59:00Z" if status == "running" else "",
        lease_expires_at="2026-05-14T01:05:00Z" if status == "running" else "",
        heartbeat_at=updated_at if status == "running" else "",
        active_action="land_batch" if status == "running" else "",
        active_phase="cleanup_candidate_ref" if status == "running" else "",
        active_record_id="landing-record" if status == "running" else "",
        step_payload=(
            {"candidate_ref": "refs/heads/launchplane/train/example/merge-train-repo/main/batch-1"}
            if status == "running"
            else {}
        ),
        last_owner="github-actions:example/merge-train-repo:run-1000",
        last_action="plan_landing",
        last_phase="planned",
        last_record_id="candidate-record",
        last_transition_at="2026-05-14T00:58:00Z",
        reconciliation_status="clean",
    )


def _merge_train_batch_landing_plan_record(
    *,
    record_id: str = "merge-train-batch-landing-plan-20260514T010500Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:05:00Z",
) -> MergeTrainBatchLandingPlanRecord:
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=_merge_train_batch_candidate_record(updated_at=updated_at).candidate,
        merge_method="squash",
        created_at=updated_at,
    )
    return MergeTrainBatchLandingPlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        landing_plan=landing_plan,
    )


def _merge_train_stack_collapse_plan_record(
    *,
    record_id: str = "merge-train-stack-collapse-plan-20260514T013000Z-active",
    status: MergeTrainStackCollapseRecordStatus = "active",
    updated_at: str = "2026-05-14T01:30:00Z",
) -> MergeTrainStackCollapsePlanRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    entries = (
        MergeTrainStackCollapseEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            head_ref="feature/root",
            base_sha="base-10",
            base_ref="main",
        ),
        MergeTrainStackCollapseEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            head_ref="feature/child",
            base_sha="base-11",
            base_ref="feature/root",
        ),
    )
    collapse_id = build_merge_train_stack_collapse_id(
        repository=repository,
        base_branch=base_branch,
        root_pull_request_number=10,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainStackCollapsePlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        plan=MergeTrainStackCollapsePlan(
            collapse_id=collapse_id,
            repository=repository,
            base_branch=base_branch,
            root_pull_request_number=10,
            root_initial_head_sha="head-10",
            root_head_ref="feature/root",
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            entries=entries,
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=11,
                    parent_pull_request_number=10,
                    child_head_sha="head-11",
                    expected_parent_head_sha="head-10",
                    parent_head_ref="feature/root",
                ),
            ),
            created_at="2026-05-14T01:29:00Z",
            updated_at=updated_at,
        ),
    )


class PostgresRecordStoreTests(unittest.TestCase):
    def test_owner_acceptance_projection_lock_does_not_consume_record_pool(self) -> None:
        store = PostgresRecordStore(
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/launchplane"
        )
        try:
            assert store._owner_acceptance_projection_lock_engine is not None
            self.assertIsInstance(
                store._owner_acceptance_projection_lock_engine.pool,
                NullPool,
            )
        finally:
            store.close()

    def test_owner_acceptance_projection_lock_commits_and_verifies_unlock(self) -> None:
        store = PostgresRecordStore(
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/launchplane"
        )
        original_lock_engine = store._owner_acceptance_projection_lock_engine
        assert original_lock_engine is not None
        original_lock_engine.dispose()
        lock_engine = MagicMock()
        connection = MagicMock()
        connection.scalar.side_effect = (True, True)
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        connection_context.__exit__.return_value = False
        lock_engine.connect.return_value = connection_context
        store._owner_acceptance_projection_lock_engine = lock_engine
        try:
            with store.owner_acceptance_projection_lock(
                repository_id="101",
                pull_request_number=42,
            ):
                connection.commit.assert_called_once_with()

            self.assertEqual(connection.commit.call_count, 2)
            self.assertEqual(connection.scalar.call_count, 2)
            self.assertIn(
                "pg_try_advisory_lock",
                str(connection.scalar.call_args_list[0].args[0]),
            )
            self.assertIn(
                "pg_advisory_unlock",
                str(connection.scalar.call_args_list[1].args[0]),
            )
        finally:
            store.close()

    def test_owner_acceptance_projection_lock_rejects_failed_unlock(self) -> None:
        store = PostgresRecordStore(
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/launchplane"
        )
        original_lock_engine = store._owner_acceptance_projection_lock_engine
        assert original_lock_engine is not None
        original_lock_engine.dispose()
        lock_engine = MagicMock()
        connection = MagicMock()
        connection.scalar.side_effect = (True, False)
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        connection_context.__exit__.return_value = False
        lock_engine.connect.return_value = connection_context
        store._owner_acceptance_projection_lock_engine = lock_engine
        try:
            with self.assertRaisesRegex(RuntimeError, "lock cleanup failed"):
                with store.owner_acceptance_projection_lock(
                    repository_id="101",
                    pull_request_number=42,
                ):
                    pass
            self.assertEqual(connection.commit.call_count, 2)
        finally:
            store.close()

    def test_postgres_metadata_index_names_fit_identifier_limit(self) -> None:
        index_names = tuple(
            index.name
            for table in Base.metadata.tables.values()
            for index in table.indexes
            if isinstance(index, Index) and index.name is not None
        )

        too_long_index_names = tuple(
            index_name for index_name in index_names if len(index_name) > 63
        )

        self.assertEqual(too_long_index_names, ())

    def test_tenant_repository_classifications_are_immutable_revision_history(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _tenant_repository_classification_record(classification_revision=1)
            revision_2 = _tenant_repository_classification_record(
                classification_kind="engineering",
                classification_revision=2,
                classified_at="2026-07-31T10:05:00Z",
                supersedes_record_id=revision_1.record_id,
            )

            first_write = store.write_tenant_repository_classification_record(revision_1)
            second_write = store.write_tenant_repository_classification_record(revision_2)
            replay = store.write_tenant_repository_classification_record(revision_2)
            loaded = store.read_tenant_repository_classification_record(revision_1.record_id)
            listed = store.list_tenant_repository_classification_records(
                repository_id=revision_1.repository_id
            )
            limited = store.list_tenant_repository_classification_records(
                repository_id=revision_1.repository_id,
                limit=1,
            )
            lookup = store.latest_tenant_repository_classification_lookup(
                repository_id=revision_1.repository_id
            )
            missing_lookup = store.latest_tenant_repository_classification_lookup(
                repository_id="9999"
            )
            store.close()

        self.assertEqual(first_write, "written")
        self.assertEqual(second_write, "written")
        self.assertEqual(replay, "replayed")
        self.assertEqual(loaded, revision_1)
        self.assertEqual([record.classification_revision for record in listed], [2, 1])
        self.assertEqual(limited, (revision_2,))
        self.assertEqual(lookup.status, "available")
        self.assertEqual(lookup.records, (revision_2,))
        self.assertEqual(missing_lookup.status, "missing")
        self.assertEqual(missing_lookup.records, ())

    def test_tenant_repository_classification_compare_write_is_atomic_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _tenant_repository_classification_record()
            mutation = DbOnlyMutationRequest(
                scope="github-actions:tenant-classification",
                route_path="/v1/tenant-admission/repository-classifications/apply",
                idempotency_key="tenant-classification-atomic",
                request_fingerprint="tenant-classification-fingerprint",
                lease_owner="trace-tenant-classification",
                response_status_code=200,
                response_trace_id="trace-tenant-classification",
                response_payload={"status": "ok", "record_id": record.record_id},
            )

            first_result = store.compare_and_write_tenant_repository_classification_record(
                record=record,
                expected_current_record_id="",
                mutation=mutation,
            )
            replay_result = store.compare_and_write_tenant_repository_classification_record(
                record=record,
                expected_current_record_id="",
                mutation=mutation,
            )
            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            records = store.list_tenant_repository_classification_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertEqual(first_result.status, "written")
        self.assertEqual(replay_result.status, "replayed")
        self.assertEqual(records, (record,))
        self.assertIsNotNone(stored_idempotency)
        assert stored_idempotency is not None
        self.assertEqual(stored_idempotency.state, "completed")
        self.assertEqual(stored_idempotency.response_payload, mutation.response_payload)

    def test_tenant_repository_classification_compare_write_rolls_back_on_completion_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _tenant_repository_classification_record()
            mutation = DbOnlyMutationRequest(
                scope="github-actions:tenant-classification",
                route_path="/v1/tenant-admission/repository-classifications/apply",
                idempotency_key="tenant-classification-rollback",
                request_fingerprint="tenant-classification-rollback-fingerprint",
                lease_owner="trace-tenant-classification-rollback",
                response_status_code=200,
                response_trace_id="trace-tenant-classification-rollback",
                response_payload={"status": "ok", "record_id": record.record_id},
            )

            with (
                patch.object(
                    store,
                    "_sync_idempotency_row",
                    side_effect=RuntimeError("injected completion failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected completion failure"),
            ):
                store.compare_and_write_tenant_repository_classification_record(
                    record=record,
                    expected_current_record_id="",
                    mutation=mutation,
                )

            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            records = store.list_tenant_repository_classification_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertIsNone(stored_idempotency)
        self.assertEqual(records, ())

    def test_tenant_repository_classification_rejects_conflicting_replay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _tenant_repository_classification_record()
            conflicting_record = _tenant_repository_classification_record(
                reason="changed-test-classification"
            )

            store.write_tenant_repository_classification_record(record)

            with self.assertRaises(TenantRepositoryClassificationConflictError):
                store.write_tenant_repository_classification_record(conflicting_record)

            listed = store.list_tenant_repository_classification_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertEqual(listed, (record,))

    def test_tenant_repository_classification_rejects_invalid_first_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            with self.assertRaises(TenantRepositoryClassificationSequenceError):
                store.write_tenant_repository_classification_record(
                    _tenant_repository_classification_record(classification_revision=2)
                )
            store.close()

    def test_tenant_repository_classification_unique_repository_revision(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _tenant_repository_classification_record()
            raw_conflict = _tenant_repository_classification_record(
                repository="example/other-product",
                product="other-product",
                context="other-product",
            )

            store.write_tenant_repository_classification_record(record)
            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    row = LaunchplaneTenantRepositoryClassificationRow(
                        record_id="tenant-repository-classification-raw-conflict",
                        repository_id=record.repository_id,
                        repository_owner_id=raw_conflict.repository_owner_id,
                        repository=raw_conflict.repository,
                        product=raw_conflict.product,
                        context=raw_conflict.context,
                        classification_kind=raw_conflict.classification_kind,
                        classification_revision=raw_conflict.classification_revision,
                        classified_at=raw_conflict.classified_at,
                        classification_digest=raw_conflict.classification_digest,
                        payload=raw_conflict.model_dump(mode="json"),
                    )
                    session.add(row)
                    session.commit()

            listed = store.list_tenant_repository_classification_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertEqual(listed, (record,))

    def test_repository_human_role_policies_are_single_active_revision_history(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _repository_human_role_policy_record()
            revision_2 = _repository_human_role_policy_record(
                role_policy_revision=2,
                repository_owner_github_ids=(302,),
                effective_at="2026-07-31T10:05:00Z",
                supersedes_record_id=revision_1.record_id,
            )

            first_write = store.write_repository_human_role_policy_record(revision_1)
            second_write = store.write_repository_human_role_policy_record(revision_2)
            replay = store.write_repository_human_role_policy_record(revision_2)
            historical_replay = store.write_repository_human_role_policy_record(revision_1)
            loaded_superseded = store.read_repository_human_role_policy_record(revision_1.record_id)
            loaded_active = store.read_repository_human_role_policy_record(revision_2.record_id)
            listed = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                repository_owner_id=revision_1.repository_owner_id,
                repository=revision_1.repository,
                product=revision_1.product,
                context=revision_1.context,
            )
            active = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                status="active",
            )
            superseded = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                status="superseded",
            )
            limited = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                limit=1,
            )
            wrong_scope = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                product="other-product",
            )
            mixed_case_repository = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                repository="Example/Example-Product",
            )
            store.close()

        self.assertEqual(first_write, "written")
        self.assertEqual(second_write, "written")
        self.assertEqual(replay, "replayed")
        self.assertEqual(historical_replay, "replayed")
        self.assertEqual(loaded_superseded.status, "superseded")
        self.assertEqual(loaded_superseded.role_policy_digest, revision_1.role_policy_digest)
        self.assertEqual(loaded_active, revision_2)
        self.assertEqual([record.role_policy_revision for record in listed], [2, 1])
        self.assertEqual(active, (revision_2,))
        self.assertEqual(superseded, (loaded_superseded,))
        self.assertEqual(limited, (revision_2,))
        self.assertEqual(wrong_scope, ())
        self.assertEqual(mixed_case_repository, (revision_2, loaded_superseded))

    def test_repository_human_role_policy_rejects_replay_sequence_and_scope_drift(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _repository_human_role_policy_record()
            store.write_repository_human_role_policy_record(record)

            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                store.write_repository_human_role_policy_record(
                    _repository_human_role_policy_record(reason="changed-role-policy")
                )
            with self.assertRaises(RepositoryHumanRolePolicySequenceError):
                store.write_repository_human_role_policy_record(
                    _repository_human_role_policy_record(
                        role_policy_revision=3,
                        supersedes_record_id=record.record_id,
                    )
                )
            with self.assertRaises(RepositoryHumanRolePolicySequenceError):
                store.write_repository_human_role_policy_record(
                    _repository_human_role_policy_record(
                        role_policy_revision=2,
                        supersedes_record_id="wrong-record-id",
                    )
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                store.write_repository_human_role_policy_record(
                    _repository_human_role_policy_record(
                        repository_owner_id="2999",
                        role_policy_revision=2,
                        supersedes_record_id=record.record_id,
                    )
                )
            listed = store.list_repository_human_role_policy_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertEqual(listed, (record,))

        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            with self.assertRaises(RepositoryHumanRolePolicySequenceError):
                store.write_repository_human_role_policy_record(
                    _repository_human_role_policy_record(role_policy_revision=2)
                )
            store.close()

    def test_repository_human_role_policy_compare_write_is_atomic_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _repository_human_role_policy_record()
            mutation = DbOnlyMutationRequest(
                scope="github-actions:repository-human-role-policy",
                route_path="/v1/tenant-admission/repository-human-role-policies/apply",
                idempotency_key="role-policy-atomic",
                request_fingerprint="role-policy-fingerprint",
                lease_owner="trace-role-policy",
                response_status_code=202,
                response_trace_id="trace-role-policy",
                response_payload={"status": "ok", "record_id": revision_1.record_id},
                replay_response_payload={
                    "status": "ok",
                    "record_id": revision_1.record_id,
                    "result": "replayed",
                },
            )

            first_result = store.compare_and_write_repository_human_role_policy_record(
                record=revision_1,
                expected_current_record_id="",
                expected_current_role_policy_digest="",
                mutation=mutation,
            )
            same_key_result = store.compare_and_write_repository_human_role_policy_record(
                record=revision_1,
                expected_current_record_id="",
                expected_current_role_policy_digest="",
                mutation=mutation,
            )
            exact_replay_mutation = DbOnlyMutationRequest(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key="role-policy-exact-replay",
                request_fingerprint="role-policy-exact-replay-fingerprint",
                lease_owner="trace-role-policy-exact-replay",
                response_status_code=202,
                response_trace_id="trace-role-policy-exact-replay",
                response_payload={"status": "ok", "record_id": revision_1.record_id},
                replay_response_payload={
                    "status": "ok",
                    "record_id": revision_1.record_id,
                    "result": "replayed",
                },
            )
            exact_replay_result = store.compare_and_write_repository_human_role_policy_record(
                record=revision_1,
                expected_current_record_id="",
                expected_current_role_policy_digest="",
                mutation=exact_replay_mutation,
            )
            conflict_result = store.compare_and_write_repository_human_role_policy_record(
                record=revision_1,
                expected_current_record_id="",
                expected_current_role_policy_digest="",
                mutation=DbOnlyMutationRequest(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    request_fingerprint="changed-role-policy-fingerprint",
                    lease_owner="trace-role-policy-conflict",
                    response_status_code=202,
                    response_trace_id="trace-role-policy-conflict",
                    response_payload={"status": "ok"},
                ),
            )
            records = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                product=revision_1.product,
                context=revision_1.context,
            )
            exact_replay_idempotency = store.read_idempotency_record(
                scope=exact_replay_mutation.scope,
                route_path=exact_replay_mutation.route_path,
                idempotency_key=exact_replay_mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(first_result.status, "written")
        self.assertEqual(same_key_result.status, "replayed")
        self.assertEqual(exact_replay_result.status, "exact_replay")
        self.assertEqual(conflict_result.status, "idempotency_conflict")
        self.assertEqual(records, (revision_1,))
        self.assertIsNotNone(exact_replay_idempotency)
        assert exact_replay_idempotency is not None
        self.assertEqual(
            exact_replay_idempotency.response_payload,
            exact_replay_mutation.replay_response_payload,
        )

    def test_repository_human_role_policy_compare_write_validates_expected_tip(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _repository_human_role_policy_record()
            revision_2 = _repository_human_role_policy_record(
                role_policy_revision=2,
                repository_owner_github_ids=(302,),
                effective_at="2026-07-31T10:05:00Z",
                supersedes_record_id=revision_1.record_id,
            )
            store.write_repository_human_role_policy_record(revision_1)

            with self.assertRaisesRegex(ValueError, "expected current record ID and digest"):
                store.compare_and_write_repository_human_role_policy_record(
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    expected_current_role_policy_digest="",
                    mutation=_role_policy_db_only_mutation(
                        idempotency_key="role-policy-missing-digest"
                    ),
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                store.compare_and_write_repository_human_role_policy_record(
                    record=revision_2,
                    expected_current_record_id="wrong-record-id",
                    expected_current_role_policy_digest=revision_1.role_policy_digest,
                    mutation=_role_policy_db_only_mutation(idempotency_key="role-policy-stale"),
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                store.compare_and_write_repository_human_role_policy_record(
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    expected_current_role_policy_digest="f" * 64,
                    mutation=_role_policy_db_only_mutation(idempotency_key="role-policy-drift"),
                )
            records = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                product=revision_1.product,
                context=revision_1.context,
            )
            failed_reservation = store.read_idempotency_record(
                scope="github-actions:repository-human-role-policy",
                route_path="/v1/tenant-admission/repository-human-role-policies/apply",
                idempotency_key="role-policy-drift",
            )
            store.close()

        self.assertEqual(records, (revision_1,))
        self.assertIsNone(failed_reservation)

    def test_repository_human_role_policy_compare_write_replays_revision_two_with_new_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _repository_human_role_policy_record()
            revision_2 = _repository_human_role_policy_record(
                role_policy_revision=2,
                repository_owner_github_ids=(302,),
                effective_at="2026-07-31T10:05:00Z",
                supersedes_record_id=revision_1.record_id,
            )
            store.write_repository_human_role_policy_record(revision_1)
            written = store.compare_and_write_repository_human_role_policy_record(
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mutation=_role_policy_db_only_mutation(idempotency_key="role-policy-revision-2"),
            )
            replayed = store.compare_and_write_repository_human_role_policy_record(
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mutation=_role_policy_db_only_mutation(
                    idempotency_key="role-policy-revision-2-replay",
                    request_fingerprint="role-policy-revision-2-replay-fingerprint",
                    response_trace_id="trace-role-policy-revision-2-replay",
                ),
            )
            records = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                product=revision_1.product,
                context=revision_1.context,
            )
            store.close()

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "exact_replay")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], revision_2)
        self.assertEqual(records[1].record_id, revision_1.record_id)
        self.assertEqual(records[1].status, "superseded")

    def test_repository_human_role_policy_compare_write_rolls_back_on_completion_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _repository_human_role_policy_record()
            mutation = _role_policy_db_only_mutation(
                idempotency_key="role-policy-rollback",
                response_trace_id="trace-role-policy-rollback",
            )

            with (
                patch.object(
                    store,
                    "_sync_idempotency_row",
                    side_effect=RuntimeError("injected completion failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected completion failure"),
            ):
                store.compare_and_write_repository_human_role_policy_record(
                    record=record,
                    expected_current_record_id="",
                    expected_current_role_policy_digest="",
                    mutation=mutation,
                )

            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            records = store.list_repository_human_role_policy_records(
                repository_id=record.repository_id,
                product=record.product,
                context=record.context,
            )
            store.close()

        self.assertIsNone(stored_idempotency)
        self.assertEqual(records, ())

    def test_repository_human_role_policy_database_uniqueness(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _repository_human_role_policy_record()
            raw_duplicate_revision = _repository_human_role_policy_record(
                repository_owner_id="2999",
                repository="example/other-product",
                reason="raw duplicate stream revision",
            )
            raw_second_active = _repository_human_role_policy_record(
                role_policy_revision=2,
                reason="raw second active",
                supersedes_record_id=record.record_id,
            )
            store.write_repository_human_role_policy_record(record)

            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    duplicate_revision_row = store._repository_human_role_policy_row(
                        raw_duplicate_revision
                    )
                    duplicate_revision_row.record_id = "raw-duplicate-role-policy"
                    session.add(duplicate_revision_row)
                    session.commit()
            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    session.add(store._repository_human_role_policy_row(raw_second_active))
                    session.commit()

            active = store.list_repository_human_role_policy_records(
                repository_id=record.repository_id,
                status="active",
            )
            store.close()

        self.assertEqual(active, (record,))

    def test_trusted_maintenance_policies_are_single_active_revision_history(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            revision_1 = _trusted_maintenance_policy_record()
            revision_2 = _trusted_maintenance_policy_record(
                policy_revision=2,
                actor_github_id=702,
                effective_at="2026-07-31T10:05:00Z",
                supersedes_record_id=revision_1.record_id,
            )

            first_write = store.write_trusted_maintenance_policy_record(revision_1)
            second_write = store.write_trusted_maintenance_policy_record(revision_2)
            replay = store.write_trusted_maintenance_policy_record(revision_2)
            historical_replay = store.write_trusted_maintenance_policy_record(revision_1)
            loaded_superseded = store.read_trusted_maintenance_policy_record(revision_1.record_id)
            loaded_active = store.read_trusted_maintenance_policy_record(revision_2.record_id)
            listed = store.list_trusted_maintenance_policy_records(
                repository_id=revision_1.repository_id,
                repository_owner_id=revision_1.repository_owner_id,
                repository="Example/Example-Product",
                product=revision_1.product,
                context=revision_1.context,
            )
            active = store.list_trusted_maintenance_policy_records(
                repository_id=revision_1.repository_id,
                status="active",
            )
            superseded = store.list_trusted_maintenance_policy_records(
                repository_id=revision_1.repository_id,
                status="superseded",
            )
            limited = store.list_trusted_maintenance_policy_records(
                repository_id=revision_1.repository_id,
                limit=1,
            )
            store.close()

        self.assertEqual(first_write, "written")
        self.assertEqual(second_write, "written")
        self.assertEqual(replay, "replayed")
        self.assertEqual(historical_replay, "replayed")
        self.assertEqual(loaded_superseded.status, "superseded")
        self.assertEqual(loaded_superseded.policy_digest, revision_1.policy_digest)
        self.assertEqual(loaded_active, revision_2)
        self.assertEqual([record.policy_revision for record in listed], [2, 1])
        self.assertEqual(active, (revision_2,))
        self.assertEqual(superseded, (loaded_superseded,))
        self.assertEqual(limited, (revision_2,))

    def test_trusted_maintenance_policy_rejects_replay_sequence_and_scope_drift(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _trusted_maintenance_policy_record()
            store.write_trusted_maintenance_policy_record(record)

            with self.assertRaises(TrustedMaintenancePolicyConflictError):
                store.write_trusted_maintenance_policy_record(
                    _trusted_maintenance_policy_record(reason="changed-policy")
                )
            with self.assertRaises(TrustedMaintenancePolicySequenceError):
                store.write_trusted_maintenance_policy_record(
                    _trusted_maintenance_policy_record(
                        policy_revision=3,
                        supersedes_record_id=record.record_id,
                    )
                )
            with self.assertRaises(TrustedMaintenancePolicySequenceError):
                store.write_trusted_maintenance_policy_record(
                    _trusted_maintenance_policy_record(
                        policy_revision=2,
                        supersedes_record_id="wrong-record-id",
                    )
                )
            with self.assertRaises(TrustedMaintenancePolicyConflictError):
                store.write_trusted_maintenance_policy_record(
                    _trusted_maintenance_policy_record(
                        repository_owner_id="2999",
                        policy_revision=2,
                        supersedes_record_id=record.record_id,
                    )
                )
            listed = store.list_trusted_maintenance_policy_records(
                repository_id=record.repository_id
            )
            store.close()

        self.assertEqual(listed, (record,))

    def test_trusted_maintenance_policy_database_uniqueness(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _trusted_maintenance_policy_record()
            raw_duplicate_revision = _trusted_maintenance_policy_record(
                repository_owner_id="2999",
                repository="example/other-product",
                reason="raw duplicate stream revision",
            )
            raw_second_active = _trusted_maintenance_policy_record(
                policy_revision=2,
                reason="raw second active",
                supersedes_record_id=record.record_id,
            )
            store.write_trusted_maintenance_policy_record(record)

            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    duplicate_revision_row = store._trusted_maintenance_policy_row(
                        raw_duplicate_revision
                    )
                    duplicate_revision_row.record_id = "raw-duplicate-trusted-maintenance-policy"
                    session.add(duplicate_revision_row)
                    session.commit()
            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    session.add(store._trusted_maintenance_policy_row(raw_second_active))
                    session.commit()

            active = store.list_trusted_maintenance_policy_records(
                repository_id=record.repository_id,
                status="active",
            )
            store.close()

        self.assertEqual(active, (record,))

    def test_trusted_maintenance_evidence_is_append_only_and_filterable(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            evidence = _trusted_maintenance_evidence_record()
            conflicting = _trusted_maintenance_evidence_record(
                head_sha="b" * 40,
            )
            other_repository = _trusted_maintenance_evidence_record(
                repository_id="1002",
                repository_owner_id="2002",
                repository="example/other-product",
                product="other-product",
                context="other-product",
                delivery_id="delivery-other",
                signed_payload_sha256="e" * 64,
            )

            first_write = store.write_trusted_maintenance_evidence_record(evidence)
            replay = store.write_trusted_maintenance_evidence_record(evidence)
            with self.assertRaises(TrustedMaintenanceEvidenceConflictError):
                store.write_trusted_maintenance_evidence_record(conflicting)
            store.write_trusted_maintenance_evidence_record(other_repository)
            loaded = store.read_trusted_maintenance_evidence_record(evidence.evidence_id)
            listed = store.list_trusted_maintenance_evidence_records(
                repository_id=evidence.binding.repository_id,
            )
            exact = store.list_trusted_maintenance_evidence_records(
                repository_id=evidence.binding.repository_id,
                repository_owner_id=evidence.binding.repository_owner_id,
                repository="Example/Example-Product",
                product=evidence.binding.product,
                context=evidence.binding.context,
                binding_sha256=evidence.binding.binding_sha256,
                pull_request_number=evidence.binding.pull_request_number,
                head_sha=evidence.binding.head_sha,
                classification_digest=evidence.binding.classification_digest,
                policy_record_id=evidence.binding.policy_record_id,
                policy_digest=evidence.binding.policy_digest,
                matched_actor_rule_id=evidence.binding.matched_actor_rule_id,
                pr_author_github_id=evidence.binding.pr_author_github_id,
                sender_github_id=evidence.binding.sender_github_id,
                event_name=evidence.binding.event_name,
                event_action=evidence.binding.event_action,
                delivery_id=evidence.binding.delivery_id,
            )
            wrong_head = store.list_trusted_maintenance_evidence_records(
                repository_id=evidence.binding.repository_id,
                head_sha="b" * 40,
            )
            store.close()

        self.assertEqual(first_write, "written")
        self.assertEqual(replay, "replayed")
        self.assertEqual(loaded, evidence)
        self.assertEqual(listed, (evidence,))
        self.assertEqual(exact, (evidence,))
        self.assertEqual(wrong_head, ())

    def test_trusted_maintenance_evidence_database_checks(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _trusted_maintenance_evidence_record()
            build_row = store._trusted_maintenance_evidence_row

            def invalid_row(
                evidence: TrustedMaintenanceEvidenceRecord,
            ) -> LaunchplaneTrustedMaintenanceEvidenceRow:
                row = build_row(evidence)
                row.evidence_id = "raw-invalid-trusted-maintenance-evidence"
                row.sender_github_id = 0
                return row

            with patch.object(
                store,
                "_trusted_maintenance_evidence_row",
                side_effect=invalid_row,
            ):
                with self.assertRaises(IntegrityError):
                    store.write_trusted_maintenance_evidence_record(record)
            store.close()

    def test_trusted_maintenance_evidence_database_requires_same_head_repository(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _trusted_maintenance_evidence_record()
            row = store._trusted_maintenance_evidence_row(record)
            row.evidence_id = "raw-fork-head-evidence"
            row.head_repository_id = "9999"

            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    session.add(row)
                    session.commit()
            store.close()

    def test_trusted_maintenance_direct_policy_write_serializes_replay_race(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            record = _trusted_maintenance_policy_record()

            def write_once() -> str:
                active_store = PostgresRecordStore(database_url=database_url)
                try:
                    return active_store.write_trusted_maintenance_policy_record(record)
                finally:
                    active_store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: write_once(), range(2)))
            records = store.list_trusted_maintenance_policy_records(
                repository_id=record.repository_id,
                product=record.product,
                context=record.context,
            )
            store.close()

        self.assertEqual(set(results), {"written", "replayed"})
        self.assertEqual(records, (record,))

    def test_trusted_maintenance_compare_write_serializes_stale_cas_race(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            revision_1 = _trusted_maintenance_policy_record()
            revision_2_a = _trusted_maintenance_policy_record(
                policy_revision=2,
                actor_github_id=702,
                supersedes_record_id=revision_1.record_id,
            )
            revision_2_b = _trusted_maintenance_policy_record(
                policy_revision=2,
                actor_github_id=703,
                supersedes_record_id=revision_1.record_id,
            )
            store.compare_and_write_trusted_maintenance_policy_record(
                revision_1,
                expected_current_record_id="",
                expected_current_policy_digest="",
            )

            def write_revision(record: TrustedMaintenancePolicyRecord) -> str:
                active_store = PostgresRecordStore(database_url=database_url)
                try:
                    return active_store.compare_and_write_trusted_maintenance_policy_record(
                        record,
                        expected_current_record_id=revision_1.record_id,
                        expected_current_policy_digest=revision_1.policy_digest,
                    )
                except TrustedMaintenancePolicyConflictError:
                    return "conflict"
                finally:
                    active_store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(write_revision, (revision_2_a, revision_2_b)))
            records = store.list_trusted_maintenance_policy_records(
                repository_id=revision_1.repository_id,
                product=revision_1.product,
                context=revision_1.context,
            )
            store.close()

        self.assertEqual(set(results), {"written", "conflict"})
        self.assertEqual([record.policy_revision for record in records], [2, 1])
        self.assertEqual([record.status for record in records], ["active", "superseded"])

    def test_tenant_technical_human_waiver_events_are_append_only_and_filterable(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            created = _tenant_technical_human_waiver_event_record()
            conflicting_created = _tenant_technical_human_waiver_event_record(
                occurred_at="2026-07-31T10:16:00Z",
                expires_at="2026-07-31T11:16:00Z",
            )
            revoked = _tenant_technical_human_waiver_event_record(
                source_event_id="comment-1002",
                action="revoked",
                occurred_at="2026-07-31T10:30:00Z",
                expires_at="",
            )
            other_repository = _tenant_technical_human_waiver_event_record(
                source_event_id="comment-other",
                repository_id="1002",
                repository_owner_id="2002",
                repository="example/other-product",
                product="other-product",
                context="other-product",
            )

            first_write = store.write_tenant_technical_human_waiver_event_record(created)
            replay = store.write_tenant_technical_human_waiver_event_record(created)
            with self.assertRaises(TenantTechnicalHumanWaiverEventConflictError):
                store.write_tenant_technical_human_waiver_event_record(conflicting_created)
            revoked_write = store.write_tenant_technical_human_waiver_event_record(revoked)
            store.write_tenant_technical_human_waiver_event_record(other_repository)
            loaded = store.read_tenant_technical_human_waiver_event_record(created.event_id)
            listed = store.list_tenant_technical_human_waiver_event_records(
                repository_id=created.binding.repository_id,
            )
            exact_created = store.list_tenant_technical_human_waiver_event_records(
                repository_id=created.binding.repository_id,
                repository_owner_id=created.binding.repository_owner_id,
                repository="Example/Example-Product",
                product=created.binding.product,
                context=created.binding.context,
                binding_sha256=created.binding.binding_sha256,
                pull_request_number=created.binding.pull_request_number,
                head_sha=created.binding.head_sha,
                classification_digest=created.binding.classification_digest,
                role_policy_record_id=created.binding.role_policy_record_id,
                role_policy_digest=created.binding.role_policy_digest,
                authz_policy_record_id=created.binding.authz_policy_record_id,
                authz_policy_digest=created.binding.authz_policy_digest,
                action="created",
                author_github_id=created.authorization.author_github_id,
            )
            waiver_events = store.list_tenant_technical_human_waiver_event_records(
                waiver_id=created.waiver_id,
                limit=1,
            )
            wrong_head = store.list_tenant_technical_human_waiver_event_records(
                repository_id=created.binding.repository_id,
                head_sha="b" * 40,
            )
            store.close()

        self.assertEqual(first_write, "written")
        self.assertEqual(replay, "replayed")
        self.assertEqual(revoked_write, "written")
        self.assertEqual(loaded, created)
        self.assertEqual(listed, (revoked, created))
        self.assertEqual(exact_created, (created,))
        self.assertEqual(waiver_events, (revoked,))
        self.assertEqual(wrong_head, ())

    def test_tenant_technical_human_waiver_event_database_checks(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _tenant_technical_human_waiver_event_record()
            build_row = store._tenant_technical_human_waiver_event_row

            def invalid_row(
                event: TenantTechnicalHumanWaiverEventRecord,
            ) -> LaunchplaneTenantTechnicalHumanWaiverEventRow:
                row = build_row(event)
                row.event_id = "raw-invalid-waiver-event"
                row.action = "mutated"
                return row

            with patch.object(
                store,
                "_tenant_technical_human_waiver_event_row",
                side_effect=invalid_row,
            ):
                with self.assertRaises(IntegrityError):
                    store.write_tenant_technical_human_waiver_event_record(record)
            store.close()

    def test_tenant_technical_human_waiver_compare_write_create_replay_and_conflict(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)
            envelope = _tenant_technical_human_waiver_envelope()
            mutation = _tenant_technical_human_waiver_mutation()

            first_result = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=envelope,
                mutation=mutation,
            )
            same_key_result = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(login="renamed-human"),
                envelope=envelope,
                mutation=mutation,
            )
            conflict_result = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=envelope,
                mutation=_tenant_technical_human_waiver_mutation(
                    request_fingerprint="changed-waiver-fingerprint"
                ),
            )
            event_records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(first_result.status, "written")
        self.assertIsNotNone(first_result.result)
        self.assertIsNotNone(first_result.event_record)
        assert first_result.result is not None
        assert first_result.event_record is not None
        self.assertEqual(first_result.result.status, "applied")
        self.assertEqual(first_result.result.path_result.state, "satisfied")
        self.assertEqual(
            first_result.event_record.recorded_at, first_result.event_record.occurred_at
        )
        self.assertEqual(first_result.result.recorded_at, first_result.result.occurred_at)
        self.assertEqual(same_key_result.status, "replayed")
        self.assertIsNotNone(same_key_result.idempotency_record)
        assert same_key_result.idempotency_record is not None
        self.assertEqual(
            same_key_result.idempotency_record.response_trace_id,
            mutation.response_trace_id,
        )
        self.assertEqual(conflict_result.status, "idempotency_conflict")
        self.assertEqual(event_records, (first_result.event_record,))
        self.assertIsNotNone(stored_idempotency)
        assert stored_idempotency is not None
        self.assertEqual(stored_idempotency.response_payload["result"]["status"], "applied")

    def test_tenant_technical_human_waiver_compare_write_exact_create_replay_with_new_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)
            envelope = _tenant_technical_human_waiver_envelope()
            timestamp = "2026-08-01T12:00:00Z"

            with patch.object(store, "_database_mutation_timestamp", return_value=timestamp):
                written = store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=envelope,
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-create-fixed",
                        request_fingerprint="tenant-waiver-create-fixed-fingerprint",
                        response_trace_id="trace-waiver-create-fixed",
                    ),
                )
                replayed = store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=envelope,
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-create-fixed-replay",
                        request_fingerprint="tenant-waiver-create-fixed-replay-fingerprint",
                        response_trace_id="trace-waiver-create-fixed-replay",
                    ),
                )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            store.close()

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "exact_replay")
        self.assertIsNotNone(written.event_record)
        self.assertIsNotNone(replayed.event_record)
        assert written.event_record is not None
        assert replayed.event_record is not None
        self.assertEqual(written.event_record, replayed.event_record)
        self.assertEqual(records, (written.event_record,))

    def test_tenant_technical_human_waiver_compare_write_rejects_stale_create_replay(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)
            envelope = _tenant_technical_human_waiver_envelope()
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=(
                    "2026-08-01T12:00:00Z",
                    "2026-08-01T12:00:00Z",
                    "2026-08-01T12:00:01Z",
                    "2026-08-01T12:00:02Z",
                    "2026-08-01T12:00:02Z",
                ),
            ):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=envelope,
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-create-original",
                        request_fingerprint="tenant-waiver-create-original-fingerprint",
                        response_trace_id="trace-waiver-create-original",
                    ),
                )
                with self.assertRaises(TenantTechnicalHumanWaiverEventConflictError):
                    store.compare_and_write_tenant_technical_human_waiver_event(
                        identity=_tenant_technical_human_waiver_identity(),
                        envelope=envelope,
                        mutation=_tenant_technical_human_waiver_mutation(
                            idempotency_key="tenant-waiver-create-stale",
                            request_fingerprint="tenant-waiver-create-stale-fingerprint",
                            response_trace_id="trace-waiver-create-stale",
                        ),
                    )

            stale_reservation = store.read_idempotency_record(
                scope="github-human-id|301",
                route_path="/v1/tenant-admission/technical-human-waivers/apply",
                idempotency_key="tenant-waiver-create-stale",
            )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            store.close()

        self.assertIsNone(stale_reservation)
        self.assertEqual(len(records), 1)

    def test_tenant_technical_human_waiver_compare_write_revokes_with_expected_current(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)
            created = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=_tenant_technical_human_waiver_envelope(
                    source_event_id="comment-create-revoke"
                ),
                mutation=_tenant_technical_human_waiver_mutation(
                    idempotency_key="tenant-waiver-create-revoke",
                    request_fingerprint="tenant-waiver-create-revoke-fingerprint",
                    response_trace_id="trace-waiver-create-revoke",
                ),
            )
            assert created.event_record is not None
            revoke_envelope = _tenant_technical_human_waiver_envelope(
                action="revoked",
                source_event_id="comment-revoke",
                expected_current={
                    "waiver_id": created.event_record.waiver_id,
                    "event_digest": created.event_record.event_digest,
                },
                reason="Owner revoked exact waiver.",
            )
            revoked = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=revoke_envelope,
                mutation=_tenant_technical_human_waiver_mutation(
                    idempotency_key="tenant-waiver-revoke",
                    request_fingerprint="tenant-waiver-revoke-fingerprint",
                    response_trace_id="trace-waiver-revoke",
                ),
            )
            assert revoked.event_record is not None
            with self.assertRaises(TenantTechnicalHumanWaiverRevokeCurrentError):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=_tenant_technical_human_waiver_envelope(
                        action="revoked",
                        source_event_id="comment-revoke-stale-cas",
                        expected_current={
                            "waiver_id": created.event_record.waiver_id,
                            "event_digest": created.event_record.event_digest,
                        },
                        reason="Owner retried stale revoke.",
                    ),
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-revoke-stale-cas",
                        request_fingerprint="tenant-waiver-revoke-stale-cas-fingerprint",
                        response_trace_id="trace-waiver-revoke-stale-cas",
                    ),
                )
            stale_revoke_timestamp = (
                (
                    datetime.fromisoformat(revoked.event_record.occurred_at.replace("Z", "+00:00"))
                    + timedelta(seconds=1)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value=stale_revoke_timestamp,
            ):
                with self.assertRaises(TenantTechnicalHumanWaiverRevokeCurrentError):
                    store.compare_and_write_tenant_technical_human_waiver_event(
                        identity=_tenant_technical_human_waiver_identity(),
                        envelope=revoke_envelope,
                        mutation=_tenant_technical_human_waiver_mutation(
                            idempotency_key="tenant-waiver-revoke-stale-event",
                            request_fingerprint="tenant-waiver-revoke-stale-event-fingerprint",
                            response_trace_id="trace-waiver-revoke-stale-event",
                        ),
                    )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            stale_revoke_reservation = store.read_idempotency_record(
                scope="github-human-id|301",
                route_path="/v1/tenant-admission/technical-human-waivers/apply",
                idempotency_key="tenant-waiver-revoke-stale-event",
            )
            store.close()

        self.assertEqual(revoked.status, "written")
        self.assertIsNotNone(revoked.result)
        self.assertIsNotNone(revoked.event_record)
        assert revoked.result is not None
        assert revoked.event_record is not None
        self.assertEqual(revoked.result.path_result.state, "denied")
        self.assertEqual(tuple(record.action for record in records), ("revoked", "created"))
        self.assertIsNone(stale_revoke_reservation)

    def test_tenant_technical_human_waiver_compare_write_validates_current_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            role_policy = _repository_human_role_policy_record(repository_owner_github_ids=(302,))
            _seed_tenant_technical_human_waiver_authority(
                store,
                role_policy=role_policy,
            )

            with self.assertRaises(TenantTechnicalHumanWaiverAuthorizationError):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=_tenant_technical_human_waiver_envelope(role_policy=role_policy),
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-non-owner",
                        request_fingerprint="tenant-waiver-non-owner-fingerprint",
                    ),
                )
            with self.assertRaises(TenantTechnicalHumanWaiverStaleAuthorityError):
                stale_envelope = _tenant_technical_human_waiver_envelope(role_policy=role_policy)
                stale_envelope.expected_authority.authz_policy_digest = "f" * 64
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(github_id=302),
                    envelope=stale_envelope,
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="tenant-waiver-authz-drift",
                        request_fingerprint="tenant-waiver-authz-drift-fingerprint",
                        scope="github-human-id|302",
                    ),
                )
            failed_reservation = store.read_idempotency_record(
                scope="github-human-id|302",
                route_path="/v1/tenant-admission/technical-human-waivers/apply",
                idempotency_key="tenant-waiver-authz-drift",
            )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            store.close()

        self.assertIsNone(failed_reservation)
        self.assertEqual(records, ())

    def test_tenant_technical_human_waiver_compare_write_rolls_back_on_completion_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)
            mutation = _tenant_technical_human_waiver_mutation(
                idempotency_key="tenant-waiver-rollback",
                request_fingerprint="tenant-waiver-rollback-fingerprint",
                response_trace_id="trace-waiver-rollback",
            )

            with (
                patch.object(
                    store,
                    "_sync_idempotency_row",
                    side_effect=RuntimeError("injected completion failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected completion failure"),
            ):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=_tenant_technical_human_waiver_envelope(),
                    mutation=mutation,
                )

            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            store.close()

        self.assertIsNone(stored_idempotency)
        self.assertEqual(records, ())

    def test_tenant_technical_human_waiver_compare_write_serializes_no_row_race(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_tenant_technical_human_waiver_authority(store)

            def write_with_suffix(suffix: str) -> str:
                active_store = PostgresRecordStore(database_url=database_url)
                try:
                    result = active_store.compare_and_write_tenant_technical_human_waiver_event(
                        identity=_tenant_technical_human_waiver_identity(),
                        envelope=_tenant_technical_human_waiver_envelope(
                            source_event_id=f"comment-race-{suffix}"
                        ),
                        mutation=_tenant_technical_human_waiver_mutation(
                            idempotency_key=f"tenant-waiver-race-{suffix}",
                            request_fingerprint=f"tenant-waiver-race-{suffix}-fingerprint",
                            response_trace_id=f"trace-waiver-race-{suffix}",
                        ),
                    )
                    return result.status
                except TenantTechnicalHumanWaiverEventConflictError:
                    return "conflict"
                finally:
                    active_store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = tuple(executor.map(write_with_suffix, ("a", "b")))

            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="1001",
                product="example-product",
                context="example-product",
            )
            store.close()

        self.assertEqual(statuses.count("written"), 1)
        self.assertEqual(statuses.count("conflict"), 1)
        self.assertEqual(len(records), 1)

    def test_write_promotion_evidence_records_writes_promotion_and_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            promotion_record = _promotion_record(
                record_id="promotion-20260420T160500Z-opw-testing-to-prod"
            )
            inventory = _inventory_record().model_copy(
                update={
                    "instance": "prod",
                    "deploy": promotion_record.deploy,
                    "promotion_record_id": promotion_record.record_id,
                    "promoted_from_instance": "testing",
                    "updated_at": "2026-04-20T16:08:00Z",
                }
            )

            store.write_promotion_evidence_records(
                promotion_record=promotion_record,
                inventory=inventory,
            )
            loaded_promotion = store.read_promotion_record(promotion_record.record_id)
            loaded_inventory = store.read_environment_inventory(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(loaded_promotion.record_id, promotion_record.record_id)
        self.assertEqual(loaded_inventory.promotion_record_id, promotion_record.record_id)
        self.assertEqual(loaded_inventory.promoted_from_instance, "testing")

    def test_write_preview_generation_evidence_records_writes_generation_and_preview(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            generation_id = "preview-verireel-testing-verireel-pr-123-generation-0001"
            preview = _preview_record(
                preview_id="preview-verireel-testing-verireel-pr-123",
                updated_at="2026-04-20T10:05:00Z",
                pr_number=123,
            ).model_copy(
                update={
                    "active_generation_id": generation_id,
                    "serving_generation_id": generation_id,
                    "latest_generation_id": generation_id,
                    "latest_manifest_fingerprint": "preview-manifest-123",
                }
            )
            generation = _preview_generation_record(
                generation_id=generation_id,
                preview_id=preview.preview_id,
            )

            store.write_preview_generation_evidence_records(
                preview_record=preview,
                generation_record=generation,
            )
            loaded_generation = store.read_preview_generation_record(generation_id)
            loaded_preview = store.read_preview_record(preview.preview_id)
            store.close()

        self.assertEqual(loaded_generation.generation_id, generation_id)
        self.assertEqual(loaded_generation.preview_id, preview.preview_id)
        self.assertEqual(loaded_preview.preview_id, preview.preview_id)
        self.assertEqual(loaded_preview.serving_generation_id, generation_id)

    def test_alembic_baseline_creates_schema_used_by_record_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            alembic_command.upgrade(_alembic_config(database_url), "head")

            store = PostgresRecordStore(database_url=database_url)
            manifest = _artifact_manifest()
            ingress_route_audit = IngressRouteAuditRecord(
                record_id="ingress-route-audit-alembic",
                product="launchplane",
                context="reon-prod",
                mode="apply",
                status="unchanged",
                dry_run=False,
                requested_domains=("ingress-canary.example.test",),
                expected_host_id=78,
                provider_host_id=78,
                operations=(
                    IngressRouteAuditOperation(
                        action="no-op",
                        host_id=78,
                        domain_names=("ingress-canary.example.test",),
                        requires_apply=False,
                    ),
                ),
                trace_id="launchplane_req_alembic",
                idempotency_key="ingress-canary-apply",
                reason="Apply unchanged canary route.",
                recorded_at="2026-05-31T12:05:00Z",
            )
            store.write_artifact_manifest(manifest)
            store.write_ingress_route_audit_record(ingress_route_audit)
            provider_target = _provider_target_record(context="reon", instance="prod")
            store.write_provider_target_record(provider_target)
            edge_endpoint = _edge_endpoint_record()
            store.write_edge_endpoint_record(edge_endpoint)
            private_health_endpoint = _private_health_endpoint_record()
            store.write_private_health_endpoint_record(private_health_endpoint)
            ingress_canary_route = _ingress_canary_route_record()
            store.write_ingress_canary_route_record(ingress_canary_route)
            route_binding = _route_binding_record()
            store.write_route_binding_record(route_binding)
            inspect_engine = create_engine(database_url)
            inspector = inspect(inspect_engine)
            table_names = set(inspector.get_table_names())
            edge_endpoint_columns = {
                column["name"] for column in inspector.get_columns("launchplane_edge_endpoints")
            }
            ingress_canary_route_columns = {
                column["name"]
                for column in inspector.get_columns("launchplane_ingress_canary_routes")
            }
            private_health_endpoint_columns = {
                column["name"]
                for column in inspector.get_columns("launchplane_private_health_endpoints")
            }
            provider_target_columns = {
                column["name"] for column in inspector.get_columns("launchplane_provider_targets")
            }
            provider_target_indexes = {
                index["name"] for index in inspector.get_indexes("launchplane_provider_targets")
            }
            route_binding_columns = {
                column["name"] for column in inspector.get_columns("launchplane_route_bindings")
            }
            route_binding_indexes = {
                index["name"] for index in inspector.get_indexes("launchplane_route_bindings")
            }
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            loaded_provider_target = store.read_provider_target_record(
                context_name="reon",
                instance_name="prod",
            )
            loaded_edge_endpoint = store.read_edge_endpoint_record(edge_endpoint.endpoint_key)
            loaded_private_health_endpoint = store.read_private_health_endpoint_record(
                private_health_endpoint.endpoint_key
            )
            loaded_ingress_canary_route = store.read_ingress_canary_route_record(
                ingress_canary_route.canary_key
            )
            loaded_route_binding = store.read_route_binding_record(
                product=route_binding.product,
                context_name=route_binding.context,
                instance_name=route_binding.instance,
            )
            audit_records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )
            store.close()
            inspect_engine.dispose()

        self.assertEqual(loaded.artifact_id, manifest.artifact_id)
        self.assertEqual(loaded.image.digest, "sha256:image123")
        self.assertEqual(loaded_provider_target.target_id, provider_target.target_id)
        self.assertEqual(loaded_edge_endpoint.upstream_host, edge_endpoint.upstream_host)
        self.assertEqual(
            loaded_private_health_endpoint.url,
            "http://10.0.0.5:8000/health",
        )
        self.assertEqual(loaded_ingress_canary_route.domain_name, "ingress-canary.example.test")
        self.assertEqual(loaded_route_binding.binding_key, route_binding.binding_key)
        self.assertIn("launchplane_edge_endpoints", table_names)
        self.assertIn("launchplane_private_health_endpoints", table_names)
        self.assertIn("launchplane_ingress_canary_routes", table_names)
        self.assertIn("launchplane_route_bindings", table_names)
        self.assertGreaterEqual(
            edge_endpoint_columns,
            {
                "endpoint_key",
                "provider",
                "server_name",
                "upstream_host",
                "upstream_scheme",
                "upstream_port",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertGreaterEqual(
            private_health_endpoint_columns,
            {
                "endpoint_key",
                "product",
                "context",
                "instance",
                "url",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertGreaterEqual(
            ingress_canary_route_columns,
            {
                "canary_key",
                "product",
                "context",
                "domain_name",
                "expected_host_id",
                "edge_endpoint_key",
                "certificate_id",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertIn("launchplane_provider_targets", table_names)
        self.assertGreaterEqual(
            provider_target_columns,
            {
                "context",
                "instance",
                "provider_id",
                "target_category",
                "target_id",
                "display_name",
                "provider_target_type",
                "updated_at",
                "payload",
            },
        )
        self.assertIn("launchplane_provider_targets_provider_idx", provider_target_indexes)
        self.assertIn("launchplane_provider_targets_updated_idx", provider_target_indexes)
        self.assertGreaterEqual(
            route_binding_columns,
            {
                "product",
                "context",
                "instance",
                "provider_id",
                "target_category",
                "ingress_provider",
                "ingress_endpoint_key",
                "termination_kind",
                "tls_owner",
                "primary_domain",
                "status",
                "freshness_status",
                "updated_at",
                "payload",
            },
        )
        self.assertIn("launchplane_route_bindings_lookup_idx", route_binding_indexes)
        self.assertIn("launchplane_route_bindings_updated_idx", route_binding_indexes)
        self.assertEqual(
            [record.record_id for record in audit_records], [ingress_route_audit.record_id]
        )

    def test_mutation_reservation_migration_backfills_completed_idempotency_rows(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "c9d1e3f5a7b9")
            legacy_payload = {
                "schema_version": 1,
                "record_id": "idempotency-legacy-trace",
                "scope": "github-actions:legacy",
                "route_path": "/v1/evidence/previews/generations",
                "idempotency_key": "legacy-idempotency-key",
                "request_fingerprint": "legacy-fingerprint",
                "response_status_code": 202,
                "response_trace_id": "legacy-trace",
                "recorded_at": "2026-07-12T00:00:00Z",
                "response_payload": {"status": "accepted"},
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    insert(LaunchplaneIdempotencyRow).values(
                        record_id="idempotency-legacy-trace",
                        scope="github-actions:legacy",
                        route_path="/v1/evidence/previews/generations",
                        idempotency_key="legacy-idempotency-key",
                        request_fingerprint="legacy-fingerprint",
                        response_status_code=202,
                        response_trace_id="legacy-trace",
                        recorded_at="2026-07-12T00:00:00Z",
                        payload=legacy_payload,
                    )
                )
            engine.dispose()

            alembic_command.upgrade(config, "head")
            store = PostgresRecordStore(database_url=database_url)
            loaded = store.read_idempotency_record(
                scope="github-actions:legacy",
                route_path="/v1/evidence/previews/generations",
                idempotency_key="legacy-idempotency-key",
            )
            with store._engine.connect() as connection:
                promoted = (
                    connection.execute(
                        text(
                            "SELECT state, attempt, created_at, updated_at "
                            "FROM launchplane_idempotency_records "
                            "WHERE record_id = 'idempotency-legacy-trace'"
                        )
                    )
                    .mappings()
                    .one()
                )
            store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.state, "completed")
        self.assertEqual(loaded.created_at, "2026-07-12T00:00:00Z")
        self.assertEqual(promoted["state"], "completed")
        self.assertEqual(promoted["attempt"], 1)
        self.assertEqual(promoted["created_at"], "2026-07-12T00:00:00Z")
        self.assertEqual(promoted["updated_at"], "2026-07-12T00:00:00Z")

    def test_every_code_lease_migration_blocks_legacy_active_requests_for_review(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "e1f3a5c7b9d1")
            legacy_record = _every_code_work_request(state="running")
            legacy_payload = legacy_record.model_dump(mode="json")
            for field_name in ("lease_expires_at", "fencing_token", "attempt"):
                legacy_payload.pop(field_name, None)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_every_code_work_requests "
                        "(request_id, source, state, repository, issue_number, trigger_label, "
                        "updated_at, claimed_by_host, payload) "
                        "VALUES (:request_id, :source, :state, :repository, :issue_number, "
                        ":trigger_label, :updated_at, :claimed_by_host, :payload)"
                    ),
                    {
                        "request_id": legacy_record.request_id,
                        "source": legacy_record.source,
                        "state": legacy_record.state,
                        "repository": legacy_record.repository,
                        "issue_number": legacy_record.issue_number,
                        "trigger_label": legacy_record.trigger_label,
                        "updated_at": legacy_record.updated_at,
                        "claimed_by_host": legacy_record.claimed_by_host,
                        "payload": json.dumps(legacy_payload),
                    },
                )
            engine.dispose()

            alembic_command.upgrade(config, "head")
            store = PostgresRecordStore(database_url=database_url)
            migrated = store.read_every_code_work_request_record(legacy_record.request_id)
            recovered = store.recover_stale_every_code_work_request_record(
                expected_record=migrated,
                recovered_at="2026-07-13T00:00:00Z",
            )
            store.close()

        self.assertEqual(migrated.fencing_token, 4)
        self.assertEqual(migrated.attempt, 4)
        self.assertEqual(migrated.lease_expires_at, "1970-01-01T00:00:00Z")
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, "blocked")
        self.assertIn("manual review required", recovered.error_message)

    def test_verify_schema_rejects_empty_database_without_creating_tables(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)

            with self.assertRaisesRegex(RuntimeError, "missing required table"):
                store.verify_schema()

            self.assertEqual(inspect(store._engine).get_table_names(), [])
            store.close()

    def test_verify_schema_rejects_missing_columns_without_creating_them(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            alembic_command.upgrade(_alembic_config(database_url), "head")
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE launchplane_artifact_manifests DROP COLUMN payload")
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)

            with self.assertRaisesRegex(
                RuntimeError,
                "missing required column.*launchplane_artifact_manifests.payload",
            ):
                store.verify_schema()

            artifact_columns = {
                column["name"]
                for column in inspect(store._engine).get_columns("launchplane_artifact_manifests")
            }
            self.assertNotIn("payload", artifact_columns)
            store.close()

    def test_ensure_schema_verifies_non_sqlite_schema_without_create_all(self) -> None:
        store = object.__new__(PostgresRecordStore)
        store._engine = Mock()
        store._engine.url.get_backend_name.return_value = "postgresql"

        with (
            patch.object(store, "verify_schema") as verify_schema,
            patch("control_plane.storage.postgres.Base.metadata.create_all") as create_all,
        ):
            store.ensure_schema()

        verify_schema.assert_called_once_with()
        create_all.assert_not_called()

    def test_shared_record_store_verifies_existing_schema_without_creating_it(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            with self.assertRaisesRegex(RuntimeError, "Run Alembic migrations"):
                build_shared_record_store(database_url=database_url)

            alembic_command.upgrade(_alembic_config(database_url), "head")
            store = build_shared_record_store(database_url=database_url)
            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            store.close()

        self.assertEqual(loaded.artifact_id, manifest.artifact_id)

    def test_privileged_operation_worker_store_uses_bounded_runtime_check(self) -> None:
        runtime_store = Mock()
        schema_probe_succeeded = Mock()
        database_url = "postgresql+psycopg://launchplane.invalid/launchplane"

        with patch(
            "control_plane.storage.factory.PostgresRecordStore",
            return_value=runtime_store,
        ) as store_type:
            result = build_privileged_operation_worker_store(
                database_url=database_url,
                schema_probe_completed=True,
                on_schema_probe_succeeded=schema_probe_succeeded,
            )

        self.assertIs(result, runtime_store)
        store_type.assert_called_once_with(
            database_url=database_url,
            postgres_connect_timeout_seconds=(PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS),
            postgres_statement_timeout_milliseconds=(
                PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS
            ),
        )
        schema_probe_succeeded.assert_called_once_with()

    def test_privileged_operation_worker_store_runs_direct_probe_without_marker(self) -> None:
        startup_probe = Mock()
        runtime_store = Mock()
        schema_probe_succeeded = Mock()
        database_url = "postgresql+psycopg://launchplane.invalid/launchplane"

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "control_plane.storage.factory.PostgresRecordStore",
                side_effect=[startup_probe, runtime_store],
            ) as store_type,
        ):
            result = build_privileged_operation_worker_store(
                database_url=database_url,
                on_schema_probe_succeeded=schema_probe_succeeded,
            )

        self.assertIs(result, runtime_store)
        self.assertEqual(store_type.call_count, 2)
        startup_probe.verify_runtime_schema_compatibility.assert_called_once_with(
            required_relations=PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS
        )
        startup_probe.close.assert_called_once_with()
        schema_probe_succeeded.assert_called_once_with()

    def test_privileged_operation_worker_store_closes_incompatible_schema(self) -> None:
        startup_probe = Mock()
        startup_probe.verify_runtime_schema_compatibility.side_effect = RuntimeError(
            "schema detail must remain internal"
        )
        database_url = "postgresql+psycopg://launchplane.invalid/launchplane"

        with (
            patch(
                "control_plane.storage.privileged_operation_worker_probe.PostgresRecordStore",
                return_value=startup_probe,
            ) as store_type,
            patch.dict(
                "os.environ",
                {"LAUNCHPLANE_DATABASE_URL": database_url},
                clear=True,
            ),
        ):
            result = run_privileged_operation_worker_schema_probe()

        self.assertEqual(result, PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE)
        store_type.assert_called_once_with(
            database_url=database_url,
            postgres_connect_timeout_seconds=(PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS),
            postgres_statement_timeout_milliseconds=(
                PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS
            ),
        )
        startup_probe.verify_runtime_schema_compatibility.assert_called_once_with(
            required_relations=PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS
        )
        startup_probe.close.assert_called_once_with()

    def test_privileged_operation_worker_store_rejects_failed_schema_probe(self) -> None:
        startup_probe = Mock()
        startup_probe.verify_runtime_schema_compatibility.side_effect = RuntimeError(
            "schema detail must remain internal"
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "control_plane.storage.factory.PostgresRecordStore",
                return_value=startup_probe,
            ),
            self.assertRaisesRegex(PrivilegedOperationWorkerSchemaError, "not runtime-compatible"),
        ):
            build_privileged_operation_worker_store(
                database_url="postgresql+psycopg://launchplane.invalid/launchplane"
            )

        startup_probe.close.assert_called_once_with()

    def test_privileged_operation_worker_probe_module_is_silent_without_database_url(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "control_plane.storage.privileged_operation_worker_probe",
            ],
            capture_output=True,
            check=False,
            env={},
            text=True,
        )

        self.assertEqual(completed.returncode, PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_postgres_engine_applies_bounded_connection_options(self) -> None:
        database_url = (
            "postgresql+psycopg://launchplane.invalid/launchplane"
            "?options=-c%20search_path%3Dlaunchplane"
        )

        with patch("control_plane.storage.postgres.create_engine") as create_engine_mock:
            _build_engine(
                database_url,
                postgres_connect_timeout_seconds=10,
                postgres_statement_timeout_milliseconds=30_000,
            )

        create_engine_mock.assert_called_once_with(
            database_url,
            connect_args={
                "connect_timeout": 10,
                "options": "-c search_path=launchplane -c statement_timeout=30000",
            },
        )

    def test_postgres_store_applies_bounded_options_to_lock_engine(self) -> None:
        database_url = "postgresql+psycopg://launchplane.invalid/launchplane"
        primary_engine = Mock()
        primary_engine.url.get_backend_name.return_value = "postgresql"

        with (
            patch("control_plane.storage.postgres._build_engine", return_value=primary_engine),
            patch("control_plane.storage.postgres.create_engine") as create_engine_mock,
        ):
            store = PostgresRecordStore(
                database_url=database_url,
                postgres_connect_timeout_seconds=10,
                postgres_statement_timeout_milliseconds=30_000,
            )

        create_engine_mock.assert_called_once_with(
            database_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 10,
                "options": "-c statement_timeout=30000",
            },
        )
        store.close()

    def test_runtime_schema_compatibility_requires_postgres(self) -> None:
        store = object.__new__(PostgresRecordStore)
        store._engine = Mock()
        store._engine.url.get_backend_name.return_value = "sqlite"

        with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
            store.verify_runtime_schema_compatibility(required_relations=("required_table",))

    def test_runtime_schema_compatibility_checks_revision_and_relations(self) -> None:
        store = object.__new__(PostgresRecordStore)
        store._engine = MagicMock()
        store._engine.url.get_backend_name.return_value = "postgresql"
        connection = store._engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalars.return_value.all.return_value = []

        with patch.object(store, "schema_revision", return_value="e2221b0c2d3e"):
            store.verify_runtime_schema_compatibility(
                required_relations=("required_table", "required_index")
            )

        connection.execute.assert_called_once()
        self.assertEqual(
            connection.execute.call_args.args[1],
            {"relation_0": "required_table", "relation_1": "required_index"},
        )

    def test_runtime_schema_compatibility_rejects_missing_relations(self) -> None:
        store = object.__new__(PostgresRecordStore)
        store._engine = MagicMock()
        store._engine.url.get_backend_name.return_value = "postgresql"
        connection = store._engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalars.return_value.all.return_value = ["missing_index"]

        with (
            patch.object(store, "schema_revision", return_value="e2221b0c2d3e"),
            self.assertRaisesRegex(RuntimeError, "missing_index"),
        ):
            store.verify_runtime_schema_compatibility(
                required_relations=("required_table", "missing_index")
            )

    def test_postgres_engine_rejects_nonpositive_timeouts(self) -> None:
        database_url = "postgresql+psycopg://launchplane.invalid/launchplane"

        with self.assertRaisesRegex(ValueError, "connect timeout must be positive"):
            _build_engine(database_url, postgres_connect_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "statement timeout must be positive"):
            _build_engine(database_url, postgres_statement_timeout_milliseconds=0)

    def test_alembic_head_repairs_stamped_schema_missing_odoo_replacement_scope(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "head")
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("DROP INDEX launchplane_odoo_replacement_operation_idempotency_idx")
                )
                connection.execute(
                    text(
                        "ALTER TABLE "
                        "launchplane_odoo_stable_target_replacement_operations "
                        "DROP COLUMN idempotency_scope"
                    )
                )
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
                    {"version_num": "b1c3d5e7f9a1"},
                )
            engine.dispose()

            alembic_command.upgrade(config, "head")
            store = PostgresRecordStore(database_url=database_url)
            store.verify_schema()

            repaired_columns = {
                column["name"]
                for column in inspect(store._engine).get_columns(
                    "launchplane_odoo_stable_target_replacement_operations"
                )
            }
            store.close()

        self.assertIn("idempotency_scope", repaired_columns)

    def test_runner_host_hygiene_audit_downgrade_tolerates_missing_table(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "c2d4e6f8a0b2")
            alembic_command.stamp(config, "c3e5f7a9b1d2")

            alembic_command.downgrade(config, "c2d4e6f8a0b2")

            engine = create_engine(database_url)
            table_names = set(inspect(engine).get_table_names())
            engine.dispose()

        self.assertNotIn("launchplane_runner_host_hygiene_audits", table_names)

    def test_alembic_baseline_downgrades_to_empty_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            config = _alembic_config(database_url)

            alembic_command.upgrade(config, "head")
            alembic_command.downgrade(config, "base")

            store = PostgresRecordStore(database_url=database_url)
            with self.assertRaises(SQLAlchemyError):
                store.list_artifact_manifests()
            store.close()

    def test_artifact_manifests_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            listed = store.list_artifact_manifests()

            self.assertEqual(loaded.artifact_id, manifest.artifact_id)
            self.assertEqual(loaded.image.repository, "ghcr.io/cbusillo/odoo-tenant-opw")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].image.digest, "sha256:image123")
            self.assertIsNone(loaded.dependency_provenance)

    def test_v2_artifact_dependency_provenance_round_trips(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            manifest = artifact_manifest_v2()

            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)

            provenance = loaded.dependency_provenance
            assert provenance is not None
            self.assertEqual(loaded.schema_version, 2)
            self.assertEqual(provenance.uv_locks[0].scope, "support_runtime")
            self.assertEqual(
                provenance.external_compatibility_inputs[0].resolution_posture,
                "exact_source_unlocked",
            )

    def test_artifacts_show_uses_database_store_when_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_artifact_manifest(_artifact_manifest())
            runner = CliRunner()

            show_result = runner.invoke(
                main,
                [
                    "artifacts",
                    "show",
                    "--database-url",
                    database_url,
                    "--artifact-id",
                    "artifact-20260420-a1b2c3d4",
                ],
            )
            self.assertEqual(show_result.exit_code, 0, show_result.output)
            self.assertIn("ghcr.io/cbusillo/odoo-tenant-opw", show_result.output)

    def test_dokploy_target_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _dokploy_target_record()
            store.write_dokploy_target_record(record)
            loaded = store.read_dokploy_target_record(context_name="opw", instance_name="prod")
            listed = store.list_dokploy_target_records()

            self.assertEqual(loaded.target_name, "opw-prod")
            self.assertEqual(loaded.compose_path, "./docker-compose.yml")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].context, "opw")

    def test_provider_target_records_require_physical_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(
                    context="syo",
                    instance="prod",
                    project_name="syo-prod-project",
                )
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="app-syo-prod",
                )
            )

            with self.assertRaises(FileNotFoundError):
                store.read_provider_target_record(context_name="syo", instance_name="prod")
            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id=" DOKPLOY ")
            store.close()

        self.assertEqual(listed, ())
        self.assertEqual(filtered, ())

    def test_provider_target_records_round_trip_from_physical_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(
                context="verireel",
                instance="prod",
                provider_id=" Dokploy ",
                target_id="app-verireel-prod",
            )
            store.write_provider_target_record(record)

            loaded = store.read_provider_target_record(
                context_name="verireel",
                instance_name="prod",
            )
            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id=" DOKPLOY ")
            store.close()

        self.assertEqual(loaded, record)
        self.assertEqual(listed, (record,))
        self.assertEqual(filtered, (record,))

    def test_create_provider_target_record_if_absent_refuses_existing_route(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(context="verireel", instance="prod")
            changed_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod-new",
            )
            first_status = store.create_provider_target_record_if_absent(record)
            second_status = store.create_provider_target_record_if_absent(changed_record)
            loaded = store.read_provider_target_record(
                context_name="verireel", instance_name="prod"
            )
            store.close()

        self.assertEqual(first_status, "created")
        self.assertEqual(second_status, "exists")
        self.assertEqual(loaded, record)

    def test_delete_provider_target_record_uses_current_authority_match(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(context="verireel", instance="prod")
            changed_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod-new",
            )
            store.write_provider_target_record(record)

            changed_status = store.delete_provider_target_record(expected_record=changed_record)
            loaded_after_changed = store.read_provider_target_record(
                context_name="verireel",
                instance_name="prod",
            )
            deleted_status = store.delete_provider_target_record(expected_record=record)
            missing_status = store.delete_provider_target_record(expected_record=record)
            store.close()

        self.assertEqual(changed_status, "changed")
        self.assertEqual(loaded_after_changed, record)
        self.assertEqual(deleted_status, "deleted")
        self.assertEqual(missing_status, "missing")

    def test_provider_target_physical_storage_precedes_dokploy_projection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="syo",
                instance="prod",
                target_id="physical-app-syo-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="projected-app-syo-prod",
                )
            )

            loaded = store.read_provider_target_record(context_name="syo", instance_name="prod")
            listed = store.list_provider_target_records()
            summary = store.read_lane_summary(context_name="syo", instance_name="prod")
            store.close()

        self.assertEqual(loaded, physical_record)
        self.assertEqual(listed, (physical_record,))
        self.assertEqual(summary.provider_target, physical_record)

    def test_provider_target_filter_suppresses_shadowed_dokploy_projection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="syo",
                instance="prod",
                provider_id="future-provider",
                target_id="physical-syo-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="legacy-dokploy-syo-prod",
                )
            )

            dokploy_records = store.list_provider_target_records(provider_id="dokploy")
            future_provider_records = store.list_provider_target_records(
                provider_id="future-provider"
            )
            store.close()

        self.assertEqual(dokploy_records, ())
        self.assertEqual(future_provider_records, (physical_record,))

    def test_read_lane_summary_does_not_project_provider_target_from_dokploy_pair(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="legacy-dokploy-syo-prod",
                )
            )

            summary = store.read_lane_summary(context_name="syo", instance_name="prod")
            store.close()

        self.assertIsNone(summary.provider_target)
        self.assertIsNotNone(summary.dokploy_target)
        self.assertIsNotNone(summary.dokploy_target_id)

    def test_provider_target_list_returns_only_physical_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="app-syo-prod",
                )
            )
            store.write_dokploy_target_record(
                _dokploy_target_record(context="incomplete", instance="prod")
            )

            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id="dokploy")
            store.close()

        self.assertEqual(
            [(record.context, record.instance, record.target_id) for record in listed],
            [
                ("verireel", "prod", "app-verireel-prod"),
            ],
        )
        self.assertEqual(filtered, listed)

    def test_provider_target_list_skips_incomplete_dokploy_pairs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )

            self.assertEqual(store.list_provider_target_records(), ())
            with self.assertRaises(FileNotFoundError):
                store.read_provider_target_record(context_name="syo", instance_name="prod")
            store.close()

    def test_read_lane_summary_keeps_deployment_target_evidence_separate(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            store.write_deployment_record(
                _deployment_record_with_target_id(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    target_id="old-compose-id",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            store.write_provider_target_record(
                _provider_target_record(
                    context="opw",
                    instance="testing",
                    target_id="current-compose-id",
                )
            )

            summary = store.read_lane_summary(context_name="opw", instance_name="testing")
            store.close()

        provider_target = summary.provider_target
        assert provider_target is not None
        self.assertEqual(provider_target.target_id, "current-compose-id")
        deployed_target = summary.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.target_id, "old-compose-id")

    def test_idempotency_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id="launchplane_req_123",
                ),
                scope="every/verireel|workflow|repo:every/verireel:pull_request",
                route_path="/v1/evidence/previews/generations",
                idempotency_key="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                request_fingerprint="fingerprint-123",
                response_status_code=202,
                response_trace_id="launchplane_req_123",
                recorded_at="2026-04-21T01:00:00Z",
                response_payload={"status": "accepted", "records": {"preview_id": "preview-35"}},
            )

            store.write_idempotency_record(record)
            loaded = store.read_idempotency_record(
                scope=record.scope,
                route_path=record.route_path,
                idempotency_key=record.idempotency_key,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.request_fingerprint, "fingerprint-123")
            self.assertEqual(loaded.response_payload["records"]["preview_id"], "preview-35")

    def test_outbox_delivery_round_trip_claim_and_complete(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            delivery = _outbox_delivery()

            store.write_outbox_delivery_record(delivery)
            claimed = store.claim_next_outbox_delivery_record(
                lease_owner="worker-a",
                lease_seconds=300,
                now="2026-07-13T00:00:10Z",
            )
            assert claimed.record is not None
            delivered_record = claimed.record.model_copy(
                update={
                    "state": "delivered",
                    "action": "dispatched_workflow",
                    "external_id": "12345",
                    "external_url": "https://github.example/actions/runs/12345",
                }
            )
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value="2026-07-13T00:00:20Z",
            ):
                stale_completion = store.complete_outbox_delivery_record(
                    record=delivered_record,
                    lease_owner="worker-b",
                )
                completion = store.complete_outbox_delivery_record(
                    record=delivered_record,
                    lease_owner="worker-a",
                )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            store.close()

        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(claimed.record.state, "running")
        self.assertEqual(claimed.record.lease_owner, "worker-a")
        self.assertEqual(claimed.record.attempt, 1)
        self.assertEqual(stale_completion.status, "owner_mismatch")
        self.assertEqual(completion.status, "updated")
        self.assertEqual(loaded.state, "delivered")
        self.assertEqual(loaded.lease_owner, "")
        self.assertEqual(loaded.external_id, "12345")

    def test_outbox_enqueue_with_idempotency_commits_both_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            delivery = _outbox_delivery()
            idempotency = LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id="launchplane_req_outbox",
                ),
                scope="github-actions:outbox-test",
                route_path="/v1/drivers/generic-web/prod-promotion-workflow",
                idempotency_key="outbox-idempotency-key",
                request_fingerprint="fingerprint-outbox",
                response_status_code=202,
                response_trace_id="launchplane_req_outbox",
                recorded_at="2026-07-13T00:00:00Z",
                response_payload={
                    "status": "accepted",
                    "records": {"outbox_delivery_id": delivery.delivery_id},
                },
            )

            first = store.enqueue_outbox_delivery_with_idempotency(
                OutboxWithIdempotencyRequest(
                    delivery=delivery,
                    idempotency_record=idempotency,
                )
            )
            duplicate = store.enqueue_outbox_delivery_with_idempotency(
                OutboxWithIdempotencyRequest(
                    delivery=delivery.model_copy(update={"delivery_id": "ignored-duplicate"}),
                    idempotency_record=None,
                )
            )
            loaded_idempotency = store.read_idempotency_record(
                scope=idempotency.scope,
                route_path=idempotency.route_path,
                idempotency_key=idempotency.idempotency_key,
            )
            outbox_rows = store.list_outbox_delivery_records(states=("pending",))
            store.close()

        self.assertEqual(first.delivery_id, delivery.delivery_id)
        self.assertEqual(duplicate.delivery_id, delivery.delivery_id)
        self.assertIsNotNone(loaded_idempotency)
        assert loaded_idempotency is not None
        self.assertEqual(
            loaded_idempotency.response_payload["records"]["outbox_delivery_id"],
            delivery.delivery_id,
        )
        self.assertEqual([row.delivery_id for row in outbox_rows], [delivery.delivery_id])

    def test_outbox_claim_marks_expired_provider_marker_for_reconciliation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            delivery = _outbox_delivery(
                state="running",
                provider_operation_key="github-workflow-dispatch:example/repo:deploy.yml:main:abc123",
                attempt=1,
                lease_owner="worker-a",
                lease_expires_at="2026-07-13T00:01:00Z",
            )
            store.write_outbox_delivery_record(delivery)

            claimed = store.claim_next_outbox_delivery_record(
                lease_owner="worker-b",
                now="2026-07-13T00:02:00Z",
            )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            store.close()

        self.assertEqual(claimed.status, "claimed")
        assert claimed.record is not None
        self.assertEqual(claimed.record.state, "running")
        self.assertEqual(claimed.record.lease_owner, "worker-b")
        self.assertEqual(claimed.record.attempt, 2)
        self.assertEqual(
            claimed.record.provider_operation_key,
            "github-workflow-dispatch:example/repo:deploy.yml:main:abc123",
        )
        self.assertEqual(loaded.state, "running")
        self.assertEqual(loaded.lease_owner, "worker-b")

    def test_mutation_reservation_replays_conflicts_and_reclaims_expired_lease(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            first_reservation = _mutation_reservation()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(store, first_reservation)
                stale_completion = complete_launchplane_mutation_reservation(
                    acquired.record,
                    response_status_code=202,
                    response_trace_id="trace-stale-owner",
                    completed_at="2026-07-12T01:01:00Z",
                    response_payload={"status": "accepted"},
                ).model_copy(update={"lease_owner": "worker-b"})
                stale_owner_completion = store.complete_mutation_reservation(
                    completion=stale_completion,
                )
                clock["now"] = "2026-07-12T01:01:00Z"
                in_progress = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-b"),
                )
                conflict = _reserve_mutation(
                    store,
                    _mutation_reservation(
                        request_fingerprint="mutation-fingerprint-b",
                        lease_owner="worker-b",
                    ),
                )
                clock["now"] = "2026-07-12T01:06:00Z"
                reclaimed = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-b"),
                )
                completion = complete_launchplane_mutation_reservation(
                    reclaimed.record,
                    response_status_code=202,
                    response_trace_id="trace-mutation-completed",
                    completed_at="2026-07-12T01:07:00Z",
                    response_payload={"status": "accepted"},
                )
                clock["now"] = "2026-07-12T01:07:00Z"
                completed = store.complete_mutation_reservation(completion=completion)
                clock["now"] = "2026-07-12T01:08:00Z"
                replayed = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-c"),
                )
            store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(stale_owner_completion.status, "owner_mismatch")
        self.assertEqual(in_progress.status, "in_progress")
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(reclaimed.status, "acquired")
        self.assertEqual(reclaimed.record.attempt, 2)
        self.assertEqual(reclaimed.record.lease_owner, "worker-b")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(replayed.record.state, "completed")
        self.assertEqual(replayed.record.response_trace_id, "trace-mutation-completed")

    def test_existing_mutation_lookup_crosses_scope_and_rejects_ambiguity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value="2026-08-16T16:00:00Z",
            ):
                first = store.reserve_mutation(
                    scope="github-actions:attempt-1",
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                    lease_owner="worker-a",
                    lease_seconds=300,
                )
                found = store.lookup_existing_mutation_reservation(
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                )
                conflict = store.lookup_existing_mutation_reservation(
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="different-fingerprint",
                )
                store.reserve_mutation(
                    scope="github-actions:attempt-2",
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                    lease_owner="worker-b",
                    lease_seconds=300,
                )
                ambiguous = store.lookup_existing_mutation_reservation(
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                )
            store.close()

        self.assertEqual(first.status, "acquired")
        self.assertEqual(found.status, "found")
        self.assertIsNotNone(found.record)
        assert found.record is not None
        self.assertEqual(found.record.scope, "github-actions:attempt-1")
        self.assertEqual(found.observed_at, "2026-08-16T16:00:00Z")
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNone(conflict.record)
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertIsNone(ambiguous.record)

    def test_existing_mutation_lookup_rejects_payload_projection_drift(self) -> None:
        for field_name, corrupt_value, corrupt_row in (
            ("record_id", "corrupt-record", False),
            ("scope", "github-actions:corrupt", False),
            ("route_path", "/v1/drivers/generic-web/other", False),
            ("idempotency_key", "corrupt-key", False),
            ("request_fingerprint", "corrupt-fingerprint", False),
            ("state", "reconcile_required", True),
            ("lease_owner", "worker-corrupt", False),
            ("lease_expires_at", "2099-08-16T18:00:00Z", False),
            ("attempt", 2, False),
            ("reconciliation_key", "reconciliation-corrupt", False),
            ("provider_target_key", "provider-target-corrupt", False),
            ("provider_effect_phase", " target_update ", False),
            ("provider_effect_started_at", "2026-08-16T16:00:00+00:00", False),
            ("created_at", "2026-08-16T15:59:59Z", False),
            ("updated_at", "2026-08-16T16:00:01Z", False),
            ("response_status_code", 201, False),
            ("response_trace_id", "trace-corrupt", False),
            ("recorded_at", "2026-08-16T16:00:02Z", False),
            ("response_payload", "not-a-payload", False),
        ):
            with (
                self.subTest(field_name=field_name),
                TemporaryDirectory() as temporary_directory_name,
            ):
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(
                        Path(temporary_directory_name) / "launchplane.sqlite3"
                    )
                )
                store.ensure_schema()
                with patch.object(
                    store,
                    "_database_mutation_timestamp",
                    side_effect=(
                        "2026-08-16T16:00:00Z",
                        "2026-08-16T16:00:10Z",
                        "2026-08-16T16:01:00Z",
                        "2026-08-16T16:02:00Z",
                    ),
                ):
                    running = store.reserve_mutation(
                        scope="github-actions:attempt-1",
                        route_path="/v1/drivers/generic-web/deploy",
                        idempotency_key="legacy-deploy-key",
                        request_fingerprint="exact-original-fingerprint",
                        lease_owner="worker-a",
                        lease_seconds=300,
                        reconciliation_key="reconciliation-key",
                        provider_target_key="provider-target-key",
                    ).record
                    checkpointed = store.checkpoint_mutation_provider_effect(
                        reservation=running,
                        effect_phase="target_update",
                        lease_seconds=300,
                    )
                    assert checkpointed.record is not None
                    completion = complete_launchplane_mutation_reservation(
                        checkpointed.record,
                        response_status_code=202,
                        response_trace_id="trace-completed",
                        completed_at="2026-08-16T16:01:00Z",
                        response_payload={"status": "accepted"},
                    )
                    completed = store.complete_mutation_reservation(completion=completion)
                    assert completed.record is not None
                    reservation = completed.record
                corrupt_payload = reservation.model_dump(mode="json")
                with store._session_factory() as session:
                    values: dict[str, object]
                    if corrupt_row:
                        values = {field_name: corrupt_value}
                    else:
                        corrupt_payload[field_name] = corrupt_value
                        values = {"payload": corrupt_payload}
                    session.execute(
                        update(LaunchplaneIdempotencyRow)
                        .where(LaunchplaneIdempotencyRow.record_id == reservation.record_id)
                        .values(**values)
                    )
                    session.commit()

                result = store.lookup_existing_mutation_reservation(
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                )
                store.close()

            self.assertEqual(result.status, "conflict")
            self.assertIsNone(result.record)

    def test_existing_mutation_lookup_reads_at_most_two_candidate_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            for attempt in range(3):
                store.reserve_mutation(
                    scope=f"github-actions:attempt-{attempt}",
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                    lease_owner=f"worker-{attempt}",
                    lease_seconds=300,
                )
            with patch.object(store, "_read_payload", wraps=store._read_payload) as read_payload:
                result = store.lookup_existing_mutation_reservation(
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="legacy-deploy-key",
                    request_fingerprint="exact-original-fingerprint",
                )
            store.close()

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(read_payload.call_count, 2)

    def test_existing_mutation_lookup_holds_on_malformed_running_lease_timestamp(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            reservation = store.reserve_mutation(
                scope="github-actions:attempt-1",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="legacy-deploy-key",
                request_fingerprint="exact-original-fingerprint",
                lease_owner="worker-a",
                lease_seconds=300,
            ).record
            corrupt_payload = reservation.model_dump(mode="json")
            corrupt_payload["lease_expires_at"] = "not-a-timestamp"
            with store._session_factory() as session:
                session.execute(
                    update(LaunchplaneIdempotencyRow)
                    .where(LaunchplaneIdempotencyRow.record_id == reservation.record_id)
                    .values(
                        lease_expires_at="not-a-timestamp",
                        payload=corrupt_payload,
                    )
                )
                session.commit()

            result = store.lookup_existing_mutation_reservation(
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
            )
            store.close()

        self.assertEqual(result.status, "hold_unknown")
        self.assertIsNone(result.record)

    def test_active_reconciliation_key_fences_other_idempotency_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                first = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:first",
                    request_fingerprint="mutation-fingerprint-a",
                    lease_owner="worker-a",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-123",
                )
                blocked = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:second",
                    request_fingerprint="mutation-fingerprint-b",
                    lease_owner="worker-b",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-123",
                )
                completion = complete_launchplane_mutation_reservation(
                    first.record,
                    response_status_code=202,
                    response_trace_id="trace-target-first",
                    completed_at=clock["now"],
                    response_payload={"status": "accepted"},
                )
                completed = store.complete_mutation_reservation(completion=completion)
                later = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:third",
                    request_fingerprint="mutation-fingerprint-c",
                    lease_owner="worker-c",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-123",
                )
            store.close()

        self.assertEqual(first.status, "acquired")
        self.assertEqual(blocked.status, "target_busy")
        self.assertEqual(blocked.record.idempotency_key, "mutation:target:first")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(later.status, "acquired")

    def test_provider_effect_checkpoint_fences_stale_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            reservation = store.reserve_mutation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key="mutation:effect-checkpoint",
                request_fingerprint="mutation-fingerprint-a",
                lease_owner="worker-a",
                lease_seconds=300,
                reconciliation_key="dokploy:compose:target-checkpoint",
            ).record
            checkpointed = store.checkpoint_mutation_provider_effect(
                reservation=reservation,
                effect_phase="deploy_trigger",
                lease_seconds=300,
            )
            stale_renewal = store.renew_mutation_reservation(
                reservation=reservation,
                lease_seconds=300,
            )
            store.close()

        self.assertEqual(checkpointed.status, "updated")
        assert checkpointed.record is not None
        self.assertEqual(checkpointed.record.provider_effect_phase, "deploy_trigger")
        self.assertTrue(checkpointed.record.provider_effect_started_at)
        self.assertEqual(stale_renewal.status, "reservation_mismatch")

    def test_stale_completion_cannot_adopt_newer_reconcile_required_attempt(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            first = store.reserve_mutation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key="mutation:late-completion",
                request_fingerprint="mutation-fingerprint-a",
                lease_owner="worker-a",
                lease_seconds=300,
                reconciliation_key="dokploy:compose:target-late-completion",
            ).record
            first_reconcile = store.mark_mutation_reconcile_required(
                reservation=first,
                reconciliation_key=first.reconciliation_key,
            )
            assert first_reconcile.record is not None
            retried = store.retry_reconciled_mutation(
                reservation=first_reconcile.record,
                lease_owner="worker-b",
                lease_seconds=300,
            )
            assert retried.record is not None
            second = retried.record
            second_reconcile = store.mark_mutation_reconcile_required(
                reservation=second,
                reconciliation_key=second.reconciliation_key,
            )
            stale_retry = store.retry_reconciled_mutation(
                reservation=first_reconcile.record,
                lease_owner="worker-c",
                lease_seconds=300,
            )
            stale_completion = complete_launchplane_mutation_reservation(
                first,
                response_status_code=202,
                response_trace_id="trace-worker-a",
                completed_at=first.updated_at,
                response_payload={"status": "accepted"},
            )
            completion_result = store.complete_mutation_reservation(completion=stale_completion)
            store.close()

        self.assertEqual(retried.status, "acquired")
        self.assertEqual(second.attempt, first.attempt + 1)
        self.assertEqual(second_reconcile.status, "updated")
        self.assertEqual(stale_retry.status, "reservation_mismatch")
        self.assertEqual(completion_result.status, "owner_mismatch")
        assert completion_result.record is not None
        self.assertEqual(completion_result.record.lease_owner, "worker-b")
        self.assertEqual(completion_result.record.state, "reconcile_required")

    def test_reservation_retries_when_active_collision_completes_before_lookup(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            owner_store = PostgresRecordStore(database_url=database_url)
            owner_store.ensure_schema()
            contender_store = PostgresRecordStore(database_url=database_url)
            incumbent = owner_store.reserve_mutation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key="mutation:collision:first",
                request_fingerprint="mutation-fingerprint-a",
                lease_owner="worker-a",
                lease_seconds=300,
                reconciliation_key="dokploy:compose:target-collision",
            ).record
            original_begin = contender_store._begin_serialized_write
            begin_calls = 0

            def begin_after_incumbent_release(session: object) -> None:
                nonlocal begin_calls
                begin_calls += 1
                if begin_calls == 2:
                    released = owner_store.release_reserved_mutation(reservation=incumbent)
                    self.assertEqual(released.status, "released")
                original_begin(session)

            with patch.object(
                contender_store,
                "_begin_serialized_write",
                side_effect=begin_after_incumbent_release,
            ):
                acquired = contender_store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:collision:second",
                    request_fingerprint="mutation-fingerprint-b",
                    lease_owner="worker-b",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-collision",
                )
            owner_store.close()
            contender_store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(acquired.record.idempotency_key, "mutation:collision:second")
        self.assertGreaterEqual(begin_calls, 3)

    def test_reconcile_required_target_stays_fenced(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            first = store.reserve_mutation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key="mutation:target:unknown",
                request_fingerprint="mutation-fingerprint-a",
                lease_owner="worker-a",
                lease_seconds=300,
                reconciliation_key="dokploy:compose:target-unknown",
            )
            marked = store.mark_mutation_reconcile_required(
                reservation=first.record,
                reconciliation_key="dokploy:compose:target-unknown",
            )
            blocked = store.reserve_mutation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key="mutation:target:replacement",
                request_fingerprint="mutation-fingerprint-b",
                lease_owner="worker-b",
                lease_seconds=300,
                reconciliation_key="dokploy:compose:target-unknown",
            )
            store.close()

        self.assertEqual(marked.status, "updated")
        self.assertEqual(blocked.status, "target_busy")
        self.assertEqual(blocked.record.state, "reconcile_required")

    def test_expired_reconcile_required_target_can_be_superseded(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                first = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:stale",
                    request_fingerprint="mutation-fingerprint-a",
                    lease_owner="worker-a",
                    lease_seconds=60,
                    reconciliation_key="dokploy:compose:target-stale",
                )
                marked = store.mark_mutation_reconcile_required(
                    reservation=first.record,
                    reconciliation_key="dokploy:compose:target-stale",
                )
                assert marked.record is not None
                clock["now"] = "2026-07-30T15:17:00Z"
                replacement = store.supersede_expired_reconciled_mutation_and_reserve(
                    reservation=marked.record,
                    response_status_code=409,
                    response_trace_id="destroy-trace",
                    response_payload={"status": "superseded"},
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:replacement",
                    request_fingerprint="mutation-fingerprint-b",
                    lease_owner="worker-b",
                    lease_seconds=300,
                    minimum_expired_seconds=900,
                    reconciliation_key="dokploy:compose:target-stale",
                    provider_target_key="dokploy:compose:target-stale",
                )
                replay = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:stale",
                    request_fingerprint="mutation-fingerprint-a",
                    lease_owner="worker-c",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-stale",
                )
                stored_stale = store.read_idempotency_record(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:stale",
                )
            assert replacement.record is not None
            assert stored_stale is not None
            store.close()

        self.assertEqual(marked.status, "updated")
        self.assertEqual(replacement.status, "acquired")
        self.assertEqual(replacement.record.state, "running")
        self.assertEqual(stored_stale.state, "completed")
        self.assertEqual(stored_stale.response_status_code, 409)
        self.assertEqual(replacement.status, "acquired")
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(replay.record.response_payload, {"status": "superseded"})

    def test_reconcile_required_target_cannot_be_superseded_before_lease_expiry(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                first = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:settling",
                    request_fingerprint="mutation-fingerprint-a",
                    lease_owner="worker-a",
                    lease_seconds=300,
                    reconciliation_key="dokploy:compose:target-settling",
                )
                marked = store.mark_mutation_reconcile_required(
                    reservation=first.record,
                    reconciliation_key="dokploy:compose:target-settling",
                )
                assert marked.record is not None
                superseded = store.supersede_expired_reconciled_mutation_and_reserve(
                    reservation=marked.record,
                    response_status_code=409,
                    response_trace_id="destroy-trace",
                    response_payload={"status": "superseded"},
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:replacement",
                    request_fingerprint="mutation-fingerprint-b",
                    lease_owner="worker-b",
                    lease_seconds=300,
                    minimum_expired_seconds=0,
                    reconciliation_key="dokploy:compose:target-settling",
                    provider_target_key="dokploy:compose:target-settling",
                )
            assert superseded.record is not None
            store.close()

        self.assertEqual(superseded.status, "lease_active")
        self.assertEqual(superseded.record.state, "reconcile_required")

    def test_expired_reconcile_required_target_waits_for_supersession_grace(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                first = store.reserve_mutation(
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:grace",
                    request_fingerprint="mutation-fingerprint-a",
                    lease_owner="worker-a",
                    lease_seconds=60,
                    reconciliation_key="dokploy:compose:target-grace",
                )
                marked = store.mark_mutation_reconcile_required(
                    reservation=first.record,
                    reconciliation_key="dokploy:compose:target-grace",
                )
                assert marked.record is not None
                clock["now"] = "2026-07-30T15:02:00Z"
                superseded = store.supersede_expired_reconciled_mutation_and_reserve(
                    reservation=marked.record,
                    response_status_code=409,
                    response_trace_id="destroy-trace",
                    response_payload={"status": "superseded"},
                    scope="github-actions:mutation-test",
                    route_path="/v1/test/mutation",
                    idempotency_key="mutation:target:replacement",
                    request_fingerprint="mutation-fingerprint-b",
                    lease_owner="worker-b",
                    lease_seconds=300,
                    minimum_expired_seconds=900,
                    reconciliation_key="dokploy:compose:target-grace",
                    provider_target_key="dokploy:compose:target-grace",
                )
            assert superseded.record is not None
            store.close()

        self.assertEqual(superseded.status, "grace_active")
        self.assertEqual(superseded.record.state, "reconcile_required")

    def test_db_only_mutation_preflight_releases_expired_unbound_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            mutation = _product_profile_db_only_mutation()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = store.reserve_mutation(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    request_fingerprint=mutation.request_fingerprint,
                    lease_owner="orphaned-worker",
                    lease_seconds=60,
                )
                clock["now"] = "2026-07-12T01:02:00Z"
                preflight = store.prepare_db_only_mutation(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    request_fingerprint=mutation.request_fingerprint,
                )
            stored_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(preflight.status, "released")
        self.assertIsNone(stored_record)

    def test_mutation_reconciliation_key_fences_expired_lease(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(store, _mutation_reservation())
                clock["now"] = "2026-07-12T01:01:00Z"
                renewed = store.renew_mutation_reservation(
                    reservation=acquired.record,
                    lease_seconds=600,
                )
                assert renewed.record is not None
                clock["now"] = "2026-07-12T01:02:00Z"
                bound = store.bind_mutation_reconciliation_key(
                    reservation=renewed.record,
                    reconciliation_key="provider-operation-123",
                )
                clock["now"] = "2026-07-12T01:12:00Z"
                reconcile_required = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-b"),
                )
            store.close()

        self.assertEqual(renewed.status, "updated")
        self.assertEqual(bound.status, "updated")
        self.assertEqual(reconcile_required.status, "reconcile_required")
        self.assertEqual(reconcile_required.record.state, "reconcile_required")
        self.assertEqual(
            reconcile_required.record.reconciliation_key,
            "provider-operation-123",
        )
        self.assertEqual(reconcile_required.record.attempt, 1)

    def test_mutation_owner_can_mark_unknown_provider_outcome_for_reconciliation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(
                    store,
                    _mutation_reservation(idempotency_key="mutation:test:unknown"),
                )
                clock["now"] = "2026-07-12T01:01:00Z"
                bound = store.bind_mutation_reconciliation_key(
                    reservation=acquired.record,
                    reconciliation_key="provider-operation-unknown",
                )
                assert bound.record is not None
                clock["now"] = "2026-07-12T01:02:00Z"
                marked = store.mark_mutation_reconcile_required(
                    reservation=bound.record,
                    reconciliation_key="provider-operation-unknown",
                )
            store.close()

        self.assertEqual(marked.status, "updated")
        self.assertIsNotNone(marked.record)
        assert marked.record is not None
        self.assertEqual(marked.record.state, "reconcile_required")
        self.assertEqual(marked.record.reconciliation_key, "provider-operation-unknown")

    def test_mutation_transitions_reject_stale_attempt_with_reused_owner(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-reused"),
                    lease_seconds=60,
                )
                clock["now"] = "2026-07-12T01:02:00Z"
                reclaimed = _reserve_mutation(
                    store,
                    _mutation_reservation(lease_owner="worker-reused"),
                )
                stale_completion = complete_launchplane_mutation_reservation(
                    acquired.record,
                    response_status_code=202,
                    response_trace_id="trace-stale-attempt",
                    completed_at="2026-07-12T01:02:00Z",
                    response_payload={"status": "accepted"},
                )
                renewed = store.renew_mutation_reservation(reservation=acquired.record)
                bound = store.bind_mutation_reconciliation_key(
                    reservation=acquired.record,
                    reconciliation_key="provider-operation-stale",
                )
                marked = store.mark_mutation_reconcile_required(
                    reservation=acquired.record,
                    reconciliation_key="provider-operation-stale",
                )
                completed = store.complete_mutation_reservation(
                    completion=stale_completion,
                )
            store.close()

        self.assertEqual(reclaimed.status, "acquired")
        self.assertEqual(reclaimed.record.attempt, 2)
        self.assertEqual(renewed.status, "reservation_mismatch")
        self.assertEqual(bound.status, "reservation_mismatch")
        self.assertEqual(marked.status, "reservation_mismatch")
        self.assertEqual(completed.status, "reservation_mismatch")

    def test_mutation_reservation_rejects_empty_idempotency_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires idempotency_key"):
            build_launchplane_mutation_reservation(
                scope="github-actions:mutation-test",
                route_path="/v1/test/mutation",
                idempotency_key=" ",
                request_fingerprint="mutation-fingerprint-a",
                lease_owner="worker-a",
                lease_expires_at="2026-07-12T01:05:00Z",
                reserved_at="2026-07-12T01:00:00Z",
            )

    def test_every_code_work_requests_round_trip_list_and_claim_once(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            older_record = _every_code_work_request(
                request_id="every-code-cbusillo-code-122-test",
                updated_at="2026-05-05T21:00:00Z",
            )
            newer_record = _every_code_work_request()
            store.write_every_code_work_request_record(older_record)
            store.write_every_code_work_request_record(newer_record)
            created_duplicate, duplicate_created = (
                store.create_every_code_work_request_record_if_absent(
                    newer_record.model_copy(update={"updated_at": "2026-05-05T23:00:00Z"})
                )
            )

            listed = store.list_every_code_work_request_records(state="queued")
            offset_listed = store.list_every_code_work_request_records(
                state="queued",
                limit=1,
                offset=1,
            )
            claimed = store.claim_every_code_work_request_record(
                request_id=newer_record.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            second_claim = store.claim_every_code_work_request_record(
                request_id=newer_record.request_id,
                host="Other-Host",
                claimed_at="2026-05-05T22:02:00Z",
            )
            loaded = store.read_every_code_work_request_record(newer_record.request_id)

        self.assertEqual(
            [record.request_id for record in listed],
            [newer_record.request_id, older_record.request_id],
        )
        self.assertEqual([record.request_id for record in offset_listed], [older_record.request_id])
        self.assertFalse(duplicate_created)
        self.assertEqual(created_duplicate.updated_at, newer_record.updated_at)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.state, "claimed")
        self.assertEqual(claimed.claimed_by_host, "Chris-Studio")
        self.assertIsNone(second_claim)
        self.assertEqual(loaded.state, "claimed")

    def test_every_code_work_request_claim_sets_lease_and_fencing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=600,
            )

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.fencing_token, 1)
        self.assertEqual(claimed.attempt, 1)
        self.assertEqual(claimed.lease_expires_at, "2026-05-05T22:11:00Z")

    def test_every_code_work_request_two_worker_only_one_claims(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            first_claim = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
            )
            second_claim = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-2",
                claimed_at="2026-05-05T22:01:01Z",
            )
            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertIsNotNone(first_claim)
        self.assertIsNone(second_claim)
        assert first_claim is not None
        self.assertEqual(loaded.claimed_by_host, "worker-1")
        self.assertEqual(loaded.fencing_token, 1)

    def test_every_code_work_request_stale_owner_rejected_by_fencing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
            )
            assert claimed is not None
            stale_fencing_token = 99

            from control_plane.contracts.every_code_work_request import (
                EveryCodeWorkRequestStatusUpdate,
            )

            with self.assertRaises(ValueError) as ctx:
                store.update_every_code_work_request_status_record(
                    request_id=claimed.request_id,
                    update=EveryCodeWorkRequestStatusUpdate(
                        state="done",
                        host="worker-1",
                        updated_at="2026-05-05T22:05:00Z",
                        fencing_token=stale_fencing_token,
                        result_summary="done",
                    ),
                )
            with self.assertRaises(ValueError):
                store.update_every_code_work_request_status_record(
                    request_id=claimed.request_id,
                    update=EveryCodeWorkRequestStatusUpdate(
                        state="done",
                        host="worker-1",
                        fencing_token=claimed.fencing_token + 1,
                        updated_at="2026-05-05T22:05:00Z",
                        result_summary="done",
                    ),
                )
        self.assertIn("fencing token", str(ctx.exception))

    def test_every_code_work_request_heartbeat_extends_lease(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=600,
            )
            assert claimed is not None

            accepted = store.heartbeat_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                fencing_token=claimed.fencing_token,
                heartbeat_at="2026-05-05T22:05:00Z",
                lease_expires_at="2026-05-05T22:15:00Z",
            )
            refreshed = store.read_every_code_work_request_record(record.request_id)

        self.assertTrue(accepted)
        self.assertEqual(refreshed.lease_expires_at, "2026-05-05T22:15:00Z")

    def test_every_code_work_request_heartbeat_rejects_wrong_fencing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
            )
            assert claimed is not None

            rejected = store.heartbeat_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                fencing_token=99,
                heartbeat_at="2026-05-05T22:05:00Z",
                lease_expires_at="2026-05-05T22:35:00Z",
            )

        self.assertFalse(rejected)

    def test_every_code_work_request_heartbeat_rejects_wrong_host(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
            )
            assert claimed is not None

            rejected = store.heartbeat_every_code_work_request_record(
                request_id=record.request_id,
                host="attacker-2",
                fencing_token=claimed.fencing_token,
                heartbeat_at="2026-05-05T22:05:00Z",
                lease_expires_at="2026-05-05T22:35:00Z",
            )

        self.assertFalse(rejected)

    def test_every_code_work_request_stale_lease_listed_for_recovery(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=600,
            )
            assert claimed is not None

            fresh_stale = store.list_stale_every_code_work_request_records(
                as_of="2026-05-05T22:05:00Z",
            )
            expired_stale = store.list_stale_every_code_work_request_records(
                as_of="2026-05-05T23:00:00Z",
            )

        self.assertEqual(len(fresh_stale), 0)
        self.assertEqual(len(expired_stale), 1)
        self.assertEqual(expired_stale[0].request_id, record.request_id)

    def test_every_code_work_request_stale_recovery_safe_requeues(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=600,
            )
            assert claimed is not None

            requeued = store.recover_stale_every_code_work_request_record(
                expected_record=claimed,
                recovered_at="2026-05-05T23:00:00Z",
            )
            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertIsNotNone(requeued)
        self.assertEqual(loaded.state, "queued")
        self.assertEqual(loaded.fencing_token, 0)

    def test_every_code_work_request_stale_recovery_rejects_heartbeated_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=60,
            )
            assert claimed is not None
            stale_snapshot = store.list_stale_every_code_work_request_records(
                as_of="2026-05-05T22:03:00Z"
            )[0]
            heartbeat_accepted = store.heartbeat_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                fencing_token=claimed.fencing_token,
                heartbeat_at="2026-05-05T22:02:30Z",
                lease_expires_at="2026-05-05T22:12:30Z",
            )

            recovered = store.recover_stale_every_code_work_request_record(
                expected_record=stale_snapshot,
                recovered_at="2026-05-05T22:03:00Z",
            )
            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertTrue(heartbeat_accepted)
        self.assertIsNone(recovered)
        self.assertEqual(loaded.state, "claimed")
        self.assertEqual(loaded.lease_expires_at, "2026-05-05T22:12:30Z")

    def test_every_code_work_request_process_death_recovery_increments_attempt(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            first = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                lease_seconds=600,
            )
            assert first is not None
            self.assertEqual(first.attempt, 1)
            requeued = store.recover_stale_every_code_work_request_record(
                expected_record=first,
                recovered_at="2026-05-05T23:00:00Z",
            )
            assert requeued is not None

            second = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-2",
                claimed_at="2026-05-05T23:01:00Z",
                lease_seconds=600,
            )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.attempt, 2)
        self.assertEqual(second.fencing_token, 2)
        self.assertEqual(second.claimed_by_host, "worker-2")

    def test_every_code_work_request_lost_response_replay_uses_idempotent_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            def idempotency_record_factory(
                claimed_record: EveryCodeWorkRequestRecord,
            ) -> LaunchplaneIdempotencyRecord:
                return LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id="trace-every-code-claim"
                    ),
                    scope="terminal-agent:every-code-worker",
                    route_path="/v1/every-code/work-requests/claim",
                    idempotency_key="every-code-claim-test",
                    request_fingerprint="claim-fingerprint",
                    response_status_code=202,
                    response_trace_id="trace-every-code-claim",
                    recorded_at="2026-05-05T22:01:00Z",
                    response_payload={
                        "status": "accepted",
                        "result": {"request": claimed_record.model_dump(mode="json")},
                    },
                )

            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:01:00Z",
                idempotency_record_factory=idempotency_record_factory,
            )
            assert claimed is not None
            stored_idempotency = store.read_idempotency_record(
                scope="terminal-agent:every-code-worker",
                route_path="/v1/every-code/work-requests/claim",
                idempotency_key="every-code-claim-test",
            )

            second_claim_attempt = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-1",
                claimed_at="2026-05-05T22:02:00Z",
            )

        self.assertIsNone(second_claim_attempt)
        self.assertEqual(claimed.fencing_token, 1)
        self.assertIsNotNone(stored_idempotency)
        assert stored_idempotency is not None
        self.assertEqual(stored_idempotency.state, "completed")

    def test_every_code_pr_feedback_round_trip_and_filter(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            older_record = EveryCodePrFeedbackRecord(
                feedback_id="every-code-pr-feedback-cbusillo-code-26-old",
                request_id="every-code-cbusillo-code-123-test",
                repository="cbusillo/code",
                pr_number=26,
                pr_url="https://github.com/cbusillo/code/pull/26",
                feedback_kind="issue_comment",
                github_delivery_id="delivery-old",
                github_id="100",
                actor="cbusillo",
                body="Please adjust the README wording.",
                received_at="2026-05-06T17:00:00Z",
            )
            newer_record = older_record.model_copy(
                update={
                    "feedback_id": "every-code-pr-feedback-cbusillo-code-26-new",
                    "github_delivery_id": "delivery-new",
                    "github_id": "101",
                    "received_at": "2026-05-06T18:00:00Z",
                }
            )
            store.write_every_code_pr_feedback_record(older_record)
            store.write_every_code_pr_feedback_record(newer_record)

            listed = store.list_every_code_pr_feedback_records(
                repository="cbusillo/code",
                pr_number=26,
                status="pending",
            )
            offset_listed = store.list_every_code_pr_feedback_records(
                repository="cbusillo/code",
                pr_number=26,
                status="pending",
                limit=1,
                offset=1,
            )

        self.assertEqual(
            [record.feedback_id for record in listed],
            [newer_record.feedback_id, older_record.feedback_id],
        )
        self.assertEqual(
            [record.feedback_id for record in offset_listed], [older_record.feedback_id]
        )

    def test_human_sessions_round_trip_and_delete(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            session = _human_session()
            store.write_session(session)
            loaded = store.read_session(session.session_id)
            store.delete_session(session.session_id)
            deleted = store.read_session(session.session_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.identity.login, "alice")
        self.assertEqual(loaded.identity.role, "admin")
        self.assertEqual(loaded.identity.teams, frozenset({"shinycomputers/launchplane-admins"}))
        self.assertEqual(loaded.csrf_generation, 0)
        self.assertIsNone(deleted)

    def test_human_session_csrf_generation_compare_and_write_is_atomic(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            original_session = _human_session()
            rotated_session = _human_session(csrf_generation=1)
            store.write_session(original_session)

            rotated = store.write_session_if_csrf_generation(
                rotated_session,
                expected_generation=0,
            )
            replayed = store.write_session_if_csrf_generation(
                rotated_session,
                expected_generation=0,
            )
            loaded = store.read_session(original_session.session_id)

        self.assertTrue(rotated)
        self.assertFalse(replayed)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.csrf_generation, 1)

    def test_expired_human_session_reads_as_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            expired_session = LaunchplaneHumanSession(
                session_id="expired-session",
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=1),
                identity=_human_session().identity,
            )

            store.write_session(expired_session)
            loaded = store.read_session(expired_session.session_id)

        self.assertIsNone(loaded)

    def test_storage_import_core_records_is_removed(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "storage",
                "import-core-records",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such command 'import-core-records'", result.output)

    def test_secrets_put_requires_direct_db_acknowledgement(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "secrets",
                "put",
                "--database-url",
                "postgresql://launchplane:test@db/launchplane",
                "--scope",
                "context_instance",
                "--integration",
                "dokploy",
                "--name",
                "token",
                "--binding-key",
                "DOKPLOY_TOKEN",
                "--value",
                "secret-value",
                "--context",
                "verireel",
                "--instance",
                "prod",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Direct local DB mutation is restricted", result.output)
        self.assertIn("secret writes", result.output)
        self.assertIn("--allow-direct-db-mutation", result.output)
        self.assertNotIn("secret-value", result.output)

    def test_secrets_put_allows_explicit_bootstrap_repair(self) -> None:
        postgres_store = Mock()
        secret_result = {"secret_id": "secret-dokploy-token", "version_id": "version-1"}
        secret_status = {
            "secret_id": "secret-dokploy-token",
            "binding_key": "DOKPLOY_TOKEN",
            "status": "configured",
        }

        with (
            patch(
                "control_plane.cli_storage_secrets.PostgresRecordStore",
                return_value=postgres_store,
            ) as store_class,
            patch(
                "control_plane.cli_storage_secrets.control_plane_secrets.write_secret_value",
                return_value=secret_result,
            ) as write_secret_value,
            patch(
                "control_plane.cli_storage_secrets.control_plane_secrets.build_secret_status",
                return_value=secret_status,
            ) as build_secret_status,
        ):
            result = CliRunner().invoke(
                main,
                [
                    "secrets",
                    "put",
                    "--database-url",
                    "postgresql://launchplane:test@db/launchplane",
                    "--scope",
                    "context_instance",
                    "--integration",
                    "dokploy",
                    "--name",
                    "token",
                    "--binding-key",
                    "DOKPLOY_TOKEN",
                    "--value",
                    "secret-value",
                    "--context",
                    "verireel",
                    "--instance",
                    "prod",
                    "--actor",
                    "local-repair",
                    "--allow-direct-db-mutation",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        store_class.assert_called_once_with(
            database_url="postgresql://launchplane:test@db/launchplane"
        )
        postgres_store.ensure_schema.assert_called_once_with()
        write_secret_value.assert_called_once_with(
            record_store=postgres_store,
            scope="context_instance",
            integration="dokploy",
            name="token",
            plaintext_value="secret-value",
            binding_key="DOKPLOY_TOKEN",
            context_name="verireel",
            instance_name="prod",
            description="",
            actor="local-repair",
        )
        build_secret_status.assert_called_once_with(
            postgres_store, secret_id="secret-dokploy-token"
        )
        postgres_store.close.assert_called_once_with()
        self.assertNotIn("secret-value", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["secret"], secret_status)

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )

            store.ensure_schema()
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            loaded_record = store.read_deployment_record("deployment-20260420T153000Z-opw-testing")
            store.close()

        self.assertEqual(store.backend_name, "postgres")
        self.assertEqual(loaded_record.context, "opw")
        resolved_target = loaded_record.resolved_target
        assert resolved_target is not None
        self.assertEqual(resolved_target.target_id, "compose-123")
        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "dokploy")
        self.assertEqual(deployed_target.target_category, "compose")

    def test_write_and_read_provider_neutral_deployment_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = DeploymentRecord(
                record_id="deployment-20260420T153000Z-syo-prod",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="artifact-20260420-a1b2c3d4"
                ),
                context="syo",
                instance="prod",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                deployed_target=DeployedTargetReference(
                    provider_id="fake-cloud",
                    target_category="service",
                    target_id="svc-123",
                    display_name="syo-prod-service",
                    provider_target_type="managed-service",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-service",
                    target_type="application",
                    deploy_mode="fake-cloud-service-api",
                    provider_id="fake-cloud",
                    target_category="service",
                    provider_target_type="managed-service",
                    deployment_id="deploy-123",
                    status="pass",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                ),
            )

            store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            store.close()

        self.assertIsNone(loaded_record.resolved_target)
        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.target_category, "service")
        self.assertEqual(deployed_target.target_id, "svc-123")
        self.assertEqual(loaded_record.deploy.provider_id, "fake-cloud")
        self.assertEqual(loaded_record.deploy.target_category, "service")
        self.assertEqual(loaded_record.deploy.provider_target_type, "managed-service")

    def test_write_and_read_resolved_target_uses_deploy_provider_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = DeploymentRecord(
                record_id="deployment-20260420T153000Z-syo-prod",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="artifact-20260420-a1b2c3d4"
                ),
                context="syo",
                instance="prod",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="svc-123",
                    target_name="syo-prod-service",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-service",
                    target_type="application",
                    deploy_mode="fake-cloud-service-api",
                    provider_id="fake-cloud",
                    target_category="service",
                    provider_target_type="managed-service",
                    deployment_id="deploy-123",
                    status="pass",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                ),
            )

            store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            store.close()

        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.provider_target_type, "managed-service")
        self.assertEqual(deployed_target.target_id, "svc-123")

    def test_write_and_list_generic_web_rollback_plan_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_generic_web_rollback_plan_record(
                _generic_web_rollback_plan_record(
                    plan_id="rollback-plan-older",
                    created_at="2026-05-01T21:00:00Z",
                )
            )
            store.write_generic_web_rollback_plan_record(
                _generic_web_rollback_plan_record(
                    plan_id="rollback-plan-newer",
                    created_at="2026-05-01T22:00:00Z",
                )
            )
            listed_records = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing", instance_name="prod"
            )
            store.close()

        self.assertEqual(
            [record.plan_id for record in listed_records],
            ["rollback-plan-newer", "rollback-plan-older"],
        )

    def test_list_preview_records_filters_and_limits(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-101",
                    updated_at="2026-04-20T10:01:00Z",
                    pr_number=101,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-102",
                    updated_at="2026-04-20T10:03:00Z",
                    pr_number=102,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-103",
                    updated_at="2026-04-20T10:02:00Z",
                    pr_number=103,
                )
            )

            listed_records = store.list_preview_records(
                context_name="verireel-testing",
                anchor_repo="verireel",
                limit=2,
            )
            store.close()

        self.assertEqual(
            [record.preview_id for record in listed_records],
            [
                "preview-verireel-testing-verireel-pr-102",
                "preview-verireel-testing-verireel-pr-103",
            ],
        )

    def test_write_read_and_list_preview_enablement_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            older_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-101",
                updated_at="2026-04-20T10:01:00Z",
                pr_number=101,
            )
            newer_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-102",
                updated_at="2026-04-20T10:03:00Z",
                pr_number=102,
            )
            closed_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-103",
                updated_at="2026-04-20T10:02:00Z",
                pr_number=103,
            ).model_copy(update={"pr_state": "closed"})

            store.write_preview_enablement_record(older_record)
            store.write_preview_enablement_record(newer_record)
            store.write_preview_enablement_record(closed_record)
            read_record = store.read_preview_enablement_record(newer_record.record_id)
            listed_records = store.list_preview_enablement_records(
                context_name="verireel-testing",
                anchor_repo="verireel",
                pr_state="open",
                limit=2,
            )
            store.close()

        self.assertEqual(read_record.anchor_pr_number, 102)
        self.assertEqual(
            [record.record_id for record in listed_records],
            ["verireel-testing-verireel-pr-102", "verireel-testing-verireel-pr-101"],
        )

    def test_preview_summaries_include_latest_generation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            preview = _preview_record(
                preview_id="preview-verireel-testing-verireel-pr-123",
                updated_at="2026-04-20T10:05:00Z",
                pr_number=123,
            )
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id=preview.preview_id,
                )
            )
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0002",
                    preview_id=preview.preview_id,
                ).model_copy(
                    update={
                        "sequence": 2,
                        "requested_at": "2026-04-20T10:06:00Z",
                        "ready_at": "2026-04-20T10:08:00Z",
                        "finished_at": "2026-04-20T10:08:00Z",
                        "artifact_id": "artifact-verireel-pr-123-bbbbbbbb",
                    }
                )
            )

            summary = store.read_preview_summary(preview_id=preview.preview_id)
            listed_summaries = store.list_preview_summaries(
                context_name="verireel-testing",
                anchor_repo="verireel",
                generation_limit=1,
            )
            store.close()

        latest_generation = summary.latest_generation
        assert latest_generation is not None
        self.assertEqual(summary.preview.preview_id, preview.preview_id)
        self.assertEqual(
            latest_generation.generation_id,
            "preview-verireel-testing-verireel-pr-123-generation-0002",
        )
        self.assertEqual(len(summary.recent_generations), 2)
        self.assertEqual(len(listed_summaries), 1)
        self.assertEqual(len(listed_summaries[0].recent_generations), 1)
        listed_latest_generation = listed_summaries[0].latest_generation
        assert listed_latest_generation is not None
        self.assertEqual(listed_latest_generation.sequence, 2)

    def test_preview_inventory_scan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=2,
                    preview_slugs=("pr-123", "pr-124"),
                )
            )
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100600Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:06:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=0,
                    preview_slugs=(),
                )
            )
            listed_records = store.list_preview_inventory_scan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].scan_id,
            "preview-inventory-scan-verireel-testing-20260420T100600Z",
        )
        self.assertEqual(listed_records[0].preview_count, 0)

    def test_authz_policy_records_round_trip(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                    }
                ]
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            seeded_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-20260420T100500Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-04-20T10:05:00Z",
                    policy=policy,
                )
            )
            listed_records = store.list_authz_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(listed_records[0], seeded_record)
        self.assertEqual(listed_records[0].revision, 1)
        self.assertEqual(
            listed_records[0].policy.github_actions[0].repository, "cbusillo/launchplane"
        )

    def test_authz_policy_compare_write_supersedes_active_history_and_detects_conflict(
        self,
    ) -> None:
        initial_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    }
                ]
            }
        )
        replacement_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "managed_set_id": "operator.launchplane",
                        "managed_rule_id": "profile.read",
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    }
                ],
            }
        )
        initial_seed = LaunchplaneAuthzPolicyRecord(
            record_id="launchplane-authz-policy-seed",
            status="active",
            source="test:initial",
            updated_at="2026-07-18T06:00:00Z",
            policy=initial_policy,
        )
        replacement_template = LaunchplaneAuthzPolicyRecord(
            record_id="launchplane-authz-policy-replacement",
            revision=2,
            status="active",
            source="test:replacement",
            updated_at="2026-07-18T06:01:00Z",
            policy=replacement_policy,
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            initial_record = store.seed_authz_policy_if_absent(initial_seed)
            replacement_record = replacement_template.model_copy(
                update={
                    "record_id": build_authz_policy_record_id(
                        revision=2,
                        policy_sha256=replacement_template.policy_sha256,
                    )
                }
            )

            write_result = store.compare_and_write_authz_policy_record(
                expected_record=initial_record,
                replacement_record=replacement_record,
            )
            conflict_result = store.compare_and_write_authz_policy_record(
                expected_record=initial_record,
                replacement_record=replacement_record.model_copy(
                    update={
                        "record_id": "launchplane-authz-policy-conflict",
                    }
                ),
            )
            active_records = store.list_authz_policy_records(status="active")
            superseded_records = store.list_authz_policy_records(status="superseded")
            store.close()

        self.assertEqual(write_result.status, "written")
        self.assertEqual(conflict_result.status, "stale")
        self.assertIsNotNone(conflict_result.current_record)
        assert conflict_result.current_record is not None
        self.assertEqual(conflict_result.current_record.record_id, replacement_record.record_id)
        self.assertEqual(
            tuple(record.record_id for record in active_records), (replacement_record.record_id,)
        )
        self.assertEqual(
            {record.record_id for record in superseded_records},
            {initial_record.record_id},
        )

    def test_authz_policy_compare_write_completes_idempotency_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            current_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T06:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            replacement_policy = LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(
                    GitHubActionsPolicyRule(
                        repository="cbusillo/launchplane",
                        actions=("product_profile.read",),
                    ),
                ),
            )
            replacement_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=authz_policy_sha256(replacement_policy),
                ),
                revision=2,
                source="service:authz-managed-rule-set-reconcile",
                updated_at="2026-07-18T06:01:00Z",
                policy=replacement_policy,
            )
            mutation = DbOnlyMutationRequest(
                scope="github-actions:authz-test",
                route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                idempotency_key="authz:test:apply:1",
                request_fingerprint="fingerprint-a",
                lease_owner="trace-authz",
                response_status_code=202,
                response_trace_id="trace-authz",
                response_payload={"status": "accepted", "trace_id": "trace-authz"},
            )

            written = store.compare_and_write_authz_policy_record(
                expected_record=current_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            replayed = store.compare_and_write_authz_policy_record(
                expected_record=current_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            conflict = store.compare_and_write_authz_policy_record(
                expected_record=current_record,
                replacement_record=replacement_record,
                mutation=DbOnlyMutationRequest(
                    **{
                        **mutation.__dict__,
                        "request_fingerprint": "fingerprint-b",
                    }
                ),
            )
            active_records = store.list_authz_policy_records(status="active")
            superseded_records = store.list_authz_policy_records(status="superseded")
            store.close()

        self.assertEqual(written.status, "written")
        self.assertIsNotNone(written.idempotency_record)
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(conflict.status, "idempotency_conflict")
        self.assertEqual(active_records, (replacement_record,))
        self.assertEqual(
            superseded_records, (current_record.model_copy(update={"status": "superseded"}),)
        )

    def test_authz_policy_compare_write_rolls_back_policy_and_idempotency_together(self) -> None:
        class FailingAuthzStore(PostgresRecordStore):
            def _after_authz_policy_write_step(self, step_name: str) -> None:
                if step_name == "insert_active":
                    raise RuntimeError("injected authz write failure")

        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            current_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T06:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            store.close()
            replacement_policy = LaunchplaneAuthzPolicy(schema_version=2)
            replacement_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=authz_policy_sha256(replacement_policy),
                ),
                revision=2,
                source="service:authz-managed-rule-set-reconcile",
                updated_at="2026-07-18T06:01:00Z",
                policy=replacement_policy,
            )
            failing_store = FailingAuthzStore(database_url=database_url)
            with self.assertRaisesRegex(RuntimeError, "injected authz write failure"):
                failing_store.compare_and_write_authz_policy_record(
                    expected_record=current_record,
                    replacement_record=replacement_record,
                    mutation=DbOnlyMutationRequest(
                        scope="github-actions:authz-test",
                        route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                        idempotency_key="authz:test:rollback",
                        request_fingerprint="fingerprint-rollback",
                        lease_owner="trace-authz-rollback",
                        response_status_code=202,
                        response_trace_id="trace-authz-rollback",
                        response_payload={"status": "accepted"},
                    ),
                )
            active_records = failing_store.list_authz_policy_records(status="active")
            idempotency_record = failing_store.read_idempotency_record(
                scope="github-actions:authz-test",
                route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                idempotency_key="authz:test:rollback",
            )
            failing_store.close()

        self.assertEqual(active_records, (current_record,))
        self.assertIsNone(idempotency_record)

    def test_authz_policy_seed_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            seed = LaunchplaneAuthzPolicyRecord(
                record_id="seed",
                source="test",
                updated_at="2026-07-18T00:00:00Z",
                policy=LaunchplaneAuthzPolicy(),
            )
            first = store.seed_authz_policy_if_absent(seed)
            second = store.seed_authz_policy_if_absent(
                seed.model_copy(update={"source": "test:ignored"})
            )
            listed_records = store.list_authz_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(first, second)
        self.assertEqual(len(listed_records), 1)
        self.assertEqual(listed_records[0].source, "test")

    def test_runtime_key_safety_policy_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="runtime-key-safety-policy-20260505T200000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-05T20:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="SHOPIFY_ACCESS_TOKEN",
                            secret_class="testing",
                            allowed_contexts=("opw",),
                            allowed_instances=("testing",),
                        ),
                    ),
                )
            )
            listed_records = store.list_runtime_key_safety_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].record_id,
            "runtime-key-safety-policy-20260505T200000Z-test",
        )
        self.assertEqual(listed_records[0].rules[0].secret_class, "testing")

    def test_runtime_key_safety_cli_imports_lists_and_evaluates_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            policy_file = Path(temporary_directory_name) / "runtime-key-safety.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-05-05T20:00:00Z",
                        "rules": [
                            {
                                "binding_key": "SHOPIFY_ACCESS_TOKEN",
                                "secret_class": "testing",
                                "allowed_contexts": ["opw"],
                                "allowed_instances": ["testing"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_secret_binding(
                SecretBinding(
                    binding_id="binding-shopify-token",
                    secret_id="secret-shopify-token",
                    integration="runtime_environment",
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    context="opw",
                    instance="testing",
                    created_at="2026-05-05T20:00:00Z",
                    updated_at="2026-05-05T20:00:00Z",
                )
            )
            store.close()
            runner = CliRunner()

            import_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "import-policy",
                    "--database-url",
                    database_url,
                    "--policy-file",
                    str(policy_file),
                    "--source-label",
                    "test",
                    "--allow-direct-db-mutation",
                ],
            )
            list_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "list-policies",
                    "--database-url",
                    database_url,
                    "--status",
                    "active",
                ],
            )
            evaluate_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "evaluate",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--environment-class",
                    "testing",
                    "--binding-key",
                    "SHOPIFY_ACCESS_TOKEN",
                ],
            )

        self.assertEqual(import_result.exit_code, 0, import_result.output)
        self.assertIn('"rule_count": 1', import_result.output)
        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertEqual(evaluate_result.exit_code, 0, evaluate_result.output)
        self.assertIn('"status": "pass"', evaluate_result.output)

    def test_runtime_key_safety_import_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "runtime-key-safety.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-05-05T20:00:00Z",
                        "rules": [
                            {
                                "binding_key": "SHOPIFY_ACCESS_TOKEN",
                                "secret_class": "testing",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "runtime-key-safety",
                    "import-policy",
                    "--database-url",
                    "postgresql://launchplane:test@db/launchplane",
                    "--policy-file",
                    str(policy_file),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Direct local DB mutation is restricted", result.output)
        self.assertIn("--allow-direct-db-mutation", result.output)

    def test_merge_train_policy_cli_imports_and_lists_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            policy_file = Path(temporary_directory_name) / "merge-train-policy.toml"
            policy_file.write_text(
                """
schema_version = 1

[[policies]]
repository = "cbusillo/codex-skills"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"
[policies.github_token]
env_var = "GH_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            runner = CliRunner()

            import_result = runner.invoke(
                main,
                [
                    "merge-train-policies",
                    "import-policy",
                    "--database-url",
                    database_url,
                    "--apply",
                    "--policy-file",
                    str(policy_file),
                    "--source-label",
                    "test",
                    "--allow-direct-db-mutation",
                ],
            )
            list_result = runner.invoke(
                main,
                [
                    "merge-train-policies",
                    "list",
                    "--database-url",
                    database_url,
                    "--status",
                    "active",
                ],
            )

        self.assertEqual(import_result.exit_code, 0, import_result.output)
        self.assertIn('"repository_count": 1', import_result.output)
        self.assertIn('"cbusillo/codex-skills:main"', import_result.output)
        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertIn('"cbusillo/codex-skills:main"', list_result.output)

    def test_merge_train_policy_cli_builds_service_import_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "merge-train-policy.toml"
            policy_file.write_text(
                """
schema_version = 1

[[policies]]
repository = "cbusillo/codex-skills"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"
[policies.github_token]
env_var = "GH_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "merge-train-policies",
                    "build-import-request",
                    "--policy-file",
                    str(policy_file),
                    "--source-label",
                    "workflow:merge-train-policy-import",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["product"], "launchplane")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["reason"], "")
        record = payload["record"]
        self.assertEqual(record["source"], "workflow:merge-train-policy-import")
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["policy"]["policies"][0]["repository"], "cbusillo/codex-skills")
        self.assertEqual(record["policy"]["policies"][0]["base_branch"], "main")
        self.assertEqual(
            record["policy"]["policies"][0]["stack_child_disposition_label"],
            "stack-landed",
        )

    def test_merge_train_policy_build_import_request_apply_requires_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "merge-train-policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")
            result = CliRunner().invoke(
                main,
                [
                    "merge-train-policies",
                    "build-import-request",
                    "--policy-file",
                    str(policy_file),
                    "--apply",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("reason is required when apply is true", result.output)

    def test_merge_train_policy_direct_import_requires_direct_db_acknowledgement(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "merge-train-policy.toml"
            policy_file.write_text(
                """
schema_version = 1

[[policies]]
repository = "cbusillo/codex-skills"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
merge_method = "merge"
failure_policy = "pause_train"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner"]
[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"
[policies.github_token]
env_var = "GH_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "merge-train-policies",
                    "import-policy",
                    "--database-url",
                    "postgresql://launchplane:test@db/launchplane",
                    "--apply",
                    "--policy-file",
                    str(policy_file),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Direct local DB mutation is restricted", result.output)
        self.assertIn("--allow-direct-db-mutation", result.output)

    def test_authz_policy_direct_import_command_is_removed(self) -> None:
        result = CliRunner().invoke(
            main,
            ["authz-policies", "import-toml"],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such command 'import-toml'", result.output)

    def test_product_profile_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_profile_record())
            store.write_product_profile_record(_product_profile_record(product="internal-tool"))
            loaded_record = store.read_product_profile_record("sellyouroutboard")
            listed_records = store.list_product_profile_records(driver_id="generic-web")
            store.close()

        self.assertEqual(loaded_record.driver_id, "generic-web")
        self.assertEqual(loaded_record.image.repository, "ghcr.io/cbusillo/sellyouroutboard")
        self.assertEqual(loaded_record.preview.context, "sellyouroutboard-testing")
        self.assertEqual(
            [record.product for record in listed_records], ["internal-tool", "sellyouroutboard"]
        )

    def test_product_profile_compare_and_write_replaces_matching_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_record = _product_profile_record()
            replacement_record = expected_record.model_copy(
                update={
                    "display_name": "Updated Product",
                    "updated_at": "2026-07-12T01:00:00Z",
                    "source": "service:test-compare-write",
                }
            )
            store.write_product_profile_record(expected_record)

            result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=replacement_record,
            )
            loaded_record = store.read_product_profile_record(expected_record.product)
            store.close()

        self.assertEqual(result.status, "written")
        self.assertEqual(loaded_record, replacement_record)

    def test_product_profile_compare_and_write_rejects_changed_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_record = _product_profile_record()
            concurrent_record = expected_record.model_copy(
                update={
                    "display_name": "Concurrent Update",
                    "updated_at": "2026-07-12T01:00:00Z",
                    "source": "service:concurrent-update",
                }
            )
            replacement_record = expected_record.model_copy(
                update={
                    "updated_at": "2026-07-12T02:00:00Z",
                    "source": "service:test-compare-write",
                }
            )
            store.write_product_profile_record(concurrent_record)

            result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=replacement_record,
            )
            loaded_record = store.read_product_profile_record(expected_record.product)
            store.close()

        self.assertEqual(result.status, "changed")
        self.assertEqual(loaded_record, concurrent_record)

    def test_product_profile_compare_and_write_rejects_changed_provider_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_profile = _product_profile_record()
            replacement_profile = expected_profile.model_copy(
                update={
                    "updated_at": "2026-07-12T02:00:00Z",
                    "source": "service:test-compare-write",
                }
            )
            source_target = _provider_target_record(
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="shared-target",
            )
            target_target = _provider_target_record(
                context="sellyouroutboard",
                instance="testing",
                target_id="shared-target",
            )
            changed_target = target_target.model_copy(update={"updated_at": "2026-07-12T01:30:00Z"})
            store.write_product_profile_record(expected_profile)
            store.write_provider_target_record(source_target)
            store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=target_target,
                            expected_absent=True,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                            ),
                        ),
                    )
                )
            )
            store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=changed_target,
                            expected_record=target_target,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                            ),
                        ),
                    )
                )
            )

            result = store.compare_and_write_product_profile_record(
                expected_record=expected_profile,
                replacement_record=replacement_profile,
                expected_provider_targets=(source_target, target_target),
            )
            loaded_profile = store.read_product_profile_record(expected_profile.product)
            store.close()

        self.assertEqual(result.status, "changed")
        self.assertEqual(loaded_profile, expected_profile)

    def test_product_profile_compare_and_write_rejects_new_provider_route_claim(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_profile = _product_profile_record()
            replacement_profile = expected_profile.model_copy(
                update={
                    "updated_at": "2026-07-12T02:00:00Z",
                    "source": "service:test-compare-write",
                }
            )
            source_target = _provider_target_record(
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="shared-target",
            )
            target_target = _provider_target_record(
                context="sellyouroutboard",
                instance="testing",
                target_id="shared-target",
            )
            unexpected_target = _provider_target_record(
                context="other-context",
                instance="testing",
                target_id="shared-target",
            )
            store.write_product_profile_record(expected_profile)
            store.write_provider_target_record(source_target)
            store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=target_target,
                            expected_absent=True,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                            ),
                        ),
                        ProviderTargetWrite(
                            record=unexpected_target,
                            expected_absent=True,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                                (target_target.context, target_target.instance),
                            ),
                        ),
                    )
                )
            )

            result = store.compare_and_write_product_profile_record(
                expected_record=expected_profile,
                replacement_record=replacement_profile,
                expected_provider_targets=(source_target, target_target),
            )
            loaded_profile = store.read_product_profile_record(expected_profile.product)
            store.close()

        self.assertEqual(result.status, "changed")
        self.assertEqual(loaded_profile, expected_profile)

    def test_product_profile_compare_and_write_serializes_sqlite_writers(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_record = _product_profile_record()
            store.write_product_profile_record(expected_record)

            def write_replacement(display_name: str) -> str:
                replacement_record = expected_record.model_copy(
                    update={
                        "display_name": display_name,
                        "updated_at": "2026-07-12T01:00:00Z",
                        "source": f"service:{display_name}",
                    }
                )
                return store.compare_and_write_product_profile_record(
                    expected_record=expected_record,
                    replacement_record=replacement_record,
                ).status

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = tuple(executor.map(write_replacement, ("first", "second")))
            loaded_record = store.read_product_profile_record(expected_record.product)
            store.close()

        self.assertEqual(sorted(statuses), ["changed", "written"])
        self.assertIn(loaded_record.display_name, {"first", "second"})

    def test_product_profile_compare_and_write_reports_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_record = _product_profile_record()

            result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=expected_record,
            )
            store.close()

        self.assertEqual(result.status, "missing")

    def test_product_profile_compare_and_write_commits_idempotency_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected_record = _product_profile_record()
            replacement_record = expected_record.model_copy(
                update={
                    "updated_at": "2026-07-12T01:00:00Z",
                    "source": "service:test-compare-write",
                }
            )
            mutation = _product_profile_db_only_mutation()
            store.write_product_profile_record(expected_record)

            first_result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            replay_result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            conflicting_mutation = _product_profile_db_only_mutation(
                request_fingerprint="fingerprint-b"
            )
            conflict_result = store.compare_and_write_product_profile_record(
                expected_record=expected_record,
                replacement_record=replacement_record,
                mutation=conflicting_mutation,
            )
            stored_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(first_result.status, "written")
        self.assertEqual(replay_result.status, "replayed")
        self.assertIsNotNone(replay_result.idempotency_record)
        self.assertEqual(conflict_result.status, "idempotency_conflict")
        self.assertIsNotNone(stored_idempotency)
        assert stored_idempotency is not None
        self.assertEqual(stored_idempotency.state, "completed")
        self.assertEqual(stored_idempotency.response_trace_id, mutation.response_trace_id)
        self.assertEqual(stored_idempotency.response_payload, mutation.response_payload)
        self.assertEqual(stored_idempotency.attempt, 1)

    def test_product_profile_compare_and_write_reclaims_expired_reservation_with_db_clock(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            profile = _product_profile_record()
            mutation = _product_profile_db_only_mutation()
            clock = {"now": "2026-07-12T01:00:00Z"}
            store.write_product_profile_record(profile)
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = store.reserve_mutation(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                    request_fingerprint=mutation.request_fingerprint,
                    lease_owner="orphaned-worker",
                    lease_seconds=60,
                )
                clock["now"] = "2026-07-12T01:02:00Z"
                result = store.compare_and_write_product_profile_record(
                    expected_record=profile,
                    replacement_record=profile,
                    mutation=mutation,
                )
            stored_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(result.status, "written")
        self.assertIsNotNone(stored_record)
        assert stored_record is not None
        self.assertEqual(stored_record.state, "completed")
        self.assertEqual(stored_record.attempt, 2)
        self.assertEqual(stored_record.lease_owner, mutation.lease_owner)
        self.assertEqual(stored_record.created_at, "2026-07-12T01:00:00Z")
        self.assertEqual(stored_record.updated_at, "2026-07-12T01:02:00Z")
        self.assertEqual(stored_record.recorded_at, "2026-07-12T01:02:00Z")

    def test_product_profile_reads_migrate_legacy_alert_issue_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            record = _product_profile_record()
            store.write_product_profile_record(record)
            payload = store._payload_dict(record)
            lanes = payload["lanes"]
            assert isinstance(lanes, list)
            first_lane = lanes[0]
            assert isinstance(first_lane, dict)
            first_lane["health_monitoring"] = {
                "checks": [
                    {
                        "name": "public-ingress",
                        "kind": "public_http",
                        "enabled": True,
                        "alert_issue_url": "https://github.example.test/org/repo/issues/1",
                    }
                ]
            }
            with store._session_factory() as session:
                session.execute(
                    update(LaunchplaneProductProfileRow)
                    .where(LaunchplaneProductProfileRow.product == record.product)
                    .values(payload=payload)
                )
                session.commit()

            loaded_record = store.read_product_profile_record("sellyouroutboard")
            listed_records = store.list_product_profile_records(driver_id="generic-web")
            store.close()

        self.assertEqual(loaded_record.lanes[0].health_monitoring.checks[0].name, "public-ingress")
        self.assertEqual([record.product for record in listed_records], ["sellyouroutboard"])

    def test_product_profiles_cli_upserts_lists_and_shows_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            lanes_json = json.dumps(
                [
                    {
                        "instance": "testing",
                        "context": "sellyouroutboard-testing",
                        "base_url": "https://testing.sellyouroutboard.com",
                        "health_url": "https://testing.sellyouroutboard.com/api/health",
                    }
                ]
            )
            runner = CliRunner()

            upsert_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "upsert",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                    "--display-name",
                    "SellYourOutboard.com",
                    "--repository",
                    "cbusillo/sellyouroutboard",
                    "--image-repository",
                    "ghcr.io/cbusillo/sellyouroutboard",
                    "--runtime-port",
                    "3000",
                    "--health-path",
                    "/api/health",
                    "--lanes-json",
                    lanes_json,
                    "--preview-enabled",
                    "--preview-context",
                    "sellyouroutboard-testing",
                    "--preview-enable-label",
                    "preview",
                    "--updated-at",
                    "2026-04-30T22:00:00Z",
                    "--source-label",
                    "test",
                    "--allow-direct-db-mutation",
                ],
            )
            self.assertEqual(upsert_result.exit_code, 0, upsert_result.output)

            list_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "list",
                    "--database-url",
                    database_url,
                    "--driver-id",
                    "generic-web",
                ],
            )
            show_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "show",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                ],
            )

        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertIn('"product": "sellyouroutboard"', list_result.output)
        self.assertEqual(show_result.exit_code, 0, show_result.output)
        self.assertIn('"preview_context": "sellyouroutboard-testing"', list_result.output)
        self.assertIn('"preview_enable_label": "preview"', list_result.output)
        self.assertIn('"enable_label": "preview"', show_result.output)
        self.assertIn('"health_path": "/api/health"', show_result.output)

    def test_product_profiles_upsert_requires_direct_db_acknowledgement(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "product-profiles",
                "upsert",
                "--database-url",
                "postgresql://launchplane:test@db/launchplane",
                "--product",
                "sellyouroutboard",
                "--display-name",
                "SellYourOutboard.com",
                "--repository",
                "cbusillo/sellyouroutboard",
                "--image-repository",
                "ghcr.io/cbusillo/sellyouroutboard",
                "--runtime-port",
                "3000",
                "--health-path",
                "/api/health",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Direct local DB mutation is restricted", result.output)
        self.assertIn("--allow-direct-db-mutation", result.output)

    def test_preview_lifecycle_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-122", "pr-123"),
                    keep_slugs=("pr-123",),
                    orphaned_slugs=("pr-122",),
                )
            )
            listed_records = store.list_preview_lifecycle_plan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].orphaned_slugs, ("pr-122",))

    def test_preview_desired_state_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:00Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            listed_records = store.list_preview_desired_state_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].desired_state_id,
            "preview-desired-state-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].desired_count, 1)
        self.assertEqual(listed_records[0].desired_previews[0].preview_slug, "pr-123")

    def test_preview_lifecycle_cleanup_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    apply=True,
                    status="pass",
                    planned_slugs=("pr-122",),
                    destroyed_slugs=("pr-122",),
                    results=(
                        PreviewLifecycleCleanupResult(
                            preview_slug="pr-122",
                            anchor_repo="verireel",
                            anchor_pr_number=122,
                            status="destroyed",
                            application_name="ver-preview-pr-122-app",
                            application_id="app-122",
                            preview_url="https://pr-122.preview.example",
                        ),
                    ),
                )
            )
            listed_records = store.list_preview_lifecycle_cleanup_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].cleanup_id,
            "preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].destroyed_slugs, ("pr-122",))
        self.assertEqual(listed_records[0].results[0].application_id, "app-122")

    def test_runner_host_hygiene_audit_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing"
            )
            newer_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing"
            )
            failed_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-25/chris-testing",
                status="failed",
                message="post-apply evidence reported low disk",
            )

            store.write_runner_host_hygiene_audit_record(older_record)
            store.write_runner_host_hygiene_audit_record(newer_record)
            store.write_runner_host_hygiene_audit_record(failed_record)
            planned_records = store.list_runner_host_hygiene_audit_records(
                host_name="Chris-Testing",
                action="prune_docker_cache",
                status="planned",
            )
            nonmatching_action_records = store.list_runner_host_hygiene_audit_records(
                action="prune_dangling_images"
            )
            limited_records = store.list_runner_host_hygiene_audit_records(limit=1)
            read_record = store.read_runner_host_hygiene_audit_record(newer_record.audit_record_key)
            store.close()

        self.assertEqual(
            [record.audit_record_key for record in planned_records],
            [newer_record.audit_record_key, older_record.audit_record_key],
        )
        self.assertEqual(
            [record.audit_record_key for record in limited_records],
            [failed_record.audit_record_key],
        )
        self.assertEqual(nonmatching_action_records, ())
        self.assertEqual(read_record, newer_record)
        self.assertEqual(limited_records[0].message, "post-apply evidence reported low disk")

    def test_runner_host_hygiene_audit_record_with_overlong_key_round_trips(self) -> None:
        audit_record_key = "runner-host-hygiene/" + ("a" * 300)
        record = _runner_host_hygiene_audit_record(audit_record_key=audit_record_key)
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            store.write_runner_host_hygiene_audit_record(record)
            read_record = store.read_runner_host_hygiene_audit_record(audit_record_key)
            store.close()

        self.assertEqual(read_record, record)

    def test_runner_lane_registration_audit_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-08/cm-website/old",
                host_name="Chris-Testing",
            )
            newer_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-09/cm-website/new"
            )
            failed_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-10/cm-website/failed",
                status="failed",
                message="post-registration inventory did not show the lane",
            )

            store.write_runner_lane_registration_audit_record(older_record)
            store.write_runner_lane_registration_audit_record(newer_record)
            store.write_runner_lane_registration_audit_record(failed_record)
            planned_records = store.list_runner_lane_registration_audit_records(
                repository="CBUSILLO/ODOO-TENANT-CM-WEBSITE",
                host_name="Chris-Testing",
                status="planned",
            )
            limited_records = store.list_runner_lane_registration_audit_records(limit=1)
            store.close()

        self.assertEqual(
            [record.audit_record_key for record in planned_records],
            [newer_record.audit_record_key, older_record.audit_record_key],
        )
        self.assertEqual(
            [record.audit_record_key for record in limited_records],
            [failed_record.audit_record_key],
        )
        self.assertEqual(
            limited_records[0].message,
            "post-registration inventory did not show the lane",
        )

    def test_preview_pr_feedback_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    preview_url="https://pr-123.preview.example",
                    immutable_image_reference="ghcr.io/every/verireel:pr-123-a1b2c3d4",
                    refresh_image_reference="ghcr.io/every/verireel:preview-pr-123",
                    revision="a1b2c3d4",
                    run_url="https://github.com/every/verireel/actions/runs/123",
                    delivery_status="delivered",
                    delivery_action="updated_comment",
                    comment_id=456,
                    comment_url="https://github.com/every/verireel/pull/123#issuecomment-456",
                )
            )
            listed_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].feedback_id,
            "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
        )
        self.assertEqual(listed_records[0].delivery_status, "delivered")
        self.assertEqual(listed_records[0].comment_id, 456)

    def test_every_code_preview_gate_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_every_code_preview_gate_record(
                EveryCodePreviewGateRecord(
                    gate_id="every-code-preview-gate-cbusillo-code-26-abcdef",
                    request_id="every-code-cbusillo-code-123-test",
                    repository="cbusillo/code",
                    issue_number=123,
                    issue_url="https://github.com/cbusillo/code/issues/123",
                    issue_author="octocat",
                    pr_number=26,
                    pr_url="https://github.com/cbusillo/code/pull/26",
                    head_sha="abcdef1234567890",
                    status="blocked",
                    created_at="2026-05-06T17:00:00Z",
                    updated_at="2026-05-06T18:00:00Z",
                    blocked_at="2026-05-06T18:00:00Z",
                    blocked_reason="Checks did not pass: static_checks",
                )
            )
            listed_records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                status="blocked",
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].gate_id,
            "every-code-preview-gate-cbusillo-code-26-abcdef",
        )
        self.assertEqual(listed_records[0].head_sha, "abcdef1234567890")
        self.assertEqual(listed_records[0].blocked_reason, "Checks did not pass: static_checks")

    def test_agent_write_intent_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _agent_write_intent_record()
            store.write_agent_write_intent_record(record)
            loaded_record = store.read_agent_write_intent_record(record.record_id)
            listed_records = store.list_agent_write_intent_records(
                status="allowed",
                product="launchplane",
                context_name="launchplane",
            )
            store.close()

        self.assertEqual(loaded_record.record_id, record.record_id)
        self.assertEqual(loaded_record.evaluation.intent, "every_code_rerun")
        self.assertEqual(len(listed_records), 1)
        self.assertEqual(listed_records[0].trace_id, "launchplane_req_test_write_intent")
        self.assertEqual(listed_records[0].evaluation.audit.reason_code, "authorized")

    def test_merge_train_run_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _merge_train_run_record()
            store.write_merge_train_run_record(record)
            store.write_merge_train_run_record(
                _merge_train_run_record(recorded_at="2026-05-09T02:06:00Z")
            )
            loaded_record = store.read_merge_train_run_record(record.run_id)
            latest_record = store.latest_merge_train_run_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
            )
            listed_records = store.list_merge_train_run_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                mode="dry_run",
                status="merge",
            )
            store.close()

        self.assertEqual(loaded_record.run_id, record.run_id)
        self.assertEqual(loaded_record.selected_pr_number, 42)
        self.assertEqual(loaded_record.selected_head_sha, "head-42")
        self.assertEqual(loaded_record.policy_key, "cbusillo/sellyouroutboard:main")
        self.assertEqual(latest_record.recorded_at if latest_record else "", "2026-05-09T02:06:00Z")
        self.assertEqual(len(listed_records), 2)
        self.assertEqual(listed_records[0].recorded_at, "2026-05-09T02:06:00Z")

    def test_merge_train_policy_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            policy = build_test_merge_train_policy_with_codex_skills()
            older_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T200000Z-old",
                status="superseded",
                source="test",
                updated_at="2026-05-13T20:00:00Z",
                policy=policy,
            )
            active_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T210000Z-active",
                status="active",
                source="test",
                updated_at="2026-05-13T21:00:00Z",
                policy=policy,
            )
            store.write_merge_train_policy_record(older_record)
            store.write_merge_train_policy_record(active_record)
            listed_records = store.list_merge_train_policy_records(status="active")
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].policy_sha256, policy.policy_sha256)
        self.assertEqual(
            listed_records[0]
            .policy.find_repository_policy(repository="cbusillo/codex-skills", base_branch="main")
            .blocked_label,
            "merge-blocked",
        )

    def test_merge_train_batch_candidate_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_batch_candidate_record(
                record_id="merge-train-batch-candidate-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_candidate_record()
            store.write_merge_train_batch_candidate_record(older_record)
            store.write_merge_train_batch_candidate_record(active_record)
            listed_records = store.list_merge_train_batch_candidate_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].candidate.entries[1].pull_request_number, 11)
        self.assertEqual(listed_records[0].candidate.required_checks_status, "pass")

    def test_merge_train_controller_state_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            idle_record = _merge_train_controller_state_record(
                updated_at="2026-05-14T00:55:00Z",
                status="idle",
            )
            active_record = _merge_train_controller_state_record()
            store.write_merge_train_controller_state_record(idle_record)
            store.write_merge_train_controller_state_record(active_record)
            listed_records = store.list_merge_train_controller_state_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="running",
            )
            loaded_record = store.read_merge_train_controller_state_record(
                active_record.controller_key
            )
            store.close()

        self.assertEqual(
            [record.controller_key for record in listed_records],
            [active_record.controller_key],
        )
        self.assertEqual(loaded_record.active_phase, "cleanup_candidate_ref")
        self.assertEqual(loaded_record.lease_owner, active_record.lease_owner)
        self.assertEqual(
            loaded_record.step_payload["candidate_ref"],
            "refs/heads/launchplane/train/example/merge-train-repo/main/batch-1",
        )

    def test_merge_train_batch_landing_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_batch_landing_plan_record(
                record_id="merge-train-batch-landing-plan-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_landing_plan_record()
            store.write_merge_train_batch_landing_plan_record(older_record)
            store.write_merge_train_batch_landing_plan_record(active_record)
            listed_records = store.list_merge_train_batch_landing_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].landing_plan.entries[0].merge_method, "squash")
        self.assertEqual(listed_records[0].landing_plan.entries[1].pull_request_number, 11)

    def test_merge_train_stack_collapse_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_stack_collapse_plan_record(
                record_id="merge-train-stack-collapse-plan-20260514T012900Z-old",
                status="superseded",
                updated_at="2026-05-14T01:29:00Z",
            )
            active_record = _merge_train_stack_collapse_plan_record()
            store.write_merge_train_stack_collapse_plan_record(older_record)
            store.write_merge_train_stack_collapse_plan_record(active_record)
            listed_records = store.list_merge_train_stack_collapse_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].plan.intent_source, "root_ready_to_merge")
        self.assertEqual(listed_records[0].plan.mutations[0].parent_pull_request_number, 10)

    def test_write_and_list_dokploy_target_id_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="prod", target_id="compose-123")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="cm", instance="testing", target_id="compose-456")
            )
            loaded_record = store.read_dokploy_target_id_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_dokploy_target_id_records()
            store.close()

        self.assertEqual(loaded_record.target_id, "compose-123")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_write_read_and_list_edge_endpoint_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_edge_endpoint_record(
                _edge_endpoint_record(endpoint_key="disabled-edge", status="disabled")
            )
            loaded_record = store.read_edge_endpoint_record("cm-prod-dokploy")
            active_records = store.list_edge_endpoint_records(provider="dokploy", status="active")
            store.close()

        self.assertEqual(loaded_record.server_name, "docker-cm-prod")
        self.assertEqual(loaded_record.upstream_host, "100.73.170.113")
        self.assertEqual(loaded_record.upstream_scheme, "https")
        self.assertEqual(loaded_record.upstream_port, 443)
        self.assertEqual([record.endpoint_key for record in active_records], ["cm-prod-dokploy"])

    def test_write_read_and_list_private_health_endpoint_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_private_health_endpoint_record(_private_health_endpoint_record())
            store.write_private_health_endpoint_record(
                _private_health_endpoint_record(
                    endpoint_key="disabled-runtime",
                    status="disabled",
                )
            )
            loaded_record = store.read_private_health_endpoint_record(
                "repairshopr-sync-prod-runtime"
            )
            active_records = store.list_private_health_endpoint_records(
                product="repairshopr-sync",
                context_name="repairshopr-sync",
                instance_name="prod",
                status="active",
            )
            store.close()

        self.assertEqual(loaded_record.product, "repairshopr-sync")
        self.assertEqual(loaded_record.context, "repairshopr-sync")
        self.assertEqual(loaded_record.instance, "prod")
        self.assertEqual(loaded_record.url, "http://10.0.0.5:8000/health")
        self.assertEqual(
            [record.endpoint_key for record in active_records],
            ["repairshopr-sync-prod-runtime"],
        )

    def test_edge_endpoint_record_rejects_hostname_upstream(self) -> None:
        with self.assertRaises(ValueError):
            _edge_endpoint_record(upstream_host="dokploy.shiny")

    def test_write_and_list_runtime_environment_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global", env={"ODOO_MASTER_PASSWORD": "shared-master"}
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="local",
                    env={"ODOO_DB_PASSWORD": "local-secret"},
                )
            )
            listed_records = store.list_runtime_environment_records()
            store.close()

        self.assertEqual(
            [(record.scope, record.context, record.instance) for record in listed_records],
            [("global", "", ""), ("instance", "opw", "local")],
        )

    def test_read_lane_summary_uses_repository_queries_for_gui_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_artifact_manifest(_artifact_manifest())
            store.write_environment_inventory(_inventory_record())
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            store.write_release_tuple_record(_release_tuple_record())
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="testing")
            )
            store.write_dokploy_target_record(
                _dokploy_target_record(context="opw", instance="testing")
            )
            store.write_provider_target_record(
                _provider_target_record(
                    context="opw",
                    instance="testing",
                    target_category="compose",
                    target_id="compose-123",
                    provider_target_type="compose",
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global",
                    env={"ODOO_MASTER_PASSWORD": "shared-master"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="context",
                    context="opw",
                    env={"ODOO_DB_USER": "opw"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="testing",
                    env={"ODOO_DB_NAME": "opw-testing"},
                )
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="testing")
            )
            store.write_secret_binding(
                _secret_binding(
                    binding_id="binding-dokploy-token",
                    secret_id="secret-dokploy-token",
                    updated_at="2026-04-20T18:07:00Z",
                )
            )

            summary = store.read_lane_summary(context_name="opw", instance_name="testing")
            store.close()

        self.assertEqual(summary.context, "opw")
        self.assertEqual(summary.instance, "testing")
        inventory = summary.inventory
        assert inventory is not None
        inventory_artifact_identity = inventory.artifact_identity
        assert inventory_artifact_identity is not None
        self.assertEqual(inventory_artifact_identity.artifact_id, "artifact-20260420-a1b2c3d4")
        artifact_manifest = summary.artifact_manifest
        assert artifact_manifest is not None
        self.assertEqual(artifact_manifest.artifact_id, "artifact-20260420-a1b2c3d4")
        self.assertEqual(artifact_manifest.image.repository, "ghcr.io/cbusillo/odoo-tenant-opw")
        release_tuple = summary.release_tuple
        assert release_tuple is not None
        self.assertEqual(release_tuple.channel, "testing")
        latest_deployment = summary.latest_deployment
        assert latest_deployment is not None
        self.assertEqual(latest_deployment.record_id, "deployment-20260420T153000Z-opw-testing")
        self.assertIsNone(summary.latest_promotion)
        self.assertIsNone(summary.latest_backup_gate)
        dokploy_target_id = summary.dokploy_target_id
        assert dokploy_target_id is not None
        self.assertEqual(dokploy_target_id.target_id, "compose-123")
        dokploy_target = summary.dokploy_target
        assert dokploy_target is not None
        self.assertEqual(dokploy_target.target_name, "opw-testing")
        provider_target = summary.provider_target
        assert provider_target is not None
        self.assertEqual(provider_target.provider_id, "dokploy")
        self.assertEqual(provider_target.target_category, "compose")
        self.assertEqual(provider_target.target_id, "compose-123")
        deployed_target = summary.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "dokploy")
        self.assertEqual(deployed_target.target_category, "compose")
        self.assertEqual(deployed_target.target_id, "compose-123")
        self.assertEqual(
            [
                (record.scope, record.context, record.instance)
                for record in summary.runtime_environment_records
            ],
            [("global", "", ""), ("context", "opw", ""), ("instance", "opw", "testing")],
        )
        odoo_instance_override = summary.odoo_instance_override
        assert odoo_instance_override is not None
        self.assertEqual(odoo_instance_override.config_parameters[0].key, "web.base.url")
        self.assertEqual(summary.secret_bindings[0].binding_key, "DOKPLOY_TOKEN")

    def test_write_read_and_list_odoo_instance_override_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="prod")
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="cm", instance="testing")
            )
            loaded_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_odoo_instance_override_records()
            store.close()

        self.assertEqual(loaded_record.config_parameters[0].key, "web.base.url")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_secret_records_round_trip_and_find_latest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            older_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:05:00Z",
                current_version_id="secret-version-0001",
            )
            newer_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:07:00Z",
                current_version_id="secret-version-0002",
            )
            older_version = _secret_version(
                version_id="secret-version-0001",
                secret_id=older_record.secret_id,
                created_at="2026-04-20T18:05:00Z",
            )
            newer_version = _secret_version(
                version_id="secret-version-0002",
                secret_id=newer_record.secret_id,
                created_at="2026-04-20T18:07:00Z",
            )
            binding = _secret_binding(
                binding_id="binding-dokploy-token",
                secret_id=newer_record.secret_id,
                updated_at="2026-04-20T18:07:00Z",
            )
            audit_event = _secret_audit_event(
                event_id="audit-secret-import-0001",
                secret_id=newer_record.secret_id,
                recorded_at="2026-04-20T18:07:30Z",
            )

            store.write_secret_record(older_record)
            store.write_secret_version(older_version)
            store.write_secret_record(newer_record)
            store.write_secret_version(newer_version)
            store.write_secret_binding(binding)
            store.write_secret_audit_event(audit_event)

            found_record = store.find_secret_record(
                scope="context_instance",
                integration="dokploy",
                name="api_token",
                context="opw",
                instance="testing",
            )
            listed_records = store.list_secret_records(
                integration="dokploy", context_name="opw", instance_name="testing"
            )
            listed_versions = store.list_secret_versions(secret_id=newer_record.secret_id)
            listed_bindings = store.list_secret_bindings(
                integration="dokploy",
                context_name="opw",
                instance_name="testing",
            )
            listed_events = store.list_secret_audit_events(secret_id=newer_record.secret_id)
            self.assertIsNotNone(found_record)
            assert found_record is not None
            self.assertEqual(found_record.secret_id, newer_record.secret_id)
            self.assertEqual(
                store.read_secret_record(newer_record.secret_id).current_version_id,
                "secret-version-0002",
            )
            self.assertEqual(
                store.read_secret_version("secret-version-0002").secret_id, newer_record.secret_id
            )
            self.assertEqual(
                [record.secret_id for record in listed_records], [newer_record.secret_id]
            )
            self.assertEqual(
                [version.version_id for version in listed_versions],
                ["secret-version-0002", "secret-version-0001"],
            )
            self.assertEqual(
                [item.binding_id for item in listed_bindings], ["binding-dokploy-token"]
            )
            self.assertEqual(
                [item.event_id for item in listed_events], ["audit-secret-import-0001"]
            )
            store.close()

    def test_import_core_records_from_filesystem(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            filesystem_store = FilesystemRecordStore(
                state_dir=Path(temporary_directory_name) / "state"
            )
            filesystem_store.write_backup_gate_record(_backup_gate_record())
            filesystem_store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            filesystem_store.write_promotion_record(
                _promotion_record(record_id="promotion-20260420T160500Z-opw-testing-to-prod")
            )
            filesystem_store.write_environment_inventory(_inventory_record())
            filesystem_store.write_artifact_manifest(_artifact_manifest())
            filesystem_store.write_odoo_instance_override_record(_odoo_instance_override_record())
            filesystem_store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    updated_at="2026-04-20T10:05:00Z",
                    pr_number=123,
                )
            )
            filesystem_store.write_preview_enablement_record(
                _preview_enablement_record(
                    record_id="verireel-testing-verireel-pr-123",
                    updated_at="2026-04-20T10:05:30Z",
                    pr_number=123,
                )
            )
            filesystem_store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                )
            )
            filesystem_store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=1,
                    preview_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100550Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:50Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            filesystem_store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:06:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-123",),
                    keep_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:07:00Z",
                    source="preview-janitor",
                    apply=False,
                    status="report_only",
                )
            )
            filesystem_store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    delivery_status="skipped",
                    error_message="Launchplane runtime records do not expose GITHUB_TOKEN for this context",
                )
            )
            filesystem_store.write_runner_host_hygiene_audit_record(
                _runner_host_hygiene_audit_record(
                    audit_record_key="runner-host-hygiene/2026-05-23/chris-testing"
                )
            )
            filesystem_store.write_runner_lane_registration_audit_record(
                _runner_lane_registration_audit_record(
                    audit_record_key="runner-lane-registration/2026-06-08/cm-website/import"
                )
            )
            filesystem_store.write_release_tuple_record(_release_tuple_record())
            active_profile = _product_profile_record()
            filesystem_store.write_product_profile_record(
                active_profile.model_copy(
                    update={
                        "lifecycle_state": "retiring",
                        "preview": active_profile.preview.model_copy(update={"enabled": False}),
                    }
                )
            )
            filesystem_store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="runtime-key-safety-policy-20260505T200000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-05T20:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="SHOPIFY_ACCESS_TOKEN",
                            secret_class="testing",
                        ),
                    ),
                )
            )
            classification_revision_1 = _tenant_repository_classification_record()
            classification_revision_2 = _tenant_repository_classification_record(
                classification_kind="engineering",
                classification_revision=2,
                classified_at="2026-07-31T10:05:00Z",
                supersedes_record_id=classification_revision_1.record_id,
            )
            filesystem_store.write_tenant_repository_classification_record(
                classification_revision_1
            )
            filesystem_store.write_tenant_repository_classification_record(
                classification_revision_2
            )
            filesystem_store.write_merge_train_policy_record(
                MergeTrainPolicyRecord(
                    record_id="merge-train-policy-20260513T210000Z-active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                )
            )
            filesystem_store.write_merge_train_run_record(_merge_train_run_record())
            filesystem_store.write_merge_train_batch_candidate_record(
                _merge_train_batch_candidate_record()
            )
            filesystem_store.write_merge_train_batch_landing_plan_record(
                _merge_train_batch_landing_plan_record()
            )
            filesystem_store.write_merge_train_stack_collapse_plan_record(
                _merge_train_stack_collapse_plan_record()
            )
            filesystem_store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        "operation_id": "odoo-stable-bootstrap-cm-testing-test",
                        "product": "odoo-tenant-cm",
                        "context": "cm",
                        "instance": "testing",
                        "idempotency_key": "bootstrap-cm-testing",
                        "request_fingerprint": "fingerprint-123",
                        "request": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "context": "cm",
                            "instance": "testing",
                            "confirmation": "bootstrap cm testing",
                        },
                        "status": "pending",
                        "phase": "created",
                        "created_at": "2026-05-17T00:00:00Z",
                        "updated_at": "2026-05-17T00:00:00Z",
                    }
                )
            )
            filesystem_store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        "operation_id": "odoo-target-replacement-cm-testing-test",
                        "product": "odoo-tenant-cm",
                        "context": "cm",
                        "instance": "testing",
                        "idempotency_key": "replacement-cm-testing",
                        "request_fingerprint": "fingerprint-123",
                        "request": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "instance": "testing",
                            "strategy": "recreate-in-place",
                            "allow_empty_data": False,
                        },
                        "status": "pending",
                        "phase": "created",
                        "created_at": "2026-05-17T00:00:00Z",
                        "updated_at": "2026-05-17T00:00:00Z",
                    }
                )
            )

            counts = store.import_core_records_from_filesystem(filesystem_store)
            self.assertEqual(
                counts,
                {
                    "artifacts": 1,
                    "authz_policies": 0,
                    "backup_gates": 1,
                    "deployments": 1,
                    "promotions": 1,
                    "inventory": 1,
                    "odoo_instance_overrides": 1,
                    "product_profiles": 1,
                    "change_impact_policies": 0,
                    "product_owner_policies": 0,
                    "product_owner_requirements": 0,
                    "product_owner_routing": 0,
                    "preview_records": 1,
                    "preview_enablement": 1,
                    "preview_generations": 1,
                    "manager_preview_approval_events": 0,
                    "owner_acceptance_events": 0,
                    "privileged_operation_events": 0,
                    "privileged_operations": 0,
                    "preview_desired_states": 1,
                    "preview_inventory_scans": 1,
                    "preview_lifecycle_cleanups": 1,
                    "preview_lifecycle_plans": 1,
                    "preview_pr_feedback": 1,
                    "runner_host_hygiene_audits": 1,
                    "runner_lane_registration_audits": 1,
                    "every_code_preview_gates": 0,
                    "agent_write_intents": 0,
                    "merge_train_pr_feedback": 0,
                    "merge_train_batch_candidates": 1,
                    "merge_train_controller_states": 0,
                    "odoo_prod_backup_restore_operations": 0,
                    "odoo_prod_retained_volume_backup_import_operations": 0,
                    "merge_train_batch_landing_plans": 1,
                    "merge_admissions": 0,
                    "merge_landing_outcomes": 0,
                    "merge_train_stack_collapse_plans": 1,
                    "merge_train_policies": 1,
                    "merge_train_runs": 1,
                    "odoo_stable_bootstrap_operations": 1,
                    "odoo_stable_target_replacement_operations": 1,
                    "release_tuples": 1,
                    "runtime_key_safety_policies": 1,
                    "tenant_repository_classifications": 2,
                },
            )
            self.assertEqual(
                store.latest_tenant_repository_classification_lookup(repository_id="1001")
                .records[0]
                .classification_kind,
                "engineering",
            )
            self.assertEqual(
                len(store.list_tenant_repository_classification_records(repository_id="1001")),
                2,
            )
            self.assertEqual(
                store.read_promotion_record(
                    "promotion-20260420T160500Z-opw-testing-to-prod"
                ).to_instance,
                "prod",
            )
            self.assertEqual(
                store.read_preview_generation_record(
                    "preview-verireel-testing-verireel-pr-123-generation-0001"
                ).state,
                "ready",
            )
            self.assertEqual(
                store.read_preview_enablement_record(
                    "verireel-testing-verireel-pr-123"
                ).request_metadata_baseline_channel,
                "testing",
            )
            self.assertEqual(
                store.list_preview_inventory_scan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].scan_id,
                "preview-inventory-scan-verireel-testing-20260420T100500Z",
            )
            self.assertEqual(
                store.list_preview_desired_state_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].desired_state_id,
                "preview-desired-state-verireel-testing-20260420T100550Z",
            )
            self.assertEqual(
                store.list_odoo_stable_bootstrap_operation_records(
                    context_name="cm",
                    instance_name="testing",
                    statuses=("pending",),
                    limit=1,
                )[0].operation_id,
                "odoo-stable-bootstrap-cm-testing-test",
            )
            self.assertEqual(
                store.list_odoo_stable_target_replacement_operation_records(
                    context_name="cm",
                    instance_name="testing",
                    statuses=("pending",),
                    limit=1,
                )[0].operation_id,
                "odoo-target-replacement-cm-testing-test",
            )
            self.assertEqual(
                store.list_preview_lifecycle_plan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].plan_id,
                "preview-lifecycle-plan-verireel-testing-20260420T100600Z",
            )
            self.assertEqual(
                store.list_preview_lifecycle_cleanup_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].cleanup_id,
                "preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
            )
            self.assertEqual(
                store.list_preview_pr_feedback_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].feedback_id,
                "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
            )
            self.assertEqual(
                store.list_runner_host_hygiene_audit_records(
                    host_name="chris-testing",
                    limit=1,
                )[0].audit_record_key,
                "runner-host-hygiene/2026-05-23/chris-testing",
            )
            self.assertEqual(
                store.list_runner_lane_registration_audit_records(
                    repository="cbusillo/odoo-tenant-cm-website",
                    limit=1,
                )[0].audit_record_key,
                "runner-lane-registration/2026-06-08/cm-website/import",
            )
            self.assertEqual(
                store.list_runtime_key_safety_policy_records(status="active", limit=1)[0]
                .rules[0]
                .binding_key,
                "SHOPIFY_ACCESS_TOKEN",
            )
            self.assertEqual(
                store.read_product_profile_record(active_profile.product).lifecycle_state,
                "retiring",
            )
            store.close()
