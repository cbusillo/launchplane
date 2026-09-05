from __future__ import annotations

import base64
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from control_plane import authz_grant_service, authz_policy_activation
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
    build_launchplane_mutation_reservation,
    complete_launchplane_mutation_reservation,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
)
from control_plane.contracts.manager_preview_approval import (
    ManagerPreviewApprovalAuthorization,
    ManagerPreviewApprovalBinding,
    ManagerPreviewApprovalEventRecord,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceContributionBinding,
    OwnerAcceptancePolicyFingerprintBinding,
    OwnerAcceptancePreviewIsolationBinding,
    OwnerAcceptanceReviewContext,
    OwnerAcceptanceAuthorization,
    OwnerAcceptanceBinding,
    OwnerAcceptanceEventRecord,
    OwnerAcceptancePreviewBinding,
    OwnerAcceptanceTransitionError,
    owner_acceptance_runtime_identity_binding,
)
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChannelBindingRecord,
    ChallengeResponse,
    OwnerControlConfirmationEnvelope,
    owner_control_approval_request_digest,
    owner_control_channel_binding_sha256,
    owner_control_signature_payload_bytes,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OwnerControlChallengeIssueRequest,
    OwnerControlIssuedChallengeRecord,
)
from control_plane.contracts.owner_control_enrollment_provenance import (
    OwnerControlHostPrincipalClaim,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    OdooStableBootstrapOperationPhase,
    OdooStableBootstrapOperationStatus,
)
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_delivery_id,
    build_outbox_dedupe_key,
)
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
)
from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionHumanEvidence,
    ManagedSecretReencryptionPlanInput,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PrivilegedOperationActor,
    PrivilegedOperationEventRecord,
    PrivilegedOperationRecord,
    privileged_operation_evidence_digest,
    privileged_operation_record_digest,
    privileged_operation_request_digest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
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
from control_plane.manager_preview_approval import ManagerPreviewApprovalEventConflictError
from control_plane.contracts.product_owner import (
    PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS,
)
from control_plane.owner_acceptance import OwnerAcceptanceEventConflictError
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyConflictError,
    TenantTechnicalHumanWaiverApplyEnvelope,
    TenantTechnicalHumanWaiverExpectedAuthority,
    TenantTechnicalHumanWaiverRevokeCurrentError,
    TenantTechnicalHumanWaiverEventConflictError,
    TenantTechnicalHumanWaiverStaleAuthorityError,
)
from control_plane.repository_inventory import RepositoryInventoryConflictError
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    ProviderTargetSupersession,
    run_durable_provider_operation,
)
from control_plane.privileged_operation_worker import (
    execute_approved_privileged_operations_once,
)
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from control_plane.odoo_stable_lane import (
    OdooStableLaneOperationConflictError,
    OdooStableLaneOperationRecord,
)
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalAdminPolicyRule,
)
from control_plane.storage.postgres import (
    DbOnlyMutationRequest,
    LaunchplaneEveryCodeWorkRequestRow,
    LaunchplaneOwnerControlIssuedChallengeRow,
    MutationReservationResult,
    OutboxWithIdempotencyRequest,
    PostgresRecordStore,
)
from control_plane.storage.factory import build_privileged_operation_worker_store
from tests.support.durable_operations import durable_operation_cancellation_payload
from tests.test_product_retirement import _Store as _RetirementStore
from tests.test_product_retirement import _observation as _retirement_observation
from tests.test_product_retirement import _plan as _retirement_plan
from tests.test_detached_application_retirement import (
    _plan as _detached_application_retirement_plan,
)
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    EXPECTED_ALEMBIC_HEAD_REVISION,
    verify_postgres_schema_invariants,
)
from control_plane.storage.schema_migration import migrate_schema, schema_migration_action
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationConflictError,
)
from control_plane.trusted_maintenance import (
    TrustedMaintenanceEvidenceConflictError,
    TrustedMaintenanceExpectedAuthority,
    TrustedMaintenanceGitHubEventFacts,
)
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_odoo_prod_retained_volume_backup_import_storage import (
    _retained_operation_for_restore_lane,
)
from tests.test_odoo_stable_operation_worker import _restore_operation
from tests.test_production_backup_authority import _dry_run_envelope, _source_target
from tests.test_trusted_maintenance import (
    _candidate as _trusted_maintenance_candidate,
    _classification as _trusted_maintenance_classification,
    _event_facts as _trusted_maintenance_event_facts,
    _policy as _trusted_maintenance_policy,
)

POSTGRES_TEST_URL_ENV = "LAUNCHPLANE_TEST_POSTGRES_URL"
LOCK_WAIT_TIMEOUT = "1000ms"


def _postgres_root_database_url() -> str:
    database_url = os.environ.get(POSTGRES_TEST_URL_ENV, "").strip()
    if not database_url:
        raise unittest.SkipTest(f"{POSTGRES_TEST_URL_ENV} is not set")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise unittest.SkipTest(f"{POSTGRES_TEST_URL_ENV} must use postgresql+psycopg")
    return database_url


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@contextmanager
def _isolated_postgres_database() -> Iterator[str]:
    root_database_url = _postgres_root_database_url()
    root_url = make_url(root_database_url)
    database_name = f"launchplane_test_{uuid4().hex}"
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    root_engine = create_engine(root_database_url, isolation_level="AUTOCOMMIT")
    try:
        with root_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        try:
            yield database_url
        finally:
            with root_engine.connect() as connection:
                connection.execute(
                    text(
                        "select pg_terminate_backend(pid) "
                        "from pg_stat_activity where datname = :database_name"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        root_engine.dispose()


def _upgrade_empty_database_to_head(database_url: str) -> None:
    alembic_command.upgrade(_alembic_config(database_url), "head")


@contextmanager
def _store_for_fresh_head_database() -> Iterator[PostgresRecordStore]:
    with _isolated_postgres_database() as database_url:
        _upgrade_empty_database_to_head(database_url)
        store = PostgresRecordStore(database_url=database_url)
        try:
            store.verify_schema()
            yield store
        finally:
            store.close()


def _tenant_repository_classification_record(
    *,
    revision: int,
    product: str = "postgres-tenant-site",
    context: str = "postgres-tenant-site",
    classification_kind: str = "tenant_ui",
    classified_at: str = "2026-07-31T10:00:00Z",
    reason: str = "postgres integration classification",
    supersedes_record_id: str | None = None,
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord.model_validate(
        {
            "repository_id": "901001",
            "repository_owner_id": "902001",
            "repository": "example/postgres-tenant-site",
            "product": product,
            "context": context,
            "classification_kind": classification_kind,
            "classification_revision": revision,
            "classified_at": classified_at,
            "source": "postgres-integration",
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _repository_inventory_record(
    *,
    revision: int,
    state: str = "tracked",
    recorded_at: str = "2026-08-26T10:00:00Z",
    reason: str = "postgres integration inventory",
    supersedes_record_id: str | None = None,
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord.model_validate(
        {
            "repository_id": "911001",
            "repository_owner_id": "912001",
            "repository": "example/postgres-inventory",
            "inventory_state": state,
            "inventory_revision": revision,
            "recorded_at": recorded_at,
            "source": "postgres-integration",
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _repository_human_role_policy_record(
    *,
    revision: int,
    repository_owner_github_ids: tuple[int, ...] = (903001,),
    effective_at: str = "2026-08-01T10:00:00Z",
    reason: str = "postgres integration role policy",
    supersedes_record_id: str | None = None,
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord.model_validate(
        {
            "repository_id": "901001",
            "repository_owner_id": "902001",
            "repository": "example/postgres-tenant-site",
            "product": "postgres-tenant-site",
            "context": "postgres-tenant-site",
            "role_policy_revision": revision,
            "repository_owner_github_ids": repository_owner_github_ids,
            "manager_primary_github_ids": (904001,),
            "effective_at": effective_at,
            "source": "postgres-integration",
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _tenant_technical_human_waiver_event_record(
    *,
    source_event_id: str = "comment-1001",
    action: str = "created",
    occurred_at: str = "2026-08-01T10:15:00Z",
    expires_at: str = "2026-08-01T11:15:00Z",
) -> TenantTechnicalHumanWaiverEventRecord:
    repository_id = "901001"
    repository_owner_id = "902001"
    repository = "example/postgres-tenant-site"
    product = "postgres-tenant-site"
    context = "postgres-tenant-site"
    role_policy_record_id = "repository-human-role-policy-901001-abc123-r1"
    role_policy_digest = "c" * 64
    authz_policy_digest = "d" * 64
    binding = TenantTechnicalHumanWaiverBinding(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
        pull_request_number=42,
        head_sha="a" * 40,
        classification_revision=1,
        classification_digest="b" * 64,
        role_policy_record_id=role_policy_record_id,
        role_policy_revision=1,
        role_policy_digest=role_policy_digest,
        authz_policy_record_id="authz-policy-r1",
        authz_policy_revision=1,
        authz_policy_digest=authz_policy_digest,
    )
    authorization = TenantTechnicalHumanWaiverAuthorization(
        author_github_id=903001,
        author_login="postgres-human",
        managed_set_id="tenant-human.postgres",
        managed_rule_id="technical-waiver",
        authz_policy_record_id="authz-policy-r1",
        authz_policy_revision=1,
        authz_policy_digest=authz_policy_digest,
        authz_policy_source="postgres-integration-authz",
        role_policy_provenance=RepositoryHumanRolePolicyProvenance(
            repository_id=repository_id,
            repository_owner_id=repository_owner_id,
            repository=repository,
            product=product,
            context=context,
            role_policy_record_id=role_policy_record_id,
            role_policy_revision=1,
            role_policy_digest=role_policy_digest,
            role_policy_source="postgres-integration",
            authority_kind="repository_owner",
            evaluated_at=occurred_at,
        ),
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


def _tenant_technical_human_waiver_authz_policy_record(
    *,
    github_ids: tuple[int, ...] = (903001,),
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.postgres",
                managed_rule_id="technical-waiver",
                github_ids=github_ids,
                roles=("read_only",),
                products=("postgres-tenant-site",),
                contexts=("postgres-tenant-site",),
                actions=(TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="postgres-integration-authz",
        updated_at="2026-07-01T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _tenant_technical_human_waiver_identity(
    *,
    github_id: int = 903001,
    login: str = "postgres-human",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Postgres Human",
        email="postgres-human@example.test",
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
    classification_record = classification or _tenant_repository_classification_record(
        revision=1,
        classified_at="2026-07-01T00:00:00Z",
    )
    role_policy_record = role_policy or _repository_human_role_policy_record(
        revision=1,
        effective_at="2026-07-01T00:00:00Z",
    )
    authz_policy_record = authz_policy or _tenant_technical_human_waiver_authz_policy_record()
    store.write_tenant_repository_classification_record(classification_record)
    store.write_repository_human_role_policy_record(role_policy_record)
    store.seed_authz_policy_if_absent(authz_policy_record)
    return classification_record, role_policy_record, authz_policy_record


def _tenant_technical_human_waiver_envelope(
    *,
    classification: TenantRepositoryClassificationRecord,
    role_policy: RepositoryHumanRolePolicyRecord,
    authz_policy: LaunchplaneAuthzPolicyRecord,
    action: str = "created",
    source_event_id: str = "comment-waiver-create",
    expected_current: dict[str, object] | None = None,
    reason: str = "Owner reviewed exact technical waiver.",
) -> TenantTechnicalHumanWaiverApplyEnvelope:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "apply",
        "action": action,
        "candidate": {
            "product": "postgres-tenant-site",
            "context": "postgres-tenant-site",
            "repository_id": "901001",
            "repository_owner_id": "902001",
            "repository": "example/postgres-tenant-site",
            "pull_request_number": 42,
            "head_sha": "a" * 40,
        },
        "expected_authority": TenantTechnicalHumanWaiverExpectedAuthority(
            classification_record_id=classification.record_id,
            classification_digest=classification.classification_digest,
            role_policy_record_id=role_policy.record_id,
            role_policy_digest=role_policy.role_policy_digest,
            authz_policy_record_id=authz_policy.record_id,
            authz_policy_digest=authz_policy.policy_sha256,
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
    idempotency_key: str,
    request_fingerprint: str,
    response_trace_id: str,
    scope: str = "github-human-id|903001",
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


def _bootstrap_operation(
    *,
    operation_id: str = "odoo-stable-bootstrap-cm-testing-20260517t000000z-base",
    idempotency_key: str = "bootstrap-cm-testing",
    status: OdooStableBootstrapOperationStatus = "pending",
    phase: OdooStableBootstrapOperationPhase = "created",
    created_at: str = "2026-05-17T00:00:00Z",
    updated_at: str = "2026-05-17T00:00:00Z",
    lease_owner: str = "",
    lease_expires_at: str = "",
    heartbeat_at: str = "",
    attempt: int = 0,
    started_at: str = "",
    finished_at: str = "",
    error_message: str = "",
) -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord(
        operation_id=operation_id,
        product="odoo-tenant-cm",
        context="cm",
        instance="testing",
        idempotency_key=idempotency_key,
        request_fingerprint=f"fingerprint-{idempotency_key}",
        request=OdooStableBootstrapRequest(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            confirmation="bootstrap cm testing",
        ),
        status=status,
        phase=phase,
        created_at=created_at,
        updated_at=updated_at,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        heartbeat_at=heartbeat_at,
        attempt=attempt,
        started_at=started_at or (updated_at if status == "running" else ""),
        finished_at=finished_at,
        error_message=error_message,
    )


def _create_cross_kind_stable_lane_operation(
    *,
    database_url: str,
    operation_kind: str,
    start_barrier: threading.Barrier,
) -> tuple[str, str]:
    store = PostgresRecordStore(database_url=database_url)
    try:
        start_barrier.wait(timeout=5)
        try:
            if operation_kind == "prod_backup_restore":
                _, created = (
                    store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                        _restore_operation("operation-cm-prod-restore-concurrent")
                    )
                )
            else:
                _, created = (
                    store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                        _retained_operation_for_restore_lane()
                    )
                )
        except OdooStableLaneOperationConflictError as error:
            return "conflict", error.owner.operation_kind
        return "created" if created else "existing", operation_kind
    finally:
        store.close()


def _claim_cross_kind_stable_lane_operation(
    *,
    database_url: str,
    operation_kind: str,
    start_barrier: threading.Barrier,
) -> tuple[str, str]:
    store = PostgresRecordStore(database_url=database_url)
    try:
        start_barrier.wait(timeout=5)
        claim_kwargs = {
            "lease_owner": f"worker-{operation_kind}",
            "lease_expires_at": "2026-07-26T05:10:00Z",
            "claimed_at": "2026-07-26T05:00:00Z",
        }
        claimed: OdooStableLaneOperationRecord | None
        if operation_kind == "prod_backup_restore":
            claimed = store.claim_next_odoo_prod_backup_restore_operation_record(**claim_kwargs)
        else:
            claimed = store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
                **claim_kwargs
            )
        return operation_kind, claimed.operation_id if claimed is not None else ""
    finally:
        store.close()


def _idempotency_record(
    *, response_trace_id: str, request_fingerprint: str
) -> LaunchplaneIdempotencyRecord:
    return LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_idempotency_record_id(response_trace_id=response_trace_id),
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/evidence/previews/generations",
        idempotency_key="preview-generation:launchplane:test:1",
        request_fingerprint=request_fingerprint,
        response_status_code=202,
        response_trace_id=response_trace_id,
        recorded_at="2026-07-01T00:00:00Z",
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _every_code_work_request() -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-1693-test",
        source="manual",
        state="queued",
        repository="cbusillo/code",
        issue_number=1693,
        issue_url="https://github.com/cbusillo/code/issues/1693",
        trigger_label="every-code",
        queued_at="2026-07-13T09:00:00Z",
        updated_at="2026-07-13T09:00:00Z",
    )


class _BlockingEveryCodeFinishStore(PostgresRecordStore):
    def __init__(
        self,
        *,
        database_url: str,
        finish_row_synced: threading.Event,
        release_finish: threading.Event,
    ) -> None:
        super().__init__(database_url=database_url)
        self._finish_row_synced = finish_row_synced
        self._release_finish = release_finish

    def _sync_every_code_work_request_row(
        self,
        row: LaunchplaneEveryCodeWorkRequestRow,
        record: EveryCodeWorkRequestRecord,
    ) -> None:
        super()._sync_every_code_work_request_row(row, record)
        if record.state == "done":
            self._finish_row_synced.set()
            if not self._release_finish.wait(timeout=10):
                raise TimeoutError("timed out waiting to release Every Code finish transaction")


def _mutation_reservation(
    *,
    lease_owner: str,
    request_fingerprint: str = "mutation-fingerprint-a",
    idempotency_key: str = "product-preview-tls:postgres:1",
    lease_expires_at: str = "2026-07-13T00:05:00Z",
    reserved_at: str = "2026-07-13T00:00:00Z",
) -> LaunchplaneIdempotencyRecord:
    return build_launchplane_mutation_reservation(
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/product-profiles/preview-tls/apply",
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        reserved_at=reserved_at,
    )


def _mutation_completion(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    response_trace_id: str,
) -> LaunchplaneIdempotencyRecord:
    return complete_launchplane_mutation_reservation(
        reservation,
        response_status_code=202,
        response_trace_id=response_trace_id,
        completed_at="2026-07-13T00:01:00Z",
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _db_only_mutation(
    *,
    lease_owner: str,
    idempotency_key: str,
    response_trace_id: str,
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/product-profiles/preview-tls/apply",
        idempotency_key=idempotency_key,
        request_fingerprint="mutation-fingerprint-a",
        lease_owner=lease_owner,
        response_status_code=202,
        response_trace_id=response_trace_id,
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _merge_train_controller_state(
    *,
    updated_at: str = "2026-07-13T00:00:00Z",
    lease_owner: str = "controller-a",
    lease_acquired_at: str = "2026-07-13T00:00:00Z",
    lease_expires_at: str = "2026-07-13T00:05:00Z",
    status: str = "running",
) -> MergeTrainControllerStateRecord:
    repository = "cbusillo/sellyouroutboard"
    base_branch = "main"
    return MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        ),
        repository=repository,
        base_branch=base_branch,
        policy_key=f"{repository}:{base_branch}",
        policy_sha256="policy-sha",
        status=status,  # type: ignore[arg-type]
        updated_at=updated_at,
        lease_owner=lease_owner if status == "running" else "",
        lease_acquired_at=lease_acquired_at if status == "running" else "",
        lease_expires_at=lease_expires_at if status == "running" else "",
        heartbeat_at=updated_at if status == "running" else "",
        active_action="land_batch" if status == "running" else "",
        active_phase="cleanup_candidate_ref" if status == "running" else "",
        active_record_id="landing-record" if status == "running" else "",
        step_payload={"candidate_ref": "refs/heads/launchplane/train/x"},
        reconciliation_status="clean",
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


def _product_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="postgres-reservation-test",
        display_name="PostgreSQL Reservation Test",
        repository="example/postgres-reservation-test",
        driver_id="odoo",
        image=ProductImageProfile(),
        preview=ProductPreviewProfile(),
        updated_at="2026-07-13T00:00:00Z",
        source="test:postgres-integration",
    )


def _public_ingress_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="postgres-public-ingress-test",
        display_name="PostgreSQL Public Ingress Test",
        repository="example/postgres-public-ingress-test",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/example/postgres-public-ingress-test"),
        runtime_port=3000,
        health_path="/healthz",
        lanes=(
            ProductLaneProfile(
                context="postgres-public-ingress-test",
                instance="prod",
                base_url="https://example.test",
                health_monitoring=ProductLaneHealthMonitoringPolicy(
                    monitoring_intent="public",
                    checks=(ProductLaneHealthCheck(name="public-ingress"),),
                ),
            ),
        ),
        updated_at="2026-07-27T00:00:00Z",
        source="test:postgres-integration",
    )


def _route_binding() -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product="example-product",
        context="example-testing",
        instance="web",
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-target",
            provider_evidence={"target_record": "example-testing:web"},
        ),
        ingress=RouteBindingIngress(
            provider="npmplus",
            endpoint_key="example-edge",
            termination_kind="edge",
            provider_evidence={"audit_record": "audit-1"},
        ),
        domains=(RouteBindingDomain(domain_name="app.example.test", role="primary"),),
        tls=RouteBindingTls(
            owner="launchplane",
            provider_evidence={"audit_record": "audit-1"},
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


def _outbox_delivery(*, suffix: str = "one") -> OutboxDeliveryRecord:
    dedupe_key = build_outbox_dedupe_key(
        kind="github_workflow_dispatch",
        parts=("postgres", suffix),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(
            kind="github_workflow_dispatch",
            dedupe_key=dedupe_key,
        ),
        kind="github_workflow_dispatch",
        aggregate_type="postgres_test",
        aggregate_id=suffix,
        dedupe_key=dedupe_key,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
        next_attempt_at="2026-07-13T00:00:00Z",
        payload={"repository": "example/repo", "workflow_id": "deploy.yml"},
    )


def _manager_preview_approval_event() -> ManagerPreviewApprovalEventRecord:
    occurred_at = "2026-07-30T12:00:00Z"
    binding = ManagerPreviewApprovalBinding(
        product="example-site",
        context="example-site-testing",
        repository="example/example-site",
        pr_number=17,
        pr_url="https://github.com/example/example-site/pull/17",
        head_sha="1" * 40,
        preview_id="preview-17",
        serving_generation_id="generation-17",
        artifact_id="artifact-17",
        artifact_image_digest=f"sha256:{'a' * 64}",
        manifest_fingerprint="manifest-17",
        preview_url="https://preview-17.example.com/",
        runtime_identity=RuntimeIdentity(
            product="example-site",
            context="example-site-testing",
            instance="preview-17",
            environment_kind="preview",
            deployment_record_id="deployment-17",
            artifact_id="artifact-17",
            source_git_ref="1" * 40,
            image_reference=f"ghcr.io/example/site@sha256:{'a' * 64}",
            preview_id="preview-17",
            preview_generation_id="generation-17",
        ),
    )
    return ManagerPreviewApprovalEventRecord(
        binding=binding,
        action="approved",
        occurred_at=occurred_at,
        source_event_kind="github_issue_comment",
        source_event_id="comment-101",
        authorization=ManagerPreviewApprovalAuthorization(
            manager_github_id=101,
            manager_login="manager",
            managed_set_id="manager.example-site",
            managed_rule_id="preview-approval",
            policy_record_id="launchplane-authz-policy-r00000000000000000001-example",
            policy_revision=1,
            policy_sha256="b" * 64,
            policy_source="test:manager-preview-approval",
            authorized_at=occurred_at,
        ),
    )


def _owner_acceptance_event(
    *,
    product: str = "example-site",
    source_event_id: str = "",
    occurred_at: str = "2026-08-07T12:00:00Z",
) -> OwnerAcceptanceEventRecord:
    binding = OwnerAcceptanceBinding(
        repository_id="1001",
        repository_owner_id="2001",
        repository="example/example-site",
        pull_request_number=17,
        head_sha="1" * 40,
        tree_sha="2" * 40,
        change_impact_policy_record_id="change-impact-policy-1001-r1",
        change_impact_policy_revision=1,
        change_impact_policy_digest="a" * 64,
        product=product,
        system="web",
        action="pull_request.owner_acceptance",
        environment="pull_request",
        owner_policy_record_id=f"product-owner-policy-{product}-r1",
        owner_policy_revision=1,
        owner_policy_digest="b" * 64,
        owner_requirement_record_id=f"product-owner-requirement-{product}-r1",
        owner_requirement_revision=1,
        owner_requirement_digest="c" * 64,
        preview=OwnerAcceptancePreviewBinding(
            context=f"{product}-preview",
            preview_id=f"preview-{product}-pr-17",
            serving_generation_id=f"preview-{product}-pr-17-generation-0001",
            artifact_id=f"artifact-{product}-pr-17",
            artifact_image_digest=f"sha256:{'d' * 64}",
            manifest_fingerprint=f"manifest-{product}-pr-17",
            preview_url=f"https://pr-17.{product}.example.test",
            runtime_identity=owner_acceptance_runtime_identity_binding(
                RuntimeIdentity(
                    product=product,
                    context=f"{product}-preview",
                    instance="preview-pr-17",
                    environment_kind="preview",
                    deployment_record_id=f"deployment-{product}-pr-17",
                    artifact_id=f"artifact-{product}-pr-17",
                    source_git_ref="1" * 40,
                    image_reference=f"ghcr.io/example/{product}@sha256:{'d' * 64}",
                    preview_id=f"preview-{product}-pr-17",
                    preview_generation_id=f"preview-{product}-pr-17-generation-0001",
                )
            ),
        ),
        review_context=OwnerAcceptanceReviewContext(
            base_ref="main",
            base_sha="3" * 40,
            change_class="routine",
            engineering_review_tier="routine",
            review_max_age_seconds=PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS,
            contributions=OwnerAcceptanceContributionBinding(
                resolution="resolved",
                reason_code="server_resolved",
                contributor_github_ids=(4001,),
                commit_count=2,
            ),
            policy_fingerprints=OwnerAcceptancePolicyFingerprintBinding(
                owner_membership_fingerprint="1" * 64,
                self_review_fingerprint="2" * 64,
                review_age_fingerprint="3" * 64,
                requirement_fingerprint="4" * 64,
                preview_trust_fingerprint="5" * 64,
            ),
            preview_isolation=OwnerAcceptancePreviewIsolationBinding(
                isolation_class="synthetic_seeded",
                data_transport_mode="migrate_seed",
                source="product_preview_profile",
            ),
        ),
    )
    return OwnerAcceptanceEventRecord(
        binding=binding,
        action="accepted",
        occurred_at=occurred_at,
        source_event_kind="browser_api",
        source_event_id=source_event_id or f"acceptance-{product}-101",
        authorization=OwnerAcceptanceAuthorization(
            owner_identity_id=f"owner-identity-github-{product}",
            owner_github_id=101,
            owner_login="owner",
            owner_policy_record_id=binding.owner_policy_record_id,
            owner_policy_revision=binding.owner_policy_revision,
            owner_policy_digest=binding.owner_policy_digest,
            owner_requirement_record_id=binding.owner_requirement_record_id,
            owner_requirement_revision=binding.owner_requirement_revision,
            owner_requirement_digest=binding.owner_requirement_digest,
            authorized_at=occurred_at,
        ),
    )


def _owner_acceptance_system_event(
    *,
    action: str,
    source_event_id: str,
    occurred_at: str,
) -> OwnerAcceptanceEventRecord:
    return OwnerAcceptanceEventRecord(
        binding=_owner_acceptance_event().binding,
        action=action,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        source_event_kind="system",
        source_event_id=source_event_id,
        reason="PostgreSQL subject sequence integration evidence.",
    )


class RealPostgresSchemaIntegrationTests(unittest.TestCase):
    def test_production_backup_authority_schema_and_revision_fences(self) -> None:
        with _store_for_fresh_head_database() as store:
            dry_run = _dry_run_envelope()
            dry_result = store.apply_production_backup_authority(dry_run)
            apply_envelope = dry_run.model_copy(
                update={
                    "mode": "apply",
                    "reviewed_authority_digest": dry_result.authority_digest,
                }
            )
            applied = store.apply_production_backup_authority(apply_envelope)
            self.assertEqual(applied.status, "applied")

            source = store.list_production_backup_target_records(target_id="example-prod-guest")[0]
            revision_two = _source_target(
                revision=2,
                supersedes_record_id=source.record_id,
            )
            with self.assertRaises(IntegrityError):
                with store._session_factory() as session:
                    session.add(store._production_backup_target_row(revision_two))
                    session.commit()

            store.write_production_backup_target_record(revision_two)
            retired = _source_target(
                revision=3,
                status="retired",
                supersedes_record_id=revision_two.record_id,
            )
            store.write_production_backup_target_record(retired)
            self.assertEqual(
                store.list_production_backup_target_records(target_id="example-prod-guest")[
                    0
                ].status,
                "retired",
            )

            engine = create_engine(store.database_url)
            try:
                inspector = inspect(engine)
                target_indexes = {
                    index["name"]
                    for index in inspector.get_indexes("launchplane_production_backup_targets")
                }
                policy_indexes = {
                    index["name"]
                    for index in inspector.get_indexes("launchplane_production_backup_policies")
                }
            finally:
                engine.dispose()

        self.assertIn(
            "launchplane_production_backup_target_active_uidx",
            target_indexes,
        )
        self.assertIn(
            "launchplane_production_backup_policy_active_uidx",
            policy_indexes,
        )

    def test_privileged_operation_worker_store_probes_schema_and_empty_poll(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            store = build_privileged_operation_worker_store(database_url=database_url)
            try:
                records = execute_approved_privileged_operations_once(
                    record_store=store,
                    lease_owner="postgres-integration-worker",
                )
            finally:
                store.close()

        self.assertEqual(records, ())

    def test_privileged_operation_worker_heartbeat_round_trip_and_index(self) -> None:
        with _store_for_fresh_head_database() as store:
            recorded_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
            record = PrivilegedOperationWorkerHeartbeatRecord(
                worker_identity_sha256="a" * 64,
                image_reference=f"example@sha256:{'c' * 64}",
                poll_interval_seconds=15,
                last_poll_succeeded_at=recorded_at.isoformat(),
            )
            store.write_privileged_operation_worker_heartbeat_record(
                record,
                prune_before=(recorded_at - timedelta(days=7)).isoformat(),
                prune_after=(recorded_at + timedelta(seconds=60)).isoformat(),
            )
            records = store.list_privileged_operation_worker_heartbeat_records()
            engine = create_engine(store.database_url)
            try:
                index_names = {
                    index["name"]
                    for index in inspect(engine).get_indexes(
                        "launchplane_privileged_operation_worker_heartbeats"
                    )
                }
            finally:
                engine.dispose()

        self.assertEqual(records, (record,))
        self.assertIn("launchplane_privop_worker_heartbeats_freshness_idx", index_names)

    def test_merge_train_policy_compare_write_uses_active_cas_and_idempotency(self) -> None:
        with _store_for_fresh_head_database() as store:
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-active",
                updated_at="2026-08-22T19:00:00+00:00",
            )
            replacement_record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-candidate",
                updated_at="2026-08-22T20:00:00+00:00",
            )
            store.write_merge_train_policy_record(active_record)
            mutation = DbOnlyMutationRequest(
                scope="privileged-operation-execution",
                route_path="service-internal:privileged-operation-worker:managed-merge-train-policy-import",
                idempotency_key="privileged-operation-postgres-merge-train-policy",
                request_fingerprint="merge-train-policy-import-fingerprint",
                lease_owner="postgres-integration-worker",
                response_status_code=200,
                response_trace_id="trace-postgres-merge-train-policy",
                response_payload={"status": "ok"},
            )

            written = store.compare_and_write_merge_train_policy_record(
                expected_record=active_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            replayed = store.compare_and_write_merge_train_policy_record(
                expected_record=active_record,
                replacement_record=replacement_record,
                mutation=mutation,
            )
            conflict = store.compare_and_write_merge_train_policy_record(
                expected_record=active_record,
                replacement_record=replacement_record,
                mutation=replace(mutation, request_fingerprint="different-fingerprint"),
            )
            active_records = store.list_merge_train_policy_records(status="active", limit=2)
            superseded_records = store.list_merge_train_policy_records(
                status="superseded",
                limit=10,
            )

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(conflict.status, "idempotency_conflict")
        self.assertEqual(
            [record.record_id for record in active_records], [replacement_record.record_id]
        )
        self.assertEqual(
            [record.record_id for record in superseded_records], [active_record.record_id]
        )

    def test_merge_train_policy_database_trigger_fences_direct_active_writes(self) -> None:
        with _store_for_fresh_head_database() as store:
            first_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-direct-first",
                updated_at="2026-09-02T00:00:00+00:00",
            )
            second_record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-direct-second",
                updated_at="2026-09-02T00:01:00+00:00",
            )
            engine = create_engine(store.database_url)
            try:
                with engine.begin() as connection:
                    for record in (first_record, second_record):
                        connection.execute(
                            text(
                                "INSERT INTO launchplane_merge_train_policies "
                                "(record_id, status, source, updated_at, policy_sha256, payload) "
                                "VALUES (:record_id, :status, :source, :updated_at, "
                                ":policy_sha256, CAST(:payload AS JSONB))"
                            ),
                            {
                                "record_id": record.record_id,
                                "status": record.status,
                                "source": record.source,
                                "updated_at": record.updated_at,
                                "policy_sha256": record.policy_sha256,
                                "payload": json.dumps(record.model_dump(mode="json")),
                            },
                        )
                    inserted_rows = tuple(
                        connection.execute(
                            text(
                                "SELECT record_id, status, payload ->> 'status' "
                                "FROM launchplane_merge_train_policies ORDER BY record_id"
                            )
                        )
                    )
                    connection.execute(
                        text(
                            "UPDATE launchplane_merge_train_policies "
                            "SET status = 'active', "
                            "payload = jsonb_set(payload, '{status}', '\"active\"'::jsonb, true) "
                            "WHERE record_id = :record_id"
                        ),
                        {"record_id": first_record.record_id},
                    )
                    reactivated_rows = tuple(
                        connection.execute(
                            text(
                                "SELECT record_id, status, payload ->> 'status' "
                                "FROM launchplane_merge_train_policies ORDER BY record_id"
                            )
                        )
                    )
            finally:
                engine.dispose()

        self.assertEqual(
            inserted_rows,
            (
                (first_record.record_id, "superseded", "superseded"),
                (second_record.record_id, "active", "active"),
            ),
        )
        self.assertEqual(
            reactivated_rows,
            (
                (first_record.record_id, "active", "active"),
                (second_record.record_id, "superseded", "superseded"),
            ),
        )

    def test_merge_train_policy_compare_write_rejects_record_id_from_history(self) -> None:
        with _store_for_fresh_head_database() as store:
            historical_record = build_test_merge_train_policy_record(
                repository="cbusillo/odoo-devkit",
                record_id="merge-train-policy-historical",
                updated_at="2026-08-22T18:00:00+00:00",
            ).model_copy(update={"status": "superseded"})
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-active",
                updated_at="2026-08-22T19:00:00+00:00",
            )
            replacement_record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id=historical_record.record_id,
                updated_at="2026-08-22T20:00:00+00:00",
            )
            store.write_merge_train_policy_record(historical_record)
            store.write_merge_train_policy_record(active_record)

            result = store.compare_and_write_merge_train_policy_record(
                expected_record=active_record,
                replacement_record=replacement_record,
            )
            active_records = store.list_merge_train_policy_records(status="active")
            stored_historical_record = store.read_merge_train_policy_record(
                historical_record.record_id
            )

        self.assertEqual(result.status, "record_id_conflict")
        self.assertEqual(active_records, (active_record,))
        self.assertEqual(stored_historical_record, historical_record)

    def test_runtime_schema_compatibility_reports_missing_relation(self) -> None:
        with _store_for_fresh_head_database() as store:
            with self.assertRaisesRegex(
                RuntimeError,
                "launchplane_missing_worker_relation",
            ):
                store.verify_runtime_schema_compatibility(
                    required_relations=("launchplane_missing_worker_relation",)
                )

    def test_privileged_operation_worker_runtime_store_times_out_blocked_statement(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            with patch(
                "control_plane.storage.factory."
                "PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS",
                100,
            ):
                store = build_privileged_operation_worker_store(database_url=database_url)
            try:
                with (
                    self.assertRaises(OperationalError),
                    store._engine.connect() as connection,
                ):
                    connection.execute(text("select pg_sleep(1)"))
            finally:
                store.close()

    def test_repository_human_admission_schema_has_postgres_types_and_partial_index(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            engine = create_engine(store.database_url)
            try:
                inspector = inspect(engine)
                role_columns = {
                    column["name"]: str(column["type"]).lower()
                    for column in inspector.get_columns(
                        "launchplane_repository_human_role_policies"
                    )
                }
                waiver_columns = {
                    column["name"]: str(column["type"]).lower()
                    for column in inspector.get_columns(
                        "launchplane_tenant_technical_human_waiver_events"
                    )
                }
                role_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_repository_human_role_policies")
                }
            finally:
                engine.dispose()

        self.assertEqual(role_columns["payload"], "jsonb")
        self.assertIn("bigint", role_columns["role_policy_revision"])
        self.assertEqual(waiver_columns["payload"], "jsonb")
        self.assertIn("bigint", waiver_columns["author_github_id"])
        self.assertTrue(role_indexes["launchplane_repo_human_role_active_uidx"]["unique"])

    def test_manager_preview_approval_events_persist_append_only(self) -> None:
        with _store_for_fresh_head_database() as store:
            event = _manager_preview_approval_event()

            self.assertEqual(store.write_manager_preview_approval_event_record(event), "written")
            self.assertEqual(store.write_manager_preview_approval_event_record(event), "replayed")
            self.assertEqual(
                store.list_manager_preview_approval_event_records(
                    product="example-site",
                    context="example-site-testing",
                    repository="example/example-site",
                    pr_number=17,
                ),
                (event,),
            )

            conflicting = ManagerPreviewApprovalEventRecord.model_validate(
                {**event.model_dump(mode="json"), "reason": "Conflicting replay."}
            )
            with self.assertRaises(ManagerPreviewApprovalEventConflictError):
                store.write_manager_preview_approval_event_record(conflicting)

    def test_owner_acceptance_events_persist_append_only(self) -> None:
        with _store_for_fresh_head_database() as store:
            event = _owner_acceptance_event()
            second_event = _owner_acceptance_event(product="example-admin")

            self.assertEqual(store.write_owner_acceptance_event_record(event), "written")
            self.assertEqual(store.write_owner_acceptance_event_record(second_event), "written")
            replay_payload = event.model_dump(mode="json")
            replay_payload["occurred_at"] = "2026-08-07T12:01:00Z"
            replay_payload["authorization"]["authorized_at"] = "2026-08-07T12:01:00Z"
            replay = OwnerAcceptanceEventRecord.model_validate(replay_payload)
            self.assertEqual(store.write_owner_acceptance_event_record(replay), "replayed")
            persisted_event = store.read_owner_acceptance_event_record(event.event_id)
            self.assertEqual(persisted_event.subject_sequence, 1)
            self.assertEqual(
                persisted_event.model_dump(mode="json", exclude={"subject_sequence"}),
                event.model_dump(mode="json", exclude={"subject_sequence"}),
            )
            self.assertEqual(
                store.list_owner_acceptance_event_records(
                    repository_id="1001",
                    repository="example/example-site",
                    pull_request_number=17,
                    product="example-site",
                    system="web",
                    action="pull_request.owner_acceptance",
                ),
                (persisted_event,),
            )
            all_product_events = store.list_owner_acceptance_event_records(
                repository_id="1001",
                pull_request_number=17,
            )
            self.assertEqual(len(all_product_events), 2)
            self.assertEqual(
                {record.binding.product for record in all_product_events},
                {"example-site", "example-admin"},
            )

            conflicting = OwnerAcceptanceEventRecord.model_validate(
                {**event.model_dump(mode="json"), "reason": "Conflicting replay."}
            )
            with self.assertRaises(OwnerAcceptanceEventConflictError):
                store.write_owner_acceptance_event_record(conflicting)

    def test_owner_acceptance_events_project_bound_review_context_columns(self) -> None:
        """The queryable columns mirror the bound reviewed context for audit and fencing."""
        with _store_for_fresh_head_database() as store:
            event = _owner_acceptance_event()
            store.write_owner_acceptance_event_record(event)

            with store._session_factory() as session:  # noqa: SLF001
                row = session.execute(
                    text(
                        "SELECT base_ref, base_sha, change_class, review_max_age_seconds, "
                        "contribution_resolution, preview_isolation_class, self_review "
                        "FROM launchplane_owner_acceptance_events WHERE event_id = :event_id"
                    ),
                    {"event_id": event.event_id},
                ).one()

            self.assertEqual(row.base_ref, "main")
            self.assertEqual(row.base_sha, "3" * 40)
            self.assertEqual(row.change_class, "routine")
            self.assertEqual(
                row.review_max_age_seconds,
                PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS,
            )
            self.assertEqual(row.contribution_resolution, "resolved")
            self.assertEqual(row.preview_isolation_class, "synthetic_seeded")
            self.assertFalse(row.self_review)

    def test_owner_acceptance_subject_sequences_serialize_concurrent_appends_and_replay(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as first_store:
            second_store = PostgresRecordStore(database_url=first_store.database_url)
            first = _owner_acceptance_system_event(
                action="superseded",
                source_event_id="concurrent-owner-event-one",
                occurred_at="2026-08-07T13:00:00Z",
            )
            second = _owner_acceptance_system_event(
                action="invalidated",
                source_event_id="concurrent-owner-event-two",
                occurred_at="2026-08-07T11:00:00Z",
            )
            barrier = threading.Barrier(2)

            def append(
                store_and_event: tuple[PostgresRecordStore, OwnerAcceptanceEventRecord],
            ) -> str:
                active_store, event = store_and_event
                barrier.wait()
                return active_store.write_owner_acceptance_event_record(event)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(append, ((first_store, first), (second_store, second)))
                    )
                self.assertEqual(sorted(statuses), ["written", "written"])
                records = first_store.list_owner_acceptance_event_records()
                self.assertEqual(sorted(record.subject_sequence for record in records), [1, 2])

                self.assertEqual(
                    second_store.write_owner_acceptance_event_record(first),
                    "replayed",
                )
                third = _owner_acceptance_system_event(
                    action="superseded",
                    source_event_id="concurrent-owner-event-three",
                    occurred_at="2026-08-07T10:00:00Z",
                )
                self.assertEqual(
                    first_store.write_owner_acceptance_event_record(third),
                    "written",
                )
                self.assertEqual(
                    first_store.read_owner_acceptance_event_record(third.event_id).subject_sequence,
                    3,
                )
            finally:
                second_store.close()

    def test_owner_acceptance_concurrent_exact_replay_receives_no_new_sequence(self) -> None:
        with _store_for_fresh_head_database() as first_store:
            second_store = PostgresRecordStore(database_url=first_store.database_url)
            event = _owner_acceptance_system_event(
                action="superseded",
                source_event_id="concurrent-owner-exact-replay",
                occurred_at="2026-08-07T13:00:00Z",
            )
            barrier = threading.Barrier(2)

            def append(active_store: PostgresRecordStore) -> str:
                barrier.wait()
                return active_store.write_owner_acceptance_event_record(event)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(executor.map(append, (first_store, second_store)))
                self.assertEqual(sorted(statuses), ["replayed", "written"])
                persisted = first_store.read_owner_acceptance_event_record(event.event_id)
                self.assertEqual(persisted.subject_sequence, 1)

                next_event = _owner_acceptance_system_event(
                    action="invalidated",
                    source_event_id="after-concurrent-owner-exact-replay",
                    occurred_at="2026-08-07T11:00:00Z",
                )
                self.assertEqual(
                    first_store.write_owner_acceptance_event_record(next_event),
                    "written",
                )
                self.assertEqual(
                    first_store.read_owner_acceptance_event_record(
                        next_event.event_id
                    ).subject_sequence,
                    2,
                )
            finally:
                second_store.close()

    def test_owner_acceptance_invalid_transition_rolls_back_sequence_allocation(self) -> None:
        with _store_for_fresh_head_database() as store:
            accepted = _owner_acceptance_event(source_event_id="accepted-before-rollback")
            reaffirmed = _owner_acceptance_event(source_event_id="invalid-reaffirmation")
            self.assertEqual(store.write_owner_acceptance_event_record(accepted), "written")

            with self.assertRaises(OwnerAcceptanceTransitionError):
                store.write_owner_acceptance_event_record(reaffirmed)

            revoked = OwnerAcceptanceEventRecord(
                binding=accepted.binding,
                action="revoked",
                occurred_at="2026-08-07T11:00:00Z",
                source_event_kind="browser_api",
                source_event_id="revoke-after-rollback",
                reason="Owner withdrew the product review.",
                authorization=accepted.authorization.model_copy(
                    update={"authorized_at": "2026-08-07T11:00:00Z"}
                )
                if accepted.authorization is not None
                else None,
            )
            self.assertEqual(store.write_owner_acceptance_event_record(revoked), "written")
            persisted = store.read_owner_acceptance_event_record(revoked.event_id)
            self.assertEqual(persisted.subject_sequence, 2)
            with store._engine.connect() as connection:
                last_sequence = connection.execute(
                    text(
                        """
                        SELECT last_sequence
                        FROM launchplane_owner_acceptance_subject_sequences
                        WHERE repository_id = '1001'
                          AND pr_number = 17
                          AND product = 'example-site'
                          AND system = 'web'
                          AND owner_action = 'pull_request.owner_acceptance'
                          AND environment = 'pull_request'
                        """
                    )
                ).scalar_one()
            self.assertEqual(last_sequence, 2)

    def test_full_release_upgrades_compatibility_floor_before_store_startup(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(
                _alembic_config(database_url),
                AUTHZ_COMPATIBILITY_FLOOR_REVISION,
            )
            migrated_revision = migrate_schema(database_url=database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                observed_revision = _current_alembic_version(store._engine)
            finally:
                store.close()

        self.assertEqual(migrated_revision, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(observed_revision, EXPECTED_ALEMBIC_HEAD_REVISION)

    def test_owner_review_policy_defaults_backfill_on_postgres(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "b5d7f9a1c3e6")
            engine = create_engine(database_url)
            legacy_payload = {
                "schema_version": 1,
                "product": "example-site",
                "system": "web",
                "policy_revision": 1,
                "owners": [],
                "quorum": 1,
                "status": "active",
                "effective_at": "2026-08-07T00:00:00Z",
                "source": "test",
                "reason": "Legacy Owner policy.",
            }
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO launchplane_product_owner_policies (
                            record_id, product, system, status, policy_revision, quorum,
                            effective_at, source, supersedes_record_id, policy_digest, payload
                        ) VALUES (
                            'owner-policy-legacy', 'example-site', 'web', 'active', 1, 1,
                            '2026-08-07T00:00:00Z', 'test', NULL, :policy_digest,
                            CAST(:payload AS JSONB)
                        )
                        """
                    ),
                    {
                        "policy_digest": "a" * 64,
                        "payload": json.dumps(legacy_payload, sort_keys=True),
                    },
                )
            engine.dispose()

            migrate_schema(database_url=database_url)
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    payload, policy_digest = connection.execute(
                        text(
                            "SELECT payload, policy_digest "
                            "FROM launchplane_product_owner_policies "
                            "WHERE record_id = 'owner-policy-legacy'"
                        )
                    ).one()
            finally:
                engine.dispose()

        self.assertEqual(payload["review_age"]["routine_max_age_seconds"], 2592000)
        self.assertEqual(payload["review_age"]["elevated_max_age_seconds"], 604800)
        self.assertIs(payload["self_review"]["routine_exception_enabled"], False)
        self.assertEqual(
            payload["preview_trust"]["minimum_isolation_class"],
            "synthetic_seeded",
        )
        self.assertEqual(policy_digest, "a" * 64)

    def test_f4_accepts_previous_writer_shape(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            policy = LaunchplaneAuthzPolicy(
                schema_version=2,
                local_admins=(
                    LocalAdminPolicyRule(
                        managed_set_id="operator.owner",
                        managed_rule_id="authz.admin",
                        subjects=("owner",),
                        token_labels=("owner-admin",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        actions=("authz_policy_grant.write",),
                    ),
                ),
            )
            first_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-compat-first",
                source="test:compat",
                updated_at="2026-07-18T00:00:00Z",
                policy=policy,
            )
            second_record = first_record.model_copy(
                update={
                    "record_id": "authz-compat-second",
                    "updated_at": "2026-07-18T00:01:00Z",
                }
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                with store._engine.begin() as connection:
                    for record in (first_record, second_record):
                        payload = record.model_dump(mode="json")
                        payload.pop("revision", None)
                        connection.execute(
                            text(
                                "insert into launchplane_authz_policies "
                                "(record_id, status, source, updated_at, policy_sha256, payload) "
                                "values (:record_id, :status, :source, :updated_at, "
                                ":policy_sha256, cast(:payload as jsonb))"
                            ),
                            {
                                "record_id": record.record_id,
                                "status": record.status,
                                "source": record.source,
                                "updated_at": record.updated_at,
                                "policy_sha256": record.policy_sha256,
                                "payload": json.dumps(payload),
                            },
                        )
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
                with store._engine.connect() as connection:
                    revisions = tuple(
                        connection.execute(
                            text(
                                "select revision from launchplane_authz_policies order by revision"
                            )
                        ).scalars()
                    )
            finally:
                store.close()

        self.assertEqual(revisions, (1, 2))
        self.assertEqual(
            tuple(record.record_id for record in active_records), ("authz-compat-second",)
        )
        self.assertEqual(
            tuple(record.record_id for record in superseded_records),
            ("authz-compat-first",),
        )
        self.assertEqual(
            active_records[0].policy.local_admins[0].managed_rule_id,
            "authz.admin",
        )

    def test_managed_writer_rejects_concurrent_stale_authz_replacement(self) -> None:
        with _isolated_postgres_database() as database_url:
            migrate_schema(database_url=database_url)
            base_rule = LocalAdminPolicyRule(
                managed_set_id="operator.owner",
                managed_rule_id="authz.admin",
                subjects=("owner",),
                token_labels=("owner-admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            )
            base_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-concurrent-base",
                source="test:concurrent",
                updated_at="2026-07-18T00:00:00Z",
                policy=LaunchplaneAuthzPolicy(schema_version=2, local_admins=(base_rule,)),
            )
            replacements = tuple(
                LaunchplaneAuthzPolicyRecord(
                    record_id=f"authz-concurrent-{suffix}",
                    revision=2,
                    source="test:concurrent",
                    updated_at=f"2026-07-18T00:0{position}:00Z",
                    policy=LaunchplaneAuthzPolicy(
                        schema_version=2,
                        local_admins=(
                            base_rule,
                            LocalAdminPolicyRule(
                                managed_set_id="test.concurrent",
                                managed_rule_id=f"grant.{suffix}",
                                subjects=(suffix,),
                                token_labels=(f"{suffix}-token",),
                                products=("launchplane",),
                                contexts=("launchplane",),
                                actions=("product_profile.read",),
                            ),
                        ),
                    ),
                )
                for position, suffix in enumerate(("a", "b"), start=1)
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                base_record = store.seed_authz_policy_if_absent(base_record)
                start_barrier = threading.Barrier(2)

                def compare_and_write(record: LaunchplaneAuthzPolicyRecord) -> bool:
                    start_barrier.wait(timeout=5)
                    result = store.compare_and_write_authz_policy_record(
                        expected_record=base_record,
                        replacement_record=record,
                    )
                    return result.status == "written"

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = tuple(executor.map(compare_and_write, replacements))
                active_records = store.list_authz_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(active_records), 1)
        self.assertIn(active_records[0].record_id, {record.record_id for record in replacements})

    def test_privileged_policy_activation_uses_atomic_cas_and_idempotency(self) -> None:
        with _isolated_postgres_database() as database_url:
            migrate_schema(database_url=database_url)
            applying_identity = GitHubHumanIdentity(
                login="postgres-owner",
                github_id=123,
                name="Postgres Owner",
                email="postgres-owner@example.test",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
            admin_rules = tuple(
                GitHubHumanPolicyRule(
                    managed_set_id="test.activation-admins",
                    managed_rule_id=managed_rule_id,
                    github_ids=(github_id,),
                    roles=("admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                )
                for managed_rule_id, github_id in (
                    ("applying-admin", 123),
                    ("independent-admin", 456),
                )
            )
            policy = LaunchplaneAuthzPolicy(schema_version=2, github_humans=admin_rules)
            policy_digest = authz_policy_sha256(policy)
            seed_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=1,
                    policy_sha256=policy_digest,
                ),
                revision=1,
                source="test:activation-postgres",
                updated_at="2026-08-30T00:00:00Z",
                policy_sha256=policy_digest,
                policy=policy,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                seed_record = store.seed_authz_policy_if_absent(seed_record)
                dry_run_request = authz_policy_activation.build_authz_policy_operation_activation_reconcile_request(
                    github_id=applying_identity.github_id,
                    mode="dry_run",
                    reason="Review the privileged-policy activation.",
                )
                dry_run_result = authz_grant_service.execute_managed_authz_policy_reconcile(
                    record_store=store,
                    request=dry_run_request,
                    identity=applying_identity,
                    trace_id="postgres-activation-dry-run",
                    now_timestamp=lambda: "2026-08-30T00:01:00Z",
                    authorized_policy_sha256=seed_record.policy_sha256,
                    immutable_applying_github_id=applying_identity.github_id,
                    source=authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_SOURCE,
                )
                dry_run_diff = authz_grant_service.AuthzManagedPolicyDiff.model_validate(
                    dry_run_result.driver_result["diff"]
                )
                apply_request = authz_policy_activation.build_authz_policy_operation_activation_reconcile_request(
                    github_id=applying_identity.github_id,
                    mode="apply",
                    reason="Review the privileged-policy activation.",
                    reviewed_plan_sha256=dry_run_diff.plan_sha256,
                )
                apply_result = authz_grant_service.execute_managed_authz_policy_reconcile(
                    record_store=store,
                    request=apply_request,
                    identity=applying_identity,
                    trace_id="postgres-activation-apply",
                    now_timestamp=lambda: "2026-08-30T00:02:00Z",
                    authorized_policy_sha256=seed_record.policy_sha256,
                    immutable_applying_github_id=applying_identity.github_id,
                    source=authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_SOURCE,
                )
                mutation = DbOnlyMutationRequest(
                    scope=authz_policy_activation.authz_policy_operation_activation_idempotency_scope(
                        applying_identity.github_id
                    ),
                    route_path="/v1/authz-policies/privileged-policy-operations/activation/apply",
                    idempotency_key="postgres-activation",
                    request_fingerprint="a" * 64,
                    lease_owner="postgres-activation-apply",
                    response_status_code=202,
                    response_trace_id="postgres-activation-apply",
                    response_payload={"status": "accepted"},
                    lease_seconds=300,
                )
                written = store.compare_and_write_authz_policy_record(
                    expected_record=apply_result.previous_authz_policy_record,
                    replacement_record=apply_result.authz_policy_record,
                    mutation=mutation,
                )
                replayed = store.compare_and_write_authz_policy_record(
                    expected_record=apply_result.previous_authz_policy_record,
                    replacement_record=apply_result.authz_policy_record,
                    mutation=mutation,
                )
                conflicting = store.compare_and_write_authz_policy_record(
                    expected_record=apply_result.previous_authz_policy_record,
                    replacement_record=apply_result.authz_policy_record,
                    mutation=replace(mutation, request_fingerprint="b" * 64),
                )
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(conflicting.status, "idempotency_conflict")
        self.assertEqual(len(active_records), 1)
        self.assertEqual(
            active_records[0].source,
            authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_SOURCE,
        )
        self.assertEqual(
            authz_policy_activation.authz_policy_operation_activation_state(
                active_records[0].policy
            ),
            "active",
        )

    def test_schema_migration_serializes_concurrent_startups(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(
                _alembic_config(database_url),
                AUTHZ_COMPATIBILITY_FLOOR_REVISION,
            )
            first_migration_entered = threading.Event()
            release_first_migration = threading.Event()
            invocation_lock = threading.Lock()
            invocation_count = 0

            def delayed_schema_migration_action(
                *, current_revision: str, target_revision: str
            ) -> str:
                nonlocal invocation_count
                with invocation_lock:
                    invocation_count += 1
                    is_first_invocation = invocation_count == 1
                if is_first_invocation:
                    first_migration_entered.set()
                    if not release_first_migration.wait(timeout=5):
                        raise TimeoutError("Timed out waiting to release the first migration.")
                return schema_migration_action(
                    current_revision=current_revision,
                    target_revision=target_revision,
                )

            with patch(
                "control_plane.storage.schema_migration.schema_migration_action",
                side_effect=delayed_schema_migration_action,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(
                        migrate_schema,
                        database_url=database_url,
                        target_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
                    )
                    self.assertTrue(first_migration_entered.wait(timeout=5))
                    second_future = executor.submit(
                        migrate_schema,
                        database_url=database_url,
                        target_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
                    )
                    time.sleep(0.1)
                    self.assertFalse(second_future.done())
                    release_first_migration.set()
                    revisions = (first_future.result(), second_future.result())
            engine = create_engine(database_url)
            try:
                observed_revision = _current_alembic_version(engine)
            finally:
                engine.dispose()

        self.assertEqual(revisions, (EXPECTED_ALEMBIC_HEAD_REVISION,) * 2)
        self.assertEqual(observed_revision, EXPECTED_ALEMBIC_HEAD_REVISION)

    def test_artifact_dependency_provenance_round_trips_jsonb(self) -> None:
        with _store_for_fresh_head_database() as store:
            manifest = artifact_manifest_v2()

            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)

            provenance = loaded.dependency_provenance
            expected_provenance = manifest.dependency_provenance
            assert provenance is not None
            assert expected_provenance is not None
            self.assertEqual(loaded.schema_version, 2)
            self.assertEqual(provenance.target_platforms, ("linux/amd64", "linux/arm64"))
            self.assertEqual(
                provenance.python_environments["linux/amd64"].packages_sha256,
                expected_provenance.python_environments["linux/amd64"].packages_sha256,
            )

    def test_artifact_dependency_provenance_migration_preserves_v1_payload(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "e1f3a5c7d9b1")
            legacy_artifact_id = "artifact-cm-v1-before-dependency-provenance"
            legacy_source_commit = "legacy-short-ref"
            legacy_image_repository = "ghcr.io/cbusillo/odoo-tenant-cm"
            legacy_image_digest = "sha256:legacy"
            legacy_payload = {
                "artifact_id": legacy_artifact_id,
                "source_commit": legacy_source_commit,
                "enterprise_base_digest": "sha256:legacy",
                "image": {
                    "repository": legacy_image_repository,
                    "digest": legacy_image_digest,
                },
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_artifact_manifests "
                        "(artifact_id, source_commit, image_repository, image_digest, payload) "
                        "VALUES (:artifact_id, :source_commit, :image_repository, :image_digest, "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "artifact_id": legacy_artifact_id,
                        "source_commit": legacy_source_commit,
                        "image_repository": legacy_image_repository,
                        "image_digest": legacy_image_digest,
                        "payload": json.dumps(legacy_payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_artifact_manifest(legacy_artifact_id)
            finally:
                store.close()

        self.assertEqual(loaded.schema_version, 1)
        self.assertIsNone(loaded.dependency_provenance)
        self.assertEqual(loaded.source_commit, "legacy-short-ref")

    def test_authz_policy_migration_canonicalizes_active_history_and_revisions(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "f3b5d7e9a1c2")
            policy = LaunchplaneAuthzPolicy(
                github_actions=(
                    GitHubActionsPolicyRule(
                        repository="cbusillo/launchplane",
                        actions=("product_profile.read",),
                    ),
                ),
            )
            records = (
                LaunchplaneAuthzPolicyRecord(
                    record_id="legacy-authz-older",
                    source="test:legacy",
                    updated_at="2026-07-17T00:00:00Z",
                    policy=policy,
                ),
                LaunchplaneAuthzPolicyRecord(
                    record_id="legacy-authz-newer",
                    source="test:legacy",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=policy,
                ),
            )
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for record in records:
                    payload = record.model_dump(mode="json")
                    payload.pop("revision", None)
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_authz_policies "
                            "(record_id, status, source, updated_at, policy_sha256, payload) "
                            "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": record.record_id,
                            "status": record.status,
                            "source": record.source,
                            "updated_at": record.updated_at,
                            "policy_sha256": record.policy_sha256,
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                store.close()

        self.assertEqual(
            tuple((record.record_id, record.revision) for record in active_records),
            (("legacy-authz-newer", 2),),
        )
        self.assertEqual(
            tuple((record.record_id, record.revision) for record in superseded_records),
            (("legacy-authz-older", 1),),
        )

    def test_authz_policy_migration_rejects_tied_latest_active_records(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "f3b5d7e9a1c2")
            policy = LaunchplaneAuthzPolicy()
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for record_id in ("legacy-authz-a", "legacy-authz-b"):
                    record = LaunchplaneAuthzPolicyRecord(
                        record_id=record_id,
                        source="test:legacy",
                        updated_at="2026-07-18T00:00:00Z",
                        policy=policy,
                    )
                    payload = record.model_dump(mode="json")
                    payload.pop("revision", None)
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_authz_policies "
                            "(record_id, status, source, updated_at, policy_sha256, payload) "
                            "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": record.record_id,
                            "status": record.status,
                            "source": record.source,
                            "updated_at": record.updated_at,
                            "policy_sha256": record.policy_sha256,
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "share the latest updated_at timestamp"):
                _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            try:
                alembic_version = _current_alembic_version(engine)
            finally:
                engine.dispose()

        self.assertEqual(alembic_version, "f3b5d7e9a1c2")

    def test_authz_policy_write_fence_accepts_previous_binary_insert_shape(self) -> None:
        with _store_for_fresh_head_database() as store:
            current_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            legacy_record = LaunchplaneAuthzPolicyRecord(
                record_id="legacy-binary-active-write",
                source="test:legacy-binary",
                updated_at="2026-07-18T00:01:00Z",
                policy=LaunchplaneAuthzPolicy(
                    github_actions=(
                        GitHubActionsPolicyRule(
                            repository="example/legacy",
                            actions=("product_profile.read",),
                        ),
                    ),
                ),
            )
            legacy_payload = legacy_record.model_dump(mode="json")
            legacy_payload.pop("revision", None)
            engine = create_engine(store.database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_authz_policies "
                        "(record_id, status, source, updated_at, policy_sha256, payload) "
                        "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": legacy_record.record_id,
                        "status": legacy_record.status,
                        "source": legacy_record.source,
                        "updated_at": legacy_record.updated_at,
                        "policy_sha256": legacy_record.policy_sha256,
                        "payload": json.dumps(legacy_payload),
                    },
                )
                stored_payload = connection.execute(
                    text(
                        "select payload from launchplane_authz_policies "
                        "where record_id = :record_id"
                    ),
                    {"record_id": legacy_record.record_id},
                ).scalar_one()
            engine.dispose()
            active_records = store.list_authz_policy_records(status="active")
            superseded_records = store.list_authz_policy_records(status="superseded")

        self.assertEqual(active_records[0].record_id, legacy_record.record_id)
        self.assertEqual(active_records[0].revision, 2)
        self.assertEqual(
            superseded_records,
            (current_record.model_copy(update={"status": "superseded"}),),
        )
        self.assertNotIn("revision", stored_payload)

    def test_schema_verification_rejects_disabled_authz_policy_write_fence(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE launchplane_authz_policies DISABLE TRIGGER "
                            "launchplane_authz_policy_write_fence"
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "authz_policy_write_fence is disabled"):
                    verify_postgres_schema_invariants(engine)
            finally:
                engine.dispose()

    def test_schema_verification_rejects_disabled_merge_train_policy_write_fence(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE launchplane_merge_train_policies DISABLE TRIGGER "
                            "launchplane_merge_train_policy_write_fence"
                        )
                    )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "merge_train_policy_write_fence is disabled",
                ):
                    verify_postgres_schema_invariants(engine)
            finally:
                engine.dispose()

    def test_alembic_from_empty_database_reaches_exact_head_and_required_invariants(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                engine = store._engine
                inspector = inspect(engine)
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "launchplane_odoo_stable_bootstrap_operations"
                    )
                }
                idempotency_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_idempotency_records")
                }
                idempotency_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_idempotency_records")
                }
                authz_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_authz_policies")
                }
                authz_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_authz_policies")
                }
                outbox_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_outbox_deliveries")
                }
                outbox_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_outbox_deliveries")
                }
                incident_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_public_ingress_incidents")
                }
                incident_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_public_ingress_incidents")
                }
                observation_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_public_ingress_observations")
                }
                observation_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_public_ingress_observations")
                }
                payload_type = _column_type(
                    engine,
                    table_name="launchplane_idempotency_records",
                    column_name="payload",
                )
                attempt_type = _column_type(
                    engine,
                    table_name="launchplane_idempotency_records",
                    column_name="attempt",
                )
                outbox_payload_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="payload",
                )
                outbox_attempt_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="attempt",
                )
                outbox_max_attempts_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="max_attempts",
                )
                incident_state_version_type = _column_type(
                    engine,
                    table_name="launchplane_public_ingress_incidents",
                    column_name="state_version",
                )
                incident_event_payload_type = _column_type(
                    engine,
                    table_name="launchplane_public_ingress_incident_events",
                    column_name="payload",
                )
                incident_reminder_payload_type = _column_type(
                    engine,
                    table_name="launchplane_public_ingress_incident_reminders",
                    column_name="payload",
                )
                alembic_version = _current_alembic_version(engine)
            finally:
                store.close()

        self.assertEqual(alembic_version, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(payload_type, "jsonb")
        self.assertEqual(attempt_type, "integer")
        self.assertEqual(outbox_payload_type, "jsonb")
        self.assertEqual(outbox_attempt_type, "integer")
        self.assertEqual(outbox_max_attempts_type, "integer")
        self.assertEqual(incident_state_version_type, "integer")
        self.assertEqual(incident_event_payload_type, "jsonb")
        self.assertEqual(incident_reminder_payload_type, "jsonb")
        self.assertTrue(idempotency_columns["response_status_code"]["nullable"])
        self.assertFalse(authz_columns["revision"]["nullable"])
        self.assertTrue(authz_indexes["launchplane_authz_policies_revision_uidx"]["unique"])
        self.assertTrue(authz_indexes["launchplane_authz_policies_active_uidx"]["unique"])
        self.assertFalse(outbox_columns["payload"]["nullable"])
        self.assertTrue(outbox_indexes["launchplane_outbox_deliveries_dedupe_uidx"]["unique"])
        self.assertFalse(outbox_indexes["launchplane_outbox_deliveries_claim_idx"]["unique"])
        self.assertFalse(incident_columns["state_version"]["nullable"])
        self.assertFalse(observation_columns["check_token"]["nullable"])
        self.assertFalse(observation_columns["check_kind"]["nullable"])
        self.assertFalse(
            observation_indexes["launchplane_public_ingress_observations_check_idx"]["unique"]
        )
        self.assertTrue(
            incident_indexes["launchplane_public_ingress_incidents_open_uidx"]["unique"]
        )
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_scope_route_key_idx"]["unique"]
        )
        self.assertFalse(idempotency_indexes["launchplane_idempotency_state_lease_idx"]["unique"])
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_active_reconciliation_idx"]["unique"]
        )
        self.assertEqual(
            idempotency_indexes["launchplane_idempotency_active_reconciliation_idx"][
                "column_names"
            ],
            ["provider_target_key"],
        )
        self.assertIn("provider_target_key", idempotency_columns)
        self.assertTrue(indexes["launchplane_odoo_bootstrap_active_lane_uidx"]["unique"])

    def test_mutation_reservation_migration_backfills_existing_postgres_rows(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "c9d1e3f5a7b9")
            legacy_payload = {
                "schema_version": 1,
                "record_id": "idempotency-postgres-legacy",
                "scope": "github-actions:postgres-legacy",
                "route_path": "/v1/evidence/previews/generations",
                "idempotency_key": "postgres-legacy-key",
                "request_fingerprint": "postgres-legacy-fingerprint",
                "response_status_code": 202,
                "response_trace_id": "postgres-legacy-trace",
                "recorded_at": "2026-07-12T00:00:00Z",
                "response_payload": {"status": "accepted"},
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_idempotency_records "
                        "(record_id, scope, route_path, idempotency_key, request_fingerprint, "
                        "response_status_code, response_trace_id, recorded_at, payload) "
                        "VALUES (:record_id, :scope, :route_path, :idempotency_key, "
                        ":request_fingerprint, :response_status_code, :response_trace_id, "
                        ":recorded_at, CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": "idempotency-postgres-legacy",
                        "scope": "github-actions:postgres-legacy",
                        "route_path": "/v1/evidence/previews/generations",
                        "idempotency_key": "postgres-legacy-key",
                        "request_fingerprint": "postgres-legacy-fingerprint",
                        "response_status_code": 202,
                        "response_trace_id": "postgres-legacy-trace",
                        "recorded_at": "2026-07-12T00:00:00Z",
                        "payload": json.dumps(legacy_payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_idempotency_record(
                    scope="github-actions:postgres-legacy",
                    route_path="/v1/evidence/previews/generations",
                    idempotency_key="postgres-legacy-key",
                )
                with store._engine.connect() as connection:
                    promoted = (
                        connection.execute(
                            text(
                                "SELECT state, attempt, created_at, updated_at "
                                "FROM launchplane_idempotency_records "
                                "WHERE record_id = 'idempotency-postgres-legacy'"
                            )
                        )
                        .mappings()
                        .one()
                    )
            finally:
                store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.state, "completed")
        self.assertEqual(promoted["state"], "completed")
        self.assertEqual(promoted["attempt"], 1)
        self.assertEqual(promoted["created_at"], "2026-07-12T00:00:00Z")
        self.assertEqual(promoted["updated_at"], "2026-07-12T00:00:00Z")

    def test_provider_target_fence_migration_backfills_existing_active_rows(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "a2b4c6d8e0f2")
            reconciliation_key = "dokploy:compose:existing-provider-target"
            payload = {
                "schema_version": 2,
                "record_id": "mutation-reservation-existing-provider-target",
                "scope": "github-actions:provider-target-migration",
                "route_path": "/v1/test/provider-target",
                "idempotency_key": "provider-target-existing",
                "request_fingerprint": "provider-target-fingerprint",
                "state": "reconcile_required",
                "lease_owner": "worker-a",
                "lease_expires_at": "",
                "attempt": 1,
                "reconciliation_key": reconciliation_key,
                "provider_effect_phase": "",
                "provider_effect_started_at": "",
                "created_at": "2026-07-12T00:00:00Z",
                "updated_at": "2026-07-12T00:00:00Z",
                "response_status_code": None,
                "response_trace_id": "",
                "recorded_at": "",
                "response_payload": {},
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_idempotency_records "
                        "(record_id, scope, route_path, idempotency_key, request_fingerprint, "
                        "state, lease_owner, lease_expires_at, attempt, reconciliation_key, "
                        "created_at, updated_at, response_status_code, response_trace_id, "
                        "recorded_at, payload) VALUES "
                        "(:record_id, :scope, :route_path, :idempotency_key, "
                        ":request_fingerprint, 'reconcile_required', 'worker-a', '', 1, "
                        ":reconciliation_key, :created_at, :updated_at, NULL, '', '', "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": payload["record_id"],
                        "scope": payload["scope"],
                        "route_path": payload["route_path"],
                        "idempotency_key": payload["idempotency_key"],
                        "request_fingerprint": payload["request_fingerprint"],
                        "reconciliation_key": reconciliation_key,
                        "created_at": payload["created_at"],
                        "updated_at": payload["updated_at"],
                        "payload": json.dumps(payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_idempotency_record(
                    scope=str(payload["scope"]),
                    route_path=str(payload["route_path"]),
                    idempotency_key=str(payload["idempotency_key"]),
                )
                blocked = store.reserve_mutation(
                    scope="github-actions:provider-target-migration",
                    route_path="/v1/test/provider-target",
                    idempotency_key="provider-target-contender",
                    request_fingerprint="provider-target-contender-fingerprint",
                    lease_owner="worker-b",
                    reconciliation_key=reconciliation_key,
                )
            finally:
                store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.provider_target_key, reconciliation_key)
        self.assertEqual(blocked.status, "target_busy")

    def test_provider_target_fence_migration_fails_closed_on_duplicate_claims(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "a2b4c6d8e0f2")
            reconciliation_key = "dokploy:compose:duplicate-provider-target"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for index in (1, 2):
                    payload = {
                        "schema_version": 2,
                        "record_id": f"mutation-reservation-duplicate-target-{index}",
                        "scope": f"github-actions:duplicate-target-{index}",
                        "route_path": "/v1/test/provider-target",
                        "idempotency_key": f"provider-target-duplicate-{index}",
                        "request_fingerprint": f"provider-target-fingerprint-{index}",
                        "state": "reconcile_required",
                        "lease_owner": f"worker-{index}",
                        "lease_expires_at": "",
                        "attempt": 1,
                        "reconciliation_key": reconciliation_key,
                        "provider_effect_phase": "",
                        "provider_effect_started_at": "",
                        "created_at": "2026-07-12T00:00:00Z",
                        "updated_at": "2026-07-12T00:00:00Z",
                        "response_status_code": None,
                        "response_trace_id": "",
                        "recorded_at": "",
                        "response_payload": {},
                    }
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_idempotency_records "
                            "(record_id, scope, route_path, idempotency_key, "
                            "request_fingerprint, state, lease_owner, lease_expires_at, "
                            "attempt, reconciliation_key, created_at, updated_at, "
                            "response_status_code, response_trace_id, recorded_at, payload) "
                            "VALUES (:record_id, :scope, :route_path, :idempotency_key, "
                            ":request_fingerprint, 'reconcile_required', :lease_owner, '', 1, "
                            ":reconciliation_key, :created_at, :updated_at, NULL, '', '', "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": payload["record_id"],
                            "scope": payload["scope"],
                            "route_path": payload["route_path"],
                            "idempotency_key": payload["idempotency_key"],
                            "request_fingerprint": payload["request_fingerprint"],
                            "lease_owner": payload["lease_owner"],
                            "reconciliation_key": reconciliation_key,
                            "created_at": payload["created_at"],
                            "updated_at": payload["updated_at"],
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            with self.assertRaisesRegex(
                RuntimeError,
                "Cannot fence active provider mutations while duplicate target claims exist",
            ):
                _upgrade_empty_database_to_head(database_url)

    def test_startup_verification_fails_closed_when_critical_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_idempotency_scope_route_key_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_idempotency_records missing required index",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_reservation_lease_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_idempotency_state_lease_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_idempotency_state_lease_idx",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_route_bindings_lookup_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_route_bindings missing required index",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_payload_is_not_jsonb(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_route_bindings "
                        "alter column payload type json using payload::json"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_route_bindings.payload has type",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_primary_key_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_route_bindings "
                        "drop constraint launchplane_route_bindings_pkey"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"launchplane_route_bindings has primary key \(<none>\)",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_partial_predicate_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_odoo_bootstrap_active_lane_uidx"))
                connection.execute(
                    text(
                        "create unique index launchplane_odoo_bootstrap_active_lane_uidx "
                        "on launchplane_odoo_stable_bootstrap_operations "
                        "(product, context, instance)"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_odoo_bootstrap_active_lane_uidx has predicate",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_rejects_wrong_authz_active_predicate(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_authz_policies_active_uidx"))
                connection.execute(
                    text(
                        "create unique index launchplane_authz_policies_active_uidx "
                        "on launchplane_authz_policies (status) where status <> 'active'"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_authz_policies_active_uidx has predicate status<>'active'",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_outbox_claim_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_outbox_deliveries_claim_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_outbox_deliveries_claim_idx",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_outbox_payload_is_not_jsonb(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_outbox_deliveries "
                        "alter column payload type json using payload::json"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_outbox_deliveries.payload has type",
                ):
                    store.verify_schema()
            finally:
                store.close()


def _owner_control_shadow_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _owner_control_shadow_binding(private_key: Ed25519PrivateKey) -> ChannelBindingRecord:
    return ChannelBindingRecord(
        channel_session_id="owner-control-postgres-session",
        owner_github_id=100001,
        signature_algorithm="ed25519",
        owner_public_key=_owner_control_shadow_base64url(
            private_key.public_key().public_bytes_raw()
        ),
        session_issued_at="2025-01-01T00:00:00+00:00",
        session_expires_at="2035-01-01T00:00:00+00:00",
    )


def _owner_control_host_principal_claim() -> OwnerControlHostPrincipalClaim:
    return OwnerControlHostPrincipalClaim(
        host_instance_id="owner-control-postgres-host",
        principal_id="owner-control-postgres-principal",
        principal_separation="not_claimed",
        key_custody="not_claimed",
        gesture_source="not_claimed",
    )


def _seed_owner_control_shadow_issue_provenance(
    store: PostgresRecordStore,
) -> PrivilegedOperationRecord:
    request = ManagedSecretReencryptionPlanInput(reason="Rotate retained keys", source_label="test")
    evidence = ManagedSecretReencryptionHumanEvidence(
        result_status="ok",
        plan_digest="a" * 64,
        configured_secret_count=3,
        rotation_candidate_count=2,
        unchanged_count=1,
        unreadable_secret_count=0,
        active_key_id="redacted-key",
        retirement_blocked_key_ids=(),
        retirement_ready_key_ids=(),
        legacy_compatibility_key_loaded=False,
    )
    actor = PrivilegedOperationActor(identity_type="github_human", github_id=44, login="requester")
    record = PrivilegedOperationRecord(
        operation_id="privileged-operation-0123456789abcdef0123456789abcdef",
        descriptor_id="managed-secret-reencryption",
        descriptor_version=1,
        safety_class="secret_backed",
        status="planned",
        source_event_id="owner-control-test",
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    store.write_privileged_operation_plan(
        record,
        PrivilegedOperationEventRecord(
            operation_id=record.operation_id,
            sequence=1,
            action="planned",
            occurred_at=record.created_at,
            source_kind="browser_api",
            source_event_id=record.source_event_id,
            actor=actor,
            resulting_record_digest=privileged_operation_record_digest(record),
        ),
    )
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="owner-control-tests",
                managed_rule_id="approve",
                github_ids=(100001,),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    store.seed_authz_policy_if_absent(
        LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
            revision=1,
            status="active",
            source="owner-control-tests",
            updated_at="2026-08-28T00:00:00Z",
            policy_sha256=digest,
            policy=policy,
        )
    )
    return record


def _owner_control_shadow_envelope(
    private_key: Ed25519PrivateKey,
    *,
    binding: ChannelBindingRecord,
    request: ApprovalRequest,
) -> OwnerControlConfirmationEnvelope:
    response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=owner_control_channel_binding_sha256(binding),
        confirmed_at=request.issued_at,
    )
    return OwnerControlConfirmationEnvelope(
        channel_binding=binding,
        challenge_response=response,
        signature_algorithm="ed25519",
        signature=_owner_control_shadow_base64url(
            private_key.sign(owner_control_signature_payload_bytes(response))
        ),
    )


class RealPostgresStorageConcurrencyTests(unittest.TestCase):
    def test_owner_control_challenge_issuance_serializes_one_active_operation(self) -> None:
        with _store_for_fresh_head_database() as store:
            private_key = Ed25519PrivateKey.generate()
            binding = _owner_control_shadow_binding(private_key)
            store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=_owner_control_host_principal_claim(),
            )
            operation = _seed_owner_control_shadow_issue_provenance(store)
            issue_request = OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def issue_once(active_store: PostgresRecordStore) -> object:
                barrier.wait(timeout=10)
                return active_store.issue_owner_control_challenge(issue_request)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    issued_records = tuple(executor.map(issue_once, (store, second_store)))
                engine = create_engine(store.database_url)
                try:
                    with engine.connect() as connection:
                        active_count = connection.scalar(
                            text(
                                "select count(*) from "
                                "launchplane_owner_control_issued_challenges "
                                "where operation_id = :operation_id and state = 'issued'"
                            ),
                            {"operation_id": operation.operation_id},
                        )
                finally:
                    engine.dispose()
            finally:
                second_store.close()

        self.assertEqual(issued_records[0], issued_records[1])
        self.assertEqual(active_count, 1)

    def test_owner_control_reissuance_terminalizes_expired_challenge_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            private_key = Ed25519PrivateKey.generate()
            binding = _owner_control_shadow_binding(private_key)
            store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=_owner_control_host_principal_claim(),
            )
            operation = _seed_owner_control_shadow_issue_provenance(store)
            issue_request = OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=1,
            )
            with patch.object(
                store,
                "_owner_control_shadow_timestamp",
                return_value="2026-08-28T12:00:00+00:00",
            ):
                expired_candidate = store.issue_owner_control_challenge(issue_request)
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def reissue_once(
                active_store: PostgresRecordStore,
            ) -> OwnerControlIssuedChallengeRecord:
                with patch.object(
                    active_store,
                    "_owner_control_shadow_timestamp",
                    return_value="2026-08-28T12:00:02+00:00",
                ):
                    barrier.wait(timeout=10)
                    return active_store.issue_owner_control_challenge(issue_request)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    issued_records = tuple(executor.map(reissue_once, (store, second_store)))
                engine = create_engine(store.database_url)
                try:
                    with engine.connect() as connection:
                        active_count = connection.scalar(
                            text(
                                "select count(*) from "
                                "launchplane_owner_control_issued_challenges "
                                "where operation_id = :operation_id and state = 'issued'"
                            ),
                            {"operation_id": operation.operation_id},
                        )
                        lifecycle_count = connection.scalar(
                            text(
                                "select count(*) from "
                                "launchplane_owner_control_challenge_lifecycle_events "
                                "where challenge_id = :challenge_id"
                            ),
                            {"challenge_id": expired_candidate.challenge_id},
                        )
                        shadow_count = connection.scalar(
                            text(
                                "select count(*) from "
                                "launchplane_owner_control_shadow_verification_events "
                                "where challenge_id = :challenge_id"
                            ),
                            {"challenge_id": expired_candidate.challenge_id},
                        )
                finally:
                    engine.dispose()
            finally:
                second_store.close()

            expired = store.read_owner_control_issued_challenge(
                challenge_nonce=expired_candidate.challenge_nonce
            )

        self.assertEqual(issued_records[0], issued_records[1])
        self.assertNotEqual(issued_records[0].challenge_id, expired_candidate.challenge_id)
        self.assertEqual(expired.state, "expired")
        self.assertEqual(expired.attempt_count, 0)
        self.assertEqual(active_count, 1)
        self.assertEqual(lifecycle_count, 1)
        self.assertEqual(shadow_count, 0)

    def test_owner_control_shadow_verification_consumes_one_challenge_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            private_key = Ed25519PrivateKey.generate()
            binding = _owner_control_shadow_binding(private_key)
            store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=_owner_control_host_principal_claim(),
            )
            operation = _seed_owner_control_shadow_issue_provenance(store)
            issued = store.issue_owner_control_challenge(
                OwnerControlChallengeIssueRequest(
                    channel_session_id=binding.channel_session_id,
                    operation_id=operation.operation_id,
                    expires_in_seconds=300,
                )
            )
            envelope = _owner_control_shadow_envelope(
                private_key,
                binding=binding,
                request=issued.approval_request(),
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            loser_prelock_read_complete = threading.Event()
            allow_loser_locking_read = threading.Event()
            original_challenge_read = second_store._owner_control_issued_challenge_row

            def controlled_challenge_read(
                session: Any,
                *,
                challenge_nonce: str,
                for_update: bool = False,
            ) -> LaunchplaneOwnerControlIssuedChallengeRow | None:
                row = original_challenge_read(
                    session,
                    challenge_nonce=challenge_nonce,
                    for_update=for_update,
                )
                if not for_update:
                    loser_prelock_read_complete.set()
                    if not allow_loser_locking_read.wait(timeout=10):
                        raise TimeoutError("Timed out waiting to release the losing verification.")
                return row

            executor = ThreadPoolExecutor(max_workers=1)
            try:
                with patch.object(
                    second_store,
                    "_owner_control_issued_challenge_row",
                    side_effect=controlled_challenge_read,
                ):
                    losing_future = executor.submit(
                        second_store.verify_owner_control_confirmation_shadow,
                        envelope,
                    )
                    self.assertTrue(loser_prelock_read_complete.wait(timeout=10))
                    winning_status = store.verify_owner_control_confirmation_shadow(
                        envelope
                    ).verification_status
                    allow_loser_locking_read.set()
                    losing_status = losing_future.result(timeout=10).verification_status
                stored_challenge = store.read_owner_control_issued_challenge(
                    challenge_nonce=issued.challenge_nonce
                )
                events = store.list_owner_control_shadow_verification_events(
                    challenge_nonce=issued.challenge_nonce
                )
            finally:
                allow_loser_locking_read.set()
                executor.shutdown(wait=True)
                second_store.close()

        self.assertEqual([winning_status, losing_status], ["verified", "rejected"])
        self.assertEqual(stored_challenge.state, "consumed")
        self.assertEqual(stored_challenge.attempt_count, 2)
        self.assertIsNotNone(stored_challenge.consumed_at)
        self.assertEqual(
            sorted(
                (event.sequence, event.verification_status, event.rejection_reason)
                for event in events
            ),
            [(1, "verified", None), (2, "rejected", "challenge_replayed")],
        )
        self.assertTrue(all(event.verifier_mode == "shadow" for event in events))
        self.assertTrue(all(event.authorizes_execution is False for event in events))
        self.assertTrue(all(event.authority_state == "inert" for event in events))

    def test_concurrent_detached_application_retirement_plans_reuse_one_reservation(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_plan(active_store: PostgresRecordStore, trace_id: str) -> None:
                plan = _detached_application_retirement_plan().model_copy(
                    update={
                        "trace_id": trace_id,
                        "recorded_at": f"2026-08-11T12:00:0{trace_id[-1]}Z",
                    }
                )
                barrier.wait(timeout=10)
                active_store.write_detached_application_retirement_record(plan)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    tuple(
                        executor.map(
                            write_plan,
                            (store, second_store),
                            ("plan-trace-1", "plan-trace-2"),
                        )
                    )
                plans = store.list_detached_application_retirement_records(
                    candidate_target_sha256=(
                        _detached_application_retirement_plan().candidate_observation.target_id_sha256
                    ),
                    actor="test",
                    mode="plan",
                    idempotency_key="retire-detached",
                )
            finally:
                second_store.close()

        self.assertEqual(len(plans), 1)

    def test_concurrent_product_retirement_plan_writes_reuse_one_scoped_reservation(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_plan(active_store: PostgresRecordStore, trace_id: str) -> None:
                plan = _retirement_plan(_RetirementStore(), _retirement_observation()).model_copy(
                    update={
                        "trace_id": trace_id,
                        "recorded_at": f"2026-08-11T02:00:0{trace_id[-1]}Z",
                    }
                )
                barrier.wait(timeout=10)
                active_store.write_product_retirement_record(plan)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    tuple(
                        executor.map(
                            write_plan, (store, second_store), ("plan-trace-1", "plan-trace-2")
                        )
                    )
                plans = store.list_product_retirement_records(
                    product="example-site",
                    actor="test",
                    mode="plan",
                    idempotency_key="retire-example-site",
                )
            finally:
                second_store.close()

        self.assertEqual(len(plans), 1)

    def test_concurrent_public_ingress_failures_open_one_incident_and_keep_both_observations(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            store.write_product_profile_record(_public_ingress_profile())
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def monitor(active_store: PostgresRecordStore, checked_at: str) -> None:
                barrier.wait(timeout=10)
                run_public_ingress_monitor_once(
                    record_store=active_store,
                    checked_at=checked_at,
                    http_get=lambda url, _timeout: HttpObservation(
                        status_code=503,
                        final_url=url,
                        redirect_count=0,
                    ),
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(monitor, store, "2026-07-27T12:00:00Z")
                    second = executor.submit(monitor, second_store, "2026-07-27T12:00:01Z")
                    first.result(timeout=30)
                    second.result(timeout=30)
                incidents = store.list_public_ingress_incident_records(status="open")
                observations = store.list_public_ingress_observation_records(
                    product="postgres-public-ingress-test"
                )
                events = store.list_public_ingress_incident_event_records()
            finally:
                second_store.close()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(len(observations), 2)
        self.assertTrue(
            all(record.incident_id == incidents[0].incident_id for record in observations)
        )
        self.assertEqual([event.event for event in events], ["opened"])

    def test_authz_policy_compare_write_serializes_concurrent_writers(self) -> None:
        with _store_for_fresh_head_database() as store:
            initial_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def reconcile(active_store: PostgresRecordStore, suffix: str) -> str:
                policy = LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=(
                        GitHubActionsPolicyRule(
                            repository=f"example/{suffix}",
                            actions=("product_profile.read",),
                        ),
                    ),
                )
                replacement = LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(
                        revision=2,
                        policy_sha256=authz_policy_sha256(policy),
                    ),
                    revision=2,
                    source="service:authz-managed-rule-set-reconcile",
                    updated_at=f"2026-07-18T00:00:0{suffix[-1]}Z",
                    policy=policy,
                )
                barrier.wait(timeout=5)
                return active_store.compare_and_write_authz_policy_record(
                    expected_record=initial_record,
                    replacement_record=replacement,
                    mutation=DbOnlyMutationRequest(
                        scope="github-actions:authz-concurrency",
                        route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                        idempotency_key=f"authz:concurrent:{suffix}",
                        request_fingerprint=f"fingerprint-{suffix}",
                        lease_owner=f"trace-{suffix}",
                        response_status_code=202,
                        response_trace_id=f"trace-{suffix}",
                        response_payload={"status": "accepted", "suffix": suffix},
                    ),
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reconcile(*arguments),
                            ((store, "writer-1"), (second_store, "writer-2")),
                        )
                    )
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["stale", "written"])
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 2)
        self.assertEqual(
            superseded_records, (initial_record.model_copy(update={"status": "superseded"}),)
        )

    def test_tenant_repository_classification_compare_write_rolls_back_with_idempotency(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            record = _tenant_repository_classification_record(revision=1)
            mutation = DbOnlyMutationRequest(
                scope="github-actions:tenant-classification",
                route_path="/v1/tenant-admission/repository-classifications/apply",
                idempotency_key="postgres-tenant-classification-rollback",
                request_fingerprint="postgres-tenant-classification-rollback-fingerprint",
                lease_owner="trace-postgres-tenant-classification-rollback",
                response_status_code=200,
                response_trace_id="trace-postgres-tenant-classification-rollback",
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

            records = store.list_tenant_repository_classification_records(
                repository_id=record.repository_id
            )
            idempotency_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(records, ())
        self.assertIsNone(idempotency_record)

    def test_tenant_repository_classification_compare_write_serializes_concurrent_updates(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _tenant_repository_classification_record(revision=1)
            store.write_tenant_repository_classification_record(revision_1)
            revision_2a = _tenant_repository_classification_record(
                revision=2,
                classification_kind="engineering",
                classified_at="2026-07-31T10:05:00Z",
                reason="postgres integration writer one",
                supersedes_record_id=revision_1.record_id,
            )
            revision_2b = _tenant_repository_classification_record(
                revision=2,
                classification_kind="tenant_ui",
                classified_at="2026-07-31T10:05:01Z",
                reason="postgres integration writer two",
                supersedes_record_id=revision_1.record_id,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def apply_revision(
                active_store: PostgresRecordStore,
                record: TenantRepositoryClassificationRecord,
                suffix: str,
            ) -> str:
                barrier.wait(timeout=5)
                try:
                    return active_store.compare_and_write_tenant_repository_classification_record(
                        record=record,
                        expected_current_record_id=revision_1.record_id,
                        mutation=DbOnlyMutationRequest(
                            scope="github-actions:tenant-classification",
                            route_path=("/v1/tenant-admission/repository-classifications/apply"),
                            idempotency_key=f"postgres-tenant-classification-{suffix}",
                            request_fingerprint=f"postgres-tenant-fingerprint-{suffix}",
                            lease_owner=f"trace-postgres-tenant-{suffix}",
                            response_status_code=200,
                            response_trace_id=f"trace-postgres-tenant-{suffix}",
                            response_payload={"status": "ok", "suffix": suffix},
                        ),
                    ).status
                except TenantRepositoryClassificationConflictError:
                    return "classification_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: apply_revision(*arguments),
                            (
                                (store, revision_2a, "writer-1"),
                                (second_store, revision_2b, "writer-2"),
                            ),
                        )
                    )
                records = store.list_tenant_repository_classification_records(
                    repository_id=revision_1.repository_id
                )
                idempotency_records = tuple(
                    record
                    for suffix in ("writer-1", "writer-2")
                    if (
                        record := store.read_idempotency_record(
                            scope="github-actions:tenant-classification",
                            route_path=("/v1/tenant-admission/repository-classifications/apply"),
                            idempotency_key=f"postgres-tenant-classification-{suffix}",
                        )
                    )
                    is not None
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["classification_conflict", "written"])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].classification_revision, 2)
        self.assertEqual(records[1], revision_1)
        self.assertEqual(len(idempotency_records), 1)
        self.assertEqual(idempotency_records[0].state, "completed")

    def test_repository_inventory_compare_write_rolls_back_with_idempotency(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _repository_inventory_record(revision=1)
            mutation = DbOnlyMutationRequest(
                scope="github-actions:repository-inventory",
                route_path="/v1/repository-inventory/apply",
                idempotency_key="postgres-repository-inventory-rollback",
                request_fingerprint="postgres-repository-inventory-rollback-fingerprint",
                lease_owner="trace-postgres-repository-inventory-rollback",
                response_status_code=200,
                response_trace_id="trace-postgres-repository-inventory-rollback",
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
                store.compare_and_write_repository_inventory_record(
                    record=record,
                    expected_current_record_id="",
                    mutation=mutation,
                )

            records = store.list_repository_inventory_records(repository_id=record.repository_id)
            idempotency_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(records, ())
        self.assertIsNone(idempotency_record)

    def test_repository_inventory_compare_write_serializes_concurrent_updates(self) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _repository_inventory_record(revision=1)
            store.write_repository_inventory_record(revision_1)
            revision_2a = _repository_inventory_record(
                revision=2,
                state="retired",
                recorded_at="2026-08-26T10:05:00Z",
                reason="postgres integration writer one",
                supersedes_record_id=revision_1.record_id,
            )
            revision_2b = _repository_inventory_record(
                revision=2,
                state="tracked",
                recorded_at="2026-08-26T10:05:01Z",
                reason="postgres integration writer two",
                supersedes_record_id=revision_1.record_id,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def apply_revision(
                active_store: PostgresRecordStore,
                record: RepositoryInventoryRecord,
                suffix: str,
            ) -> str:
                barrier.wait(timeout=5)
                try:
                    return active_store.compare_and_write_repository_inventory_record(
                        record=record,
                        expected_current_record_id=revision_1.record_id,
                        mutation=DbOnlyMutationRequest(
                            scope="github-actions:repository-inventory",
                            route_path="/v1/repository-inventory/apply",
                            idempotency_key=f"postgres-repository-inventory-{suffix}",
                            request_fingerprint=f"postgres-inventory-fingerprint-{suffix}",
                            lease_owner=f"trace-postgres-inventory-{suffix}",
                            response_status_code=200,
                            response_trace_id=f"trace-postgres-inventory-{suffix}",
                            response_payload={"status": "ok", "suffix": suffix},
                        ),
                    ).status
                except RepositoryInventoryConflictError:
                    return "inventory_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: apply_revision(*arguments),
                            (
                                (store, revision_2a, "writer-1"),
                                (second_store, revision_2b, "writer-2"),
                            ),
                        )
                    )
                records = store.list_repository_inventory_records(
                    repository_id=revision_1.repository_id
                )
                idempotency_records = tuple(
                    record
                    for suffix in ("writer-1", "writer-2")
                    if (
                        record := store.read_idempotency_record(
                            scope="github-actions:repository-inventory",
                            route_path="/v1/repository-inventory/apply",
                            idempotency_key=f"postgres-repository-inventory-{suffix}",
                        )
                    )
                    is not None
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["inventory_conflict", "written"])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].inventory_revision, 2)
        self.assertEqual(records[1], revision_1)
        self.assertEqual(len(idempotency_records), 1)
        self.assertEqual(idempotency_records[0].state, "completed")

    def test_repository_human_role_policy_compare_write_rolls_back_with_idempotency(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            record = _repository_human_role_policy_record(revision=1)
            mutation = DbOnlyMutationRequest(
                scope="github-actions:repository-human-role-policy",
                route_path="/v1/tenant-admission/repository-human-role-policies/apply",
                idempotency_key="postgres-role-policy-rollback",
                request_fingerprint="postgres-role-policy-rollback-fingerprint",
                lease_owner="trace-postgres-role-policy-rollback",
                response_status_code=202,
                response_trace_id="trace-postgres-role-policy-rollback",
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
                store.compare_and_write_repository_human_role_policy_record(
                    record=record,
                    expected_current_record_id="",
                    expected_current_role_policy_digest="",
                    mutation=mutation,
                )

            records = store.list_repository_human_role_policy_records(
                repository_id=record.repository_id,
                product=record.product,
                context=record.context,
            )
            idempotency_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(records, ())
        self.assertIsNone(idempotency_record)

    def test_repository_human_role_policy_compare_write_replays_revision_two_with_new_key(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _repository_human_role_policy_record(revision=1)
            store.write_repository_human_role_policy_record(revision_1)
            revision_2 = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903002,),
                effective_at="2026-08-01T10:05:00Z",
                reason="postgres integration revision two replay",
                supersedes_record_id=revision_1.record_id,
            )

            def mutation(*, suffix: str) -> DbOnlyMutationRequest:
                return DbOnlyMutationRequest(
                    scope="github-actions:repository-human-role-policy",
                    route_path="/v1/tenant-admission/repository-human-role-policies/apply",
                    idempotency_key=f"postgres-role-policy-revision-2-{suffix}",
                    request_fingerprint=f"postgres-role-policy-revision-2-{suffix}-fingerprint",
                    lease_owner=f"trace-postgres-role-policy-revision-2-{suffix}",
                    response_status_code=202,
                    response_trace_id=f"trace-postgres-role-policy-revision-2-{suffix}",
                    response_payload={"status": "ok", "suffix": suffix},
                    replay_response_payload={"status": "ok", "result": "replayed"},
                )

            written = store.compare_and_write_repository_human_role_policy_record(
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mutation=mutation(suffix="write"),
            )
            replayed = store.compare_and_write_repository_human_role_policy_record(
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mutation=mutation(suffix="replay"),
            )
            records = store.list_repository_human_role_policy_records(
                repository_id=revision_1.repository_id,
                product=revision_1.product,
                context=revision_1.context,
            )

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "exact_replay")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], revision_2)
        self.assertEqual(records[1].record_id, revision_1.record_id)
        self.assertEqual(records[1].status, "superseded")

    def test_repository_human_role_policy_compare_write_serializes_concurrent_updates(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _repository_human_role_policy_record(revision=1)
            store.write_repository_human_role_policy_record(revision_1)
            revision_2a = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903002,),
                effective_at="2026-08-01T10:05:00Z",
                reason="postgres integration compare writer one",
                supersedes_record_id=revision_1.record_id,
            )
            revision_2b = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903003,),
                effective_at="2026-08-01T10:05:01Z",
                reason="postgres integration compare writer two",
                supersedes_record_id=revision_1.record_id,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def apply_revision(
                active_store: PostgresRecordStore,
                record: RepositoryHumanRolePolicyRecord,
                suffix: str,
            ) -> str:
                barrier.wait(timeout=5)
                try:
                    return active_store.compare_and_write_repository_human_role_policy_record(
                        record=record,
                        expected_current_record_id=revision_1.record_id,
                        expected_current_role_policy_digest=revision_1.role_policy_digest,
                        mutation=DbOnlyMutationRequest(
                            scope="github-actions:repository-human-role-policy",
                            route_path=(
                                "/v1/tenant-admission/repository-human-role-policies/apply"
                            ),
                            idempotency_key=f"postgres-role-policy-{suffix}",
                            request_fingerprint=f"postgres-role-policy-fingerprint-{suffix}",
                            lease_owner=f"trace-postgres-role-policy-{suffix}",
                            response_status_code=202,
                            response_trace_id=f"trace-postgres-role-policy-{suffix}",
                            response_payload={"status": "ok", "suffix": suffix},
                        ),
                    ).status
                except RepositoryHumanRolePolicyConflictError:
                    return "role_policy_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: apply_revision(*arguments),
                            (
                                (store, revision_2a, "writer-1"),
                                (second_store, revision_2b, "writer-2"),
                            ),
                        )
                    )
                records = store.list_repository_human_role_policy_records(
                    repository_id=revision_1.repository_id,
                    product=revision_1.product,
                    context=revision_1.context,
                )
                idempotency_records = tuple(
                    record
                    for suffix in ("writer-1", "writer-2")
                    if (
                        record := store.read_idempotency_record(
                            scope="github-actions:repository-human-role-policy",
                            route_path=(
                                "/v1/tenant-admission/repository-human-role-policies/apply"
                            ),
                            idempotency_key=f"postgres-role-policy-{suffix}",
                        )
                    )
                    is not None
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["role_policy_conflict", "written"])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].role_policy_revision, 2)
        self.assertEqual(records[1].status, "superseded")
        self.assertEqual(records[1].record_id, revision_1.record_id)
        self.assertEqual(len(idempotency_records), 1)
        self.assertEqual(idempotency_records[0].state, "completed")

    def test_repository_human_role_policy_exact_concurrent_revision_replays(self) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _repository_human_role_policy_record(revision=1)
            store.write_repository_human_role_policy_record(revision_1)
            revision_2 = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903002,),
                effective_at="2026-08-01T10:05:00Z",
                reason="postgres integration exact replay",
                supersedes_record_id=revision_1.record_id,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_revision(active_store: PostgresRecordStore) -> str:
                barrier.wait(timeout=5)
                return active_store.write_repository_human_role_policy_record(revision_2)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(executor.map(write_revision, (store, second_store)))
                active_records = store.list_repository_human_role_policy_records(
                    repository_id=revision_1.repository_id,
                    product=revision_1.product,
                    context=revision_1.context,
                    status="active",
                )
                all_records = store.list_repository_human_role_policy_records(
                    repository_id=revision_1.repository_id,
                    product=revision_1.product,
                    context=revision_1.context,
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["replayed", "written"])
        self.assertEqual(active_records, (revision_2,))
        self.assertEqual(len(all_records), 2)

    def test_repository_human_role_policy_concurrent_revision_conflicts(self) -> None:
        with _store_for_fresh_head_database() as store:
            revision_1 = _repository_human_role_policy_record(revision=1)
            store.write_repository_human_role_policy_record(revision_1)
            revision_2a = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903002,),
                effective_at="2026-08-01T10:05:00Z",
                reason="postgres integration writer one",
                supersedes_record_id=revision_1.record_id,
            )
            revision_2b = _repository_human_role_policy_record(
                revision=2,
                repository_owner_github_ids=(903003,),
                effective_at="2026-08-01T10:05:01Z",
                reason="postgres integration writer two",
                supersedes_record_id=revision_1.record_id,
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_revision(
                active_store: PostgresRecordStore,
                record: RepositoryHumanRolePolicyRecord,
            ) -> str:
                barrier.wait(timeout=5)
                try:
                    return active_store.write_repository_human_role_policy_record(record)
                except RepositoryHumanRolePolicyConflictError:
                    return "role_policy_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: write_revision(*arguments),
                            ((store, revision_2a), (second_store, revision_2b)),
                        )
                    )
                active_records = store.list_repository_human_role_policy_records(
                    repository_id=revision_1.repository_id,
                    product=revision_1.product,
                    context=revision_1.context,
                    status="active",
                )
                all_records = store.list_repository_human_role_policy_records(
                    repository_id=revision_1.repository_id,
                    product=revision_1.product,
                    context=revision_1.context,
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["role_policy_conflict", "written"])
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].role_policy_revision, 2)
        self.assertEqual(len(all_records), 2)

    def test_tenant_technical_human_waiver_exact_concurrent_event_replays(self) -> None:
        with _store_for_fresh_head_database() as store:
            event = _tenant_technical_human_waiver_event_record()
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_event(active_store: PostgresRecordStore) -> str:
                barrier.wait(timeout=5)
                try:
                    return active_store.write_tenant_technical_human_waiver_event_record(event)
                except TenantTechnicalHumanWaiverEventConflictError:
                    return "waiver_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(executor.map(write_event, (store, second_store)))
                rows = store.list_tenant_technical_human_waiver_event_records(
                    repository_id=event.binding.repository_id,
                    binding_sha256=event.binding.binding_sha256,
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["replayed", "written"])
        self.assertEqual(rows, (event,))

    def test_tenant_technical_human_waiver_compare_write_replay_and_conflict(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(store)
            )
            envelope = _tenant_technical_human_waiver_envelope(
                classification=classification,
                role_policy=role_policy,
                authz_policy=authz_policy,
            )
            mutation = _tenant_technical_human_waiver_mutation(
                idempotency_key="postgres-waiver-create",
                request_fingerprint="postgres-waiver-create-fingerprint",
                response_trace_id="trace-postgres-waiver-create",
            )

            written = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=envelope,
                mutation=mutation,
            )
            replay = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(login="postgres-human-renamed"),
                envelope=envelope,
                mutation=mutation,
            )
            conflict = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=envelope,
                mutation=_tenant_technical_human_waiver_mutation(
                    idempotency_key=mutation.idempotency_key,
                    request_fingerprint="postgres-waiver-changed-fingerprint",
                    response_trace_id="trace-postgres-waiver-conflict",
                ),
            )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="901001",
                product="postgres-tenant-site",
                context="postgres-tenant-site",
            )
            idempotency_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(written.status, "written")
        self.assertIsNotNone(written.result)
        self.assertIsNotNone(written.event_record)
        assert written.result is not None
        assert written.event_record is not None
        self.assertEqual(written.result.path_result.state, "satisfied")
        self.assertEqual(written.event_record.recorded_at, written.event_record.occurred_at)
        self.assertEqual(replay.status, "replayed")
        self.assertIsNotNone(replay.idempotency_record)
        assert replay.idempotency_record is not None
        self.assertEqual(replay.idempotency_record.response_trace_id, mutation.response_trace_id)
        self.assertEqual(conflict.status, "idempotency_conflict")
        self.assertEqual(records, (written.event_record,))
        self.assertIsNotNone(idempotency_record)
        assert idempotency_record is not None
        self.assertEqual(idempotency_record.state, "completed")

    def test_tenant_technical_human_waiver_compare_write_rolls_back_with_idempotency(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(store)
            )
            mutation = _tenant_technical_human_waiver_mutation(
                idempotency_key="postgres-waiver-rollback",
                request_fingerprint="postgres-waiver-rollback-fingerprint",
                response_trace_id="trace-postgres-waiver-rollback",
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
                    envelope=_tenant_technical_human_waiver_envelope(
                        classification=classification,
                        role_policy=role_policy,
                        authz_policy=authz_policy,
                    ),
                    mutation=mutation,
                )

            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="901001",
                product="postgres-tenant-site",
                context="postgres-tenant-site",
            )
            idempotency_record = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(records, ())
        self.assertIsNone(idempotency_record)

    def test_tenant_technical_human_waiver_compare_write_create_and_revoke_cas(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(store)
            )
            created = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=_tenant_technical_human_waiver_envelope(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    source_event_id="comment-postgres-create-revoke",
                ),
                mutation=_tenant_technical_human_waiver_mutation(
                    idempotency_key="postgres-waiver-create-revoke",
                    request_fingerprint="postgres-waiver-create-revoke-fingerprint",
                    response_trace_id="trace-postgres-waiver-create-revoke",
                ),
            )
            assert created.event_record is not None
            revoked = store.compare_and_write_tenant_technical_human_waiver_event(
                identity=_tenant_technical_human_waiver_identity(),
                envelope=_tenant_technical_human_waiver_envelope(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    action="revoked",
                    source_event_id="comment-postgres-revoke",
                    reason="Owner revoked exact technical waiver.",
                    expected_current={
                        "schema_version": 1,
                        "waiver_id": created.event_record.waiver_id,
                        "event_digest": created.event_record.event_digest,
                    },
                ),
                mutation=_tenant_technical_human_waiver_mutation(
                    idempotency_key="postgres-waiver-revoke",
                    request_fingerprint="postgres-waiver-revoke-fingerprint",
                    response_trace_id="trace-postgres-waiver-revoke",
                ),
            )
            with self.assertRaises(TenantTechnicalHumanWaiverRevokeCurrentError):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=_tenant_technical_human_waiver_envelope(
                        classification=classification,
                        role_policy=role_policy,
                        authz_policy=authz_policy,
                        action="revoked",
                        source_event_id="comment-postgres-revoke-stale",
                        expected_current={
                            "schema_version": 1,
                            "waiver_id": created.event_record.waiver_id,
                            "event_digest": created.event_record.event_digest,
                        },
                    ),
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="postgres-waiver-revoke-stale",
                        request_fingerprint="postgres-waiver-revoke-stale-fingerprint",
                        response_trace_id="trace-postgres-waiver-revoke-stale",
                    ),
                )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="901001",
                product="postgres-tenant-site",
                context="postgres-tenant-site",
            )

        self.assertEqual(created.status, "written")
        self.assertEqual(revoked.status, "written")
        self.assertIsNotNone(revoked.result)
        assert revoked.result is not None
        self.assertEqual(revoked.result.path_result.state, "denied")
        self.assertEqual(len(records), 2)
        self.assertEqual(sorted(record.action for record in records), ["created", "revoked"])

    def test_tenant_technical_human_waiver_compare_write_rejects_authority_drift(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            role_policy = _repository_human_role_policy_record(
                revision=1,
                repository_owner_github_ids=(903002,),
                effective_at="2026-07-01T00:00:00Z",
            )
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(
                    store,
                    role_policy=role_policy,
                )
            )
            with self.assertRaises(TenantTechnicalHumanWaiverStaleAuthorityError):
                stale_envelope = _tenant_technical_human_waiver_envelope(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                )
                stale_envelope.expected_authority.classification_digest = "f" * 64
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(github_id=903002),
                    envelope=stale_envelope,
                    mutation=_tenant_technical_human_waiver_mutation(
                        idempotency_key="postgres-waiver-authority-drift",
                        request_fingerprint="postgres-waiver-authority-drift-fingerprint",
                        response_trace_id="trace-postgres-waiver-authority-drift",
                        scope="github-human-id|903002",
                    ),
                )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="901001",
                product="postgres-tenant-site",
                context="postgres-tenant-site",
            )
            failed_idempotency = store.read_idempotency_record(
                scope="github-human-id|903002",
                route_path="/v1/tenant-admission/technical-human-waivers/apply",
                idempotency_key="postgres-waiver-authority-drift",
            )

        self.assertEqual(records, ())
        self.assertIsNone(failed_idempotency)

    def test_tenant_technical_human_waiver_compare_write_rejects_superseded_classification(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(store)
            )
            store.write_tenant_repository_classification_record(
                _tenant_repository_classification_record(
                    revision=2,
                    product="replacement-product",
                    context="replacement-context",
                    classification_kind="engineering",
                    classified_at="2026-07-02T00:00:00Z",
                    supersedes_record_id=classification.record_id,
                )
            )
            mutation = _tenant_technical_human_waiver_mutation(
                idempotency_key="postgres-waiver-superseded-classification",
                request_fingerprint="postgres-waiver-superseded-classification-fingerprint",
                response_trace_id="trace-postgres-waiver-superseded-classification",
            )

            with self.assertRaisesRegex(
                TenantTechnicalHumanWaiverStaleAuthorityError,
                "classification does not match candidate",
            ):
                store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=_tenant_technical_human_waiver_identity(),
                    envelope=_tenant_technical_human_waiver_envelope(
                        classification=classification,
                        role_policy=role_policy,
                        authz_policy=authz_policy,
                    ),
                    mutation=mutation,
                )
            records = store.list_tenant_technical_human_waiver_event_records(
                repository_id="901001",
                product="postgres-tenant-site",
                context="postgres-tenant-site",
            )
            failed_idempotency = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(records, ())
        self.assertIsNone(failed_idempotency)

    def test_tenant_technical_human_waiver_compare_write_serializes_no_row_race(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            classification, role_policy, authz_policy = (
                _seed_tenant_technical_human_waiver_authority(store)
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def write_waiver(active_store: PostgresRecordStore, suffix: str) -> str:
                barrier.wait(timeout=5)
                try:
                    result = active_store.compare_and_write_tenant_technical_human_waiver_event(
                        identity=_tenant_technical_human_waiver_identity(),
                        envelope=_tenant_technical_human_waiver_envelope(
                            classification=classification,
                            role_policy=role_policy,
                            authz_policy=authz_policy,
                            source_event_id=f"comment-postgres-race-{suffix}",
                        ),
                        mutation=_tenant_technical_human_waiver_mutation(
                            idempotency_key=f"postgres-waiver-race-{suffix}",
                            request_fingerprint=f"postgres-waiver-race-{suffix}-fingerprint",
                            response_trace_id=f"trace-postgres-waiver-race-{suffix}",
                        ),
                    )
                    return result.status
                except TenantTechnicalHumanWaiverEventConflictError:
                    return "waiver_conflict"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: write_waiver(*arguments),
                            ((store, "a"), (second_store, "b")),
                        )
                    )
                records = store.list_tenant_technical_human_waiver_event_records(
                    repository_id="901001",
                    product="postgres-tenant-site",
                    context="postgres-tenant-site",
                )
            finally:
                second_store.close()

        self.assertEqual(statuses.count("written"), 1)
        self.assertEqual(statuses.count("waiver_conflict"), 1)
        self.assertEqual(len(records), 1)

    def test_trusted_maintenance_capture_serializes_signed_body_replay(self) -> None:
        with _store_for_fresh_head_database() as store:
            classification = _trusted_maintenance_classification()
            policy = _trusted_maintenance_policy(evidence_ttl_seconds=3600)
            candidate = _trusted_maintenance_candidate()
            expected_authority = TrustedMaintenanceExpectedAuthority(
                classification_record_id=classification.record_id,
                classification_revision=classification.classification_revision,
                classification_digest=classification.classification_digest,
                policy_record_id=policy.record_id,
                policy_revision=policy.policy_revision,
                policy_digest=policy.policy_digest,
            )
            store.write_tenant_repository_classification_record(classification)
            store.write_trusted_maintenance_policy_record(policy)
            database_url = store.database_url
            barrier = threading.Barrier(2)
            event_facts = (
                _trusted_maintenance_event_facts(
                    delivery_id="postgres-delivery-a",
                    signed_payload_sha256="d" * 64,
                ),
                _trusted_maintenance_event_facts(
                    delivery_id="postgres-delivery-b",
                    signed_payload_sha256="d" * 64,
                    pr_author_login="renamed-automation",
                    sender_login="renamed-sender",
                ),
            )

            def capture_once(facts: TrustedMaintenanceGitHubEventFacts) -> str:
                active_store = PostgresRecordStore(database_url=database_url)
                try:
                    barrier.wait(timeout=5)
                    return active_store.capture_trusted_maintenance_evidence_transactionally(
                        candidate=candidate,
                        expected_authority=expected_authority,
                        event_facts=facts,
                    )
                finally:
                    active_store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(capture_once, event_facts))

            records = store.list_trusted_maintenance_evidence_records(
                repository_id=candidate.repository_id,
                pull_request_number=candidate.pull_request_number,
                head_sha=candidate.head_sha,
            )
            self.assertEqual(set(results), {"written", "replayed"})
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].binding.signed_payload_sha256, "d" * 64)
            self.assertIn(
                records[0].binding.delivery_id,
                {"postgres-delivery-a", "postgres-delivery-b"},
            )

            conflicting_facts = _trusted_maintenance_event_facts(
                delivery_id="postgres-delivery-c",
                signed_payload_sha256="d" * 64,
                event_action="opened",
            )
            with self.assertRaises(TrustedMaintenanceEvidenceConflictError):
                store.capture_trusted_maintenance_evidence_transactionally(
                    candidate=candidate,
                    expected_authority=expected_authority,
                    event_facts=conflicting_facts,
                )
            self.assertEqual(
                len(
                    store.list_trusted_maintenance_evidence_records(
                        repository_id=candidate.repository_id,
                        pull_request_number=candidate.pull_request_number,
                        head_sha=candidate.head_sha,
                    )
                ),
                1,
            )

    def test_concurrent_outbox_enqueue_reuses_one_delivery(self) -> None:
        with _store_for_fresh_head_database() as store:
            delivery = _outbox_delivery(suffix="concurrent-enqueue")
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def enqueue(active_store: PostgresRecordStore) -> OutboxDeliveryRecord:
                barrier.wait(timeout=5)
                return active_store.enqueue_outbox_delivery_with_idempotency(
                    OutboxWithIdempotencyRequest(delivery=delivery)
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(enqueue, store)
                    second_future = executor.submit(enqueue, second_store)
                    first = first_future.result(timeout=10)
                    second = second_future.result(timeout=10)
                rows = store.list_outbox_delivery_records()
            finally:
                second_store.close()

        self.assertEqual(first.delivery_id, delivery.delivery_id)
        self.assertEqual(second.delivery_id, delivery.delivery_id)
        self.assertEqual([row.delivery_id for row in rows], [delivery.delivery_id])

    def test_lane_summary_waits_for_authority_bundle_commit(self) -> None:
        with _store_for_fresh_head_database() as store:
            bundle_step_reached = threading.Event()
            release_bundle = threading.Event()
            read_started = threading.Event()
            read_finished = threading.Event()
            errors: list[BaseException] = []

            class _BlockingStore(PostgresRecordStore):
                def _after_product_authority_bundle_step(self, step_name: str) -> None:
                    if step_name == "write_product_profile":
                        bundle_step_reached.set()
                        if not release_bundle.wait(timeout=5):
                            raise TimeoutError("timed out waiting to release authority bundle")

            blocking_store = _BlockingStore(database_url=store.database_url)
            reader_store = PostgresRecordStore(database_url=store.database_url)

            def write_bundle() -> None:
                try:
                    blocking_store.write_product_authority_bundle(
                        ProductAuthorityBundle(product_profiles=(_product_profile(),))
                    )
                except BaseException as exc:
                    errors.append(exc)

            def read_lane_summary() -> None:
                read_started.set()
                try:
                    reader_store.read_lane_summary(
                        context_name="example-product",
                        instance_name="prod",
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    read_finished.set()

            writer_thread = threading.Thread(target=write_bundle)
            reader_thread = threading.Thread(target=read_lane_summary)
            writer_thread.start()
            try:
                self.assertTrue(bundle_step_reached.wait(timeout=5))
                reader_thread.start()
                self.assertTrue(read_started.wait(timeout=5))
                self.assertFalse(read_finished.wait(timeout=0.1))
            finally:
                release_bundle.set()
                writer_thread.join(timeout=5)
                if reader_thread.ident is not None:
                    reader_thread.join(timeout=5)
                blocking_store.close()
                reader_store.close()

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(reader_thread.is_alive())
        self.assertEqual(errors, [])

    def test_every_code_two_workers_claim_exactly_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def claim(
                active_store: PostgresRecordStore, host: str
            ) -> EveryCodeWorkRequestRecord | None:
                barrier.wait(timeout=5)
                return active_store.claim_every_code_work_request_record(
                    request_id=record.request_id,
                    host=host,
                    claimed_at="2026-07-13T09:01:00Z",
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = (
                        executor.submit(claim, store, "worker-a"),
                        executor.submit(claim, second_store, "worker-b"),
                    )
                    results = tuple(future.result(timeout=10) for future in futures)
                loaded = store.read_every_code_work_request_record(record.request_id)
            finally:
                second_store.close()

        claims = tuple(result for result in results if result is not None)
        self.assertEqual(len(claims), 1)
        self.assertEqual(loaded.claimed_by_host, claims[0].claimed_by_host)
        self.assertEqual(loaded.fencing_token, 1)
        self.assertEqual(loaded.attempt, 1)

    def test_every_code_heartbeat_and_stale_recovery_are_fenced(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
                lease_seconds=60,
            )
            assert claimed is not None
            stale_snapshot = store.list_stale_every_code_work_request_records(
                as_of="2026-07-13T09:03:00Z"
            )[0]
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def heartbeat() -> bool:
                barrier.wait(timeout=5)
                return store.heartbeat_every_code_work_request_record(
                    request_id=record.request_id,
                    host="worker-a",
                    fencing_token=claimed.fencing_token,
                    heartbeat_at="2026-07-13T09:02:30Z",
                    lease_expires_at="2026-07-13T09:12:30Z",
                )

            def recover() -> EveryCodeWorkRequestRecord | None:
                barrier.wait(timeout=5)
                return second_store.recover_stale_every_code_work_request_record(
                    expected_record=stale_snapshot,
                    recovered_at="2026-07-13T09:03:00Z",
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    heartbeat_future = executor.submit(heartbeat)
                    recover_future = executor.submit(recover)
                    heartbeat_result = heartbeat_future.result(timeout=10)
                    recovery_result = recover_future.result(timeout=10)
                loaded = store.read_every_code_work_request_record(record.request_id)
            finally:
                second_store.close()

        self.assertNotEqual(heartbeat_result, recovery_result is not None)
        if heartbeat_result:
            self.assertIsNone(recovery_result)
            self.assertEqual(loaded.state, "claimed")
            self.assertEqual(loaded.lease_expires_at, "2026-07-13T09:12:30Z")
        else:
            self.assertIsNotNone(recovery_result)
            self.assertEqual(loaded.state, "queued")

    def test_every_code_status_update_rejects_stale_fencing_token(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
            )
            assert claimed is not None
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with self.assertRaisesRegex(ValueError, "fencing token"):
                    second_store.update_every_code_work_request_status_record(
                        request_id=record.request_id,
                        update=EveryCodeWorkRequestStatusUpdate(
                            state="done",
                            host="worker-a",
                            fencing_token=claimed.fencing_token + 1,
                            updated_at="2026-07-13T09:02:00Z",
                            result_summary="stale completion",
                        ),
                    )
                completed = store.update_every_code_work_request_status_record(
                    request_id=record.request_id,
                    update=EveryCodeWorkRequestStatusUpdate(
                        state="done",
                        host="worker-a",
                        fencing_token=claimed.fencing_token,
                        updated_at="2026-07-13T09:02:00Z",
                        result_summary="completed",
                    ),
                )
            finally:
                second_store.close()

        self.assertEqual(completed.state, "done")

    def test_every_code_pr_close_waits_for_fresh_finish_and_preserves_higher_fence(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            stale_claim = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-old",
                claimed_at="2026-07-13T09:01:00Z",
                lease_seconds=60,
            )
            assert stale_claim is not None
            recovered = store.recover_stale_every_code_work_request_record(
                expected_record=stale_claim,
                recovered_at="2026-07-13T09:03:00Z",
            )
            assert recovered is not None
            fresh_claim = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-new",
                claimed_at="2026-07-13T09:04:00Z",
            )
            assert fresh_claim is not None

            finish_row_synced = threading.Event()
            release_finish = threading.Event()
            close_started = threading.Event()
            close_finished = threading.Event()
            finish_store = _BlockingEveryCodeFinishStore(
                database_url=store.database_url,
                finish_row_synced=finish_row_synced,
                release_finish=release_finish,
            )
            close_store = PostgresRecordStore(database_url=store.database_url)

            def finish_fresh_claim() -> EveryCodeWorkRequestRecord:
                return finish_store.update_every_code_work_request_status_record(
                    request_id=record.request_id,
                    update=EveryCodeWorkRequestStatusUpdate(
                        state="done",
                        host="worker-new",
                        fencing_token=fresh_claim.fencing_token,
                        updated_at="2026-07-13T09:05:00Z",
                        result_pr_url="https://github.com/cbusillo/code/pull/1700",
                        result_summary="Worker completed from the newer claim.",
                    ),
                )

            def close_pull_request() -> EveryCodeWorkRequestRecord | None:
                close_started.set()
                try:
                    return close_store.close_every_code_work_request_for_pull_request_record(
                        request_id=stale_claim.request_id,
                        expected_lifecycle_id=fresh_claim.lifecycle_id,
                        pr_url="https://github.com/cbusillo/code/pull/1700",
                        merged=True,
                        closed_at="2026-07-13T09:06:00Z",
                    )
                finally:
                    close_finished.set()

            executor = ThreadPoolExecutor(max_workers=2)
            try:
                finish_future = executor.submit(finish_fresh_claim)
                self.assertTrue(finish_row_synced.wait(timeout=5))
                close_future = executor.submit(close_pull_request)
                self.assertTrue(close_started.wait(timeout=5))
                self.assertFalse(close_finished.wait(timeout=0.1))
                release_finish.set()
                finished = finish_future.result(timeout=10)
                closed = close_future.result(timeout=10)
            finally:
                release_finish.set()
                executor.shutdown(wait=True)
                finish_store.close()
                close_store.close()

            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertEqual(stale_claim.fencing_token, 1)
        self.assertNotEqual(stale_claim.lifecycle_id, fresh_claim.lifecycle_id)
        self.assertEqual(finished.fencing_token, 2)
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.state, "done")
        self.assertEqual(closed.lifecycle_id, fresh_claim.lifecycle_id)
        self.assertEqual(closed.fencing_token, 2)
        self.assertEqual(closed.claimed_by_host, "worker-new")
        self.assertEqual(
            closed.result_summary,
            "Linked pull request merged: https://github.com/cbusillo/code/pull/1700\n"
            "Worker completed from the newer claim.",
        )
        self.assertEqual(loaded, closed)

    def test_every_code_claim_commits_replay_evidence_atomically(self) -> None:
        with _store_for_fresh_head_database() as store:
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
                    idempotency_key="every-code-claim-1693",
                    request_fingerprint="every-code-claim-fingerprint",
                    response_status_code=202,
                    response_trace_id="trace-every-code-claim",
                    recorded_at="2026-07-13T09:01:00Z",
                    response_payload={
                        "status": "accepted",
                        "result": {"request": claimed_record.model_dump(mode="json")},
                    },
                )

            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
                idempotency_record_factory=idempotency_record_factory,
            )
            replay_evidence = store.read_idempotency_record(
                scope="terminal-agent:every-code-worker",
                route_path="/v1/every-code/work-requests/claim",
                idempotency_key="every-code-claim-1693",
            )
            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(replay_evidence)
        self.assertEqual(loaded.state, "claimed")
        assert replay_evidence is not None
        self.assertEqual(replay_evidence.state, "completed")

    def test_outbox_claim_skips_locked_pending_row_and_claims_next_delivery(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = _outbox_delivery(suffix="first").model_copy(
                update={
                    "created_at": "2026-07-13T00:00:00Z",
                    "updated_at": "2026-07-13T00:00:00Z",
                    "next_attempt_at": "2026-07-13T00:00:00Z",
                }
            )
            second = _outbox_delivery(suffix="second").model_copy(
                update={
                    "created_at": "2026-07-13T00:00:01Z",
                    "updated_at": "2026-07-13T00:00:01Z",
                    "next_attempt_at": "2026-07-13T00:00:00Z",
                }
            )
            store.write_outbox_delivery_record(first)
            store.write_outbox_delivery_record(second)
            blocker = create_engine(store.database_url)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with blocker.connect() as connection:
                    transaction = connection.begin()
                    try:
                        locked_delivery_id = connection.execute(
                            text(
                                "select delivery_id from launchplane_outbox_deliveries "
                                "where delivery_id = :delivery_id for update"
                            ),
                            {"delivery_id": first.delivery_id},
                        ).scalar_one()
                        claimed = second_store.claim_next_outbox_delivery_record(
                            lease_owner="worker-b",
                            now="2026-07-13T00:00:02Z",
                        )
                    finally:
                        transaction.rollback()
                first_claim = store.claim_next_outbox_delivery_record(
                    lease_owner="worker-a",
                    now="2026-07-13T00:00:03Z",
                )
            finally:
                second_store.close()
                blocker.dispose()

        self.assertEqual(locked_delivery_id, first.delivery_id)
        self.assertEqual(claimed.status, "claimed")
        assert claimed.record is not None
        self.assertEqual(claimed.record.delivery_id, second.delivery_id)
        self.assertEqual(claimed.record.lease_owner, "worker-b")
        self.assertEqual(first_claim.status, "claimed")
        assert first_claim.record is not None
        self.assertEqual(first_claim.record.delivery_id, first.delivery_id)
        self.assertEqual(first_claim.record.lease_owner, "worker-a")

    def test_outbox_enqueue_with_idempotency_is_atomic_on_validation_failure(self) -> None:
        with _store_for_fresh_head_database() as store:
            delivery = _outbox_delivery(suffix="atomic-validation")
            invalid_reservation = _mutation_reservation(
                lease_owner="worker-a",
                idempotency_key="product-preview-tls:postgres:outbox-invalid",
            )
            with self.assertRaisesRegex(ValueError, "completed replay evidence"):
                store.enqueue_outbox_delivery_with_idempotency(
                    OutboxWithIdempotencyRequest(
                        delivery=delivery,
                        idempotency_record=invalid_reservation,
                    )
                )
            rows = store.list_outbox_delivery_records()
            stored_reservation = store.read_idempotency_record(
                scope=invalid_reservation.scope,
                route_path=invalid_reservation.route_path,
                idempotency_key=invalid_reservation.idempotency_key,
            )

        self.assertEqual(rows, ())
        self.assertIsNone(stored_reservation)

    def test_two_connections_claim_exactly_one_pending_operation_and_recover_lease(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            store.write_odoo_stable_bootstrap_operation_record(_bootstrap_operation())

            first_claim = store.claim_next_odoo_stable_bootstrap_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:10:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                second_claim = second_store.claim_next_odoo_stable_bootstrap_operation_record(
                    lease_owner="worker-b",
                    lease_expires_at="2026-05-17T00:11:00Z",
                    claimed_at="2026-05-17T00:02:00Z",
                )
                stale_owner_heartbeat = (
                    second_store.heartbeat_odoo_stable_bootstrap_operation_record(
                        operation_id=first_claim.operation_id if first_claim else "missing",
                        lease_owner="worker-b",
                        heartbeat_at="2026-05-17T00:03:00Z",
                        lease_expires_at="2026-05-17T00:13:00Z",
                    )
                )
                recovered_ids = store.recover_expired_odoo_stable_bootstrap_operation_records(
                    now="2026-05-17T00:12:00Z",
                    safe_phases=("running",),
                    max_attempts=3,
                )
                recovered_claim = second_store.claim_next_odoo_stable_bootstrap_operation_record(
                    lease_owner="worker-b",
                    lease_expires_at="2026-05-17T00:22:00Z",
                    claimed_at="2026-05-17T00:13:00Z",
                )
            finally:
                second_store.close()

        self.assertIsNotNone(first_claim)
        assert first_claim is not None
        self.assertEqual(first_claim.lease_owner, "worker-a")
        self.assertIsNone(second_claim)
        self.assertFalse(stale_owner_heartbeat)
        self.assertEqual(recovered_ids, (first_claim.operation_id,))
        self.assertIsNotNone(recovered_claim)
        assert recovered_claim is not None
        self.assertEqual(recovered_claim.lease_owner, "worker-b")
        self.assertEqual(recovered_claim.attempt, 2)

    def test_pending_operation_cancel_and_claim_are_mutually_exclusive(self) -> None:
        with _store_for_fresh_head_database() as store:
            pending_record = _bootstrap_operation()
            store.write_odoo_stable_bootstrap_operation_record(pending_record)
            cancelled_record = OdooStableBootstrapOperationRecord.model_validate(
                {
                    **pending_record.model_dump(mode="json"),
                    "status": "cancelled",
                    "phase": "cancelled",
                    "updated_at": "2026-07-23T03:32:00Z",
                    "finished_at": "2026-07-23T03:32:00Z",
                    "cancellation": durable_operation_cancellation_payload(),
                }
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def cancel() -> bool:
                barrier.wait(timeout=5)
                return store.cancel_pending_odoo_stable_bootstrap_operation_record(cancelled_record)

            def claim() -> OdooStableBootstrapOperationRecord | None:
                barrier.wait(timeout=5)
                return second_store.claim_next_odoo_stable_bootstrap_operation_record(
                    lease_owner="worker-a",
                    lease_expires_at="2026-07-23T03:40:00Z",
                    claimed_at="2026-07-23T03:35:00Z",
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    cancel_future = executor.submit(cancel)
                    claim_future = executor.submit(claim)
                    cancellation_committed = cancel_future.result(timeout=10)
                    claimed_record = claim_future.result(timeout=10)
                loaded = store.read_odoo_stable_bootstrap_operation_record(
                    pending_record.operation_id
                )
            finally:
                second_store.close()

        self.assertNotEqual(cancellation_committed, claimed_record is not None)
        if cancellation_committed:
            self.assertEqual(loaded.status, "cancelled")
        else:
            self.assertIsNotNone(claimed_record)
            self.assertEqual(loaded.status, "running")

    def test_merge_train_controller_lease_blocks_other_owner_until_expiry(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                first = store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-a",
                    lease_seconds=30,
                    initial_active_action="postgres_store_test",
                    initial_active_phase="acquire",
                    adoptable_active_actions=("postgres_store_test",),
                )
                with self.assertRaisesRegex(
                    MergeTrainControllerLeaseHeldError,
                    "held by another owner",
                ):
                    second_store.acquire_merge_train_controller_state_record(
                        repository="cbusillo/sellyouroutboard",
                        base_branch="main",
                        policy_key="cbusillo/sellyouroutboard:main",
                        policy_sha256="policy-sha",
                        lease_owner="controller-b",
                        lease_seconds=30,
                        initial_active_action="postgres_store_test",
                        initial_active_phase="acquire",
                        adoptable_active_actions=("postgres_store_test",),
                    )
            finally:
                second_store.close()

        self.assertEqual(first.lease_owner, "controller-a")

    def test_merge_train_controller_takeover_fences_stale_owner(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                first = store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-a",
                    lease_seconds=1,
                    initial_active_action="postgres_store_test",
                    initial_active_phase="acquire",
                    adoptable_active_actions=("postgres_store_test",),
                )
                time.sleep(1.1)
                renewed = second_store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-b",
                    lease_seconds=30,
                    initial_active_action="postgres_store_test",
                    initial_active_phase="acquire",
                    adoptable_active_actions=("postgres_store_test",),
                )
                with self.assertRaisesRegex(
                    MergeTrainControllerLeaseLostError,
                    "lease owner changed",
                ):
                    store.compare_and_set_merge_train_controller_state_record(
                        record=first.model_copy(
                            update={
                                "active_action": "land_batch",
                                "active_phase": "merge_batch_entries",
                            }
                        ),
                        expected_lease_owner="controller-a",
                        expected_lease_acquired_at=first.lease_acquired_at,
                        lease_seconds=30,
                    )
                stored = second_store.read_merge_train_controller_state_record(
                    renewed.controller_key
                )
            finally:
                second_store.close()

        self.assertEqual(renewed.lease_owner, "controller-b")
        self.assertEqual(stored.lease_owner, "controller-b")

    def test_merge_train_controller_concurrent_acquire_has_one_owner(self) -> None:
        with _store_for_fresh_head_database() as store:
            barrier = threading.Barrier(2)

            def acquire(owner: str) -> MergeTrainControllerStateRecord | BaseException:
                contender = PostgresRecordStore(database_url=store.database_url)
                try:
                    barrier.wait(timeout=5)
                    return contender.acquire_merge_train_controller_state_record(
                        repository="cbusillo/sellyouroutboard",
                        base_branch="main",
                        policy_key="cbusillo/sellyouroutboard:main",
                        policy_sha256="policy-sha",
                        lease_owner=owner,
                        lease_seconds=30,
                        initial_active_action="postgres_store_test",
                        initial_active_phase="acquire",
                        adoptable_active_actions=("postgres_store_test",),
                    )
                except BaseException as error:  # noqa: BLE001
                    return error
                finally:
                    contender.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(acquire, ("controller-a", "controller-b")))
            winners = tuple(
                result for result in results if isinstance(result, MergeTrainControllerStateRecord)
            )
            conflicts = tuple(
                result
                for result in results
                if isinstance(result, MergeTrainControllerLeaseHeldError)
            )

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)

    def test_merge_train_controller_expired_active_phase_is_adopted(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-a",
                lease_seconds=1,
                initial_active_action="postgres_store_test",
                initial_active_phase="acquire",
                adoptable_active_actions=("postgres_store_test",),
            )
            checkpointed = store.compare_and_set_merge_train_controller_state_record(
                record=first.model_copy(
                    update={
                        "active_action": "land_batch",
                        "active_phase": "cleanup_candidate_ref",
                        "active_record_id": "landing-record",
                        "step_payload": {"landing_plan_record_id": "landing-record"},
                    }
                ),
                expected_lease_owner=first.lease_owner,
                expected_lease_acquired_at=first.lease_acquired_at,
                lease_seconds=1,
            )
            time.sleep(1.1)
            adopted = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-b",
                lease_seconds=30,
                initial_active_action="postgres_store_test",
                initial_active_phase="acquire",
                adoptable_active_actions=("postgres_store_test", "land_batch"),
            )

        self.assertEqual(checkpointed.active_phase, "cleanup_candidate_ref")
        self.assertEqual(adopted.lease_owner, "controller-b")
        self.assertEqual(adopted.reconciliation_status, "adopted")
        self.assertEqual(adopted.active_record_id, "landing-record")

    def test_merge_train_controller_operator_required_state_is_retried_by_service(self) -> None:
        with _store_for_fresh_head_database() as store:
            acquired = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-a",
                lease_seconds=30,
                initial_active_action="postgres_store_test",
                initial_active_phase="acquire",
                adoptable_active_actions=("postgres_store_test",),
            )
            store.compare_and_set_merge_train_controller_state_record(
                record=acquired.model_copy(
                    update={
                        "status": "reconcile_required",
                        "lease_owner": "",
                        "lease_acquired_at": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "active_action": "execute_stack_collapse",
                        "active_phase": "merge_stack_branches",
                        "reconciliation_status": "required",
                        "reconciliation_detail": ("operator_required:github_request_rejected"),
                    }
                ),
                expected_lease_owner=acquired.lease_owner,
                expected_lease_acquired_at=acquired.lease_acquired_at,
                lease_seconds=30,
            )

            adopted = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-b",
                lease_seconds=30,
                initial_active_action="postgres_store_test",
                initial_active_phase="acquire",
                adoptable_active_actions=(
                    "postgres_store_test",
                    "execute_stack_collapse",
                ),
            )

        self.assertEqual(adopted.lease_owner, "controller-b")
        self.assertEqual(adopted.reconciliation_status, "adopted")
        self.assertEqual(adopted.active_phase, "merge_stack_branches")
        self.assertIn("operator_required:github_request_rejected", adopted.reconciliation_detail)

    def test_merge_train_controller_rejects_foreign_action_without_rewrite(self) -> None:
        with _store_for_fresh_head_database() as store:
            acquired = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="merge-train-policy",
                policy_sha256="merge-train-policy-sha",
                lease_owner="controller-a",
                lease_seconds=30,
                initial_active_action="merge_train_controller_run_once",
                initial_active_phase="select_next_action",
                adoptable_active_actions=("merge_train_controller_run_once",),
            )
            foreign_state = store.compare_and_set_merge_train_controller_state_record(
                record=acquired.model_copy(
                    update={
                        "status": "reconcile_required",
                        "lease_owner": "",
                        "lease_acquired_at": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "active_action": "land_batch",
                        "active_phase": "confirm_merge",
                        "reconciliation_status": "required",
                        "reconciliation_detail": "retryable:land_batch:confirm_merge",
                    }
                ),
                expected_lease_owner=acquired.lease_owner,
                expected_lease_acquired_at=acquired.lease_acquired_at,
                lease_seconds=30,
            )

            with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="tenant-policy",
                    policy_sha256="tenant-policy-sha",
                    lease_owner="controller-b",
                    lease_seconds=30,
                    initial_active_action="tenant_admission_merge",
                    initial_active_phase="evaluate_candidate",
                    adoptable_active_actions=("tenant_admission_merge",),
                )
            observed = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
            )[0]

        self.assertEqual(observed, foreign_state)

    def test_row_lock_blocks_stale_owner_completion_until_claim_commits(self) -> None:
        with _store_for_fresh_head_database() as store:
            store.write_odoo_stable_bootstrap_operation_record(_bootstrap_operation())
            blocker = create_engine(store.database_url)
            stale_owner_result: list[bool | BaseException] = []
            worker_started = threading.Event()
            worker: threading.Thread | None = None
            try:
                with blocker.connect() as connection:
                    transaction = connection.begin()
                    try:
                        locked_row = connection.execute(
                            text(
                                "select operation_id "
                                "from launchplane_odoo_stable_bootstrap_operations "
                                "where status = 'pending' "
                                "for update"
                            )
                        ).fetchone()
                        self.assertIsNotNone(locked_row)
                        worker = threading.Thread(
                            target=_attempt_stale_owner_completion,
                            args=(store.database_url, stale_owner_result, worker_started),
                        )
                        worker.start()
                        self.assertTrue(worker_started.wait(timeout=5))
                        worker.join(timeout=5)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(stale_owner_result), 1)
                        lock_error = stale_owner_result[0]
                        self.assertIsInstance(lock_error, OperationalError)
                        assert isinstance(lock_error, OperationalError)
                        self.assertEqual(getattr(lock_error.orig, "sqlstate", ""), "55P03")
                    finally:
                        transaction.rollback()
                post_lock_result = store.complete_odoo_stable_bootstrap_operation_record(
                    record=_bootstrap_operation(
                        status="pass",
                        phase="completed",
                        updated_at="2026-05-17T00:04:00Z",
                        finished_at="2026-05-17T00:04:00Z",
                    ),
                    lease_owner="stale-worker",
                )
            finally:
                blocker.dispose()

        self.assertFalse(post_lock_result)

    def test_idempotency_unique_index_rejects_conflicting_two_connection_insert(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            first_record = _idempotency_record(
                response_trace_id="launchplane_req_first",
                request_fingerprint="fingerprint-first",
            )
            conflicting_record = _idempotency_record(
                response_trace_id="launchplane_req_second",
                request_fingerprint="fingerprint-second",
            )
            store.write_idempotency_record(first_record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with self.assertRaises(IntegrityError):
                    second_store.write_idempotency_record(conflicting_record)
                loaded = second_store.read_idempotency_record(
                    scope=first_record.scope,
                    route_path=first_record.route_path,
                    idempotency_key=first_record.idempotency_key,
                )
            finally:
                second_store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.request_fingerprint, "fingerprint-first")

    def test_two_store_instances_reserve_same_key_once_and_conflict_deterministically(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def reserve(
                active_store: PostgresRecordStore,
                reservation: LaunchplaneIdempotencyRecord,
            ) -> str:
                barrier.wait()
                return _reserve_mutation(active_store, reservation).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reserve(*arguments),
                            (
                                (store, _mutation_reservation(lease_owner="worker-a")),
                                (second_store, _mutation_reservation(lease_owner="worker-b")),
                            ),
                        )
                    )
                conflict = _reserve_mutation(
                    second_store,
                    _mutation_reservation(
                        lease_owner="worker-c",
                        request_fingerprint="mutation-fingerprint-b",
                    ),
                )
                stored = store.read_idempotency_record(
                    scope="github-actions|cbusillo/launchplane|workflow:test",
                    route_path="/v1/product-profiles/preview-tls/apply",
                    idempotency_key="product-preview-tls:postgres:1",
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["acquired", "in_progress"])
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "running")
        self.assertEqual(stored.attempt, 1)

    def test_expired_reconciliation_key_transitions_to_reconcile_required(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = _mutation_reservation(lease_owner="worker-a")
            clock = {"now": "2026-07-13T00:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(store, reservation)
                clock["now"] = "2026-07-13T00:01:00Z"
                bound = store.bind_mutation_reconciliation_key(
                    reservation=acquired.record,
                    reconciliation_key="provider-operation-123",
                )
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with patch.object(
                    second_store,
                    "_database_mutation_timestamp",
                    return_value="2026-07-13T00:06:00Z",
                ):
                    reconciled = _reserve_mutation(
                        second_store,
                        _mutation_reservation(lease_owner="worker-b"),
                    )
            finally:
                second_store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(bound.status, "updated")
        self.assertEqual(reconciled.status, "reconcile_required")
        self.assertEqual(reconciled.record.state, "reconcile_required")
        self.assertEqual(reconciled.record.reconciliation_key, "provider-operation-123")

    def test_expired_reclaim_fences_stale_attempt_across_store_instances(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            reservation = _mutation_reservation(lease_owner="worker-reused")
            try:
                acquired = _reserve_mutation(store, reservation, lease_seconds=1)
                time.sleep(1.1)
                reclaimed = _reserve_mutation(
                    second_store,
                    _mutation_reservation(lease_owner="worker-reused"),
                )
                stale_completion = _mutation_completion(
                    acquired.record,
                    response_trace_id="trace-stale-attempt",
                )
                stale_result = store.complete_mutation_reservation(
                    completion=stale_completion,
                )
            finally:
                second_store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(reclaimed.status, "acquired")
        self.assertEqual(reclaimed.record.attempt, 2)
        self.assertEqual(stale_result.status, "reservation_mismatch")

    def test_db_only_preflight_releases_expired_reservation_on_postgres(self) -> None:
        with _store_for_fresh_head_database() as store:
            mutation = _db_only_mutation(
                lease_owner="worker-b",
                idempotency_key="product-preview-tls:postgres:preflight-expired",
                response_trace_id="trace-worker-b",
            )
            acquired = store.reserve_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner="worker-a",
                lease_seconds=1,
            )
            time.sleep(1.1)

            preflight = store.prepare_db_only_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(preflight.status, "released")
        self.assertIsNone(stored_reservation)

    def test_atomic_noop_profile_mutation_replays_across_two_store_instances(self) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            store.write_product_profile_record(profile)
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def apply_noop(active_store: PostgresRecordStore, owner: str) -> str:
                mutation = _db_only_mutation(
                    lease_owner=owner,
                    idempotency_key="product-preview-tls:postgres:noop",
                    response_trace_id=f"trace-{owner}",
                )
                barrier.wait()
                return active_store.compare_and_write_product_profile_record(
                    expected_record=profile,
                    replacement_record=profile,
                    mutation=mutation,
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: apply_noop(*arguments),
                            ((store, "worker-a"), (second_store, "worker-b")),
                        )
                    )
                stored_profile = store.read_product_profile_record(profile.product)
                stored_reservation = store.read_idempotency_record(
                    scope="github-actions|cbusillo/launchplane|workflow:test",
                    route_path="/v1/product-profiles/preview-tls/apply",
                    idempotency_key="product-preview-tls:postgres:noop",
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["replayed", "written"])
        self.assertEqual(stored_profile, profile)
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.attempt, 1)

    def test_atomic_profile_mutation_reclaims_expired_reservation_with_db_clock(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            mutation = _db_only_mutation(
                lease_owner="worker-b",
                idempotency_key="product-preview-tls:postgres:expired",
                response_trace_id="trace-worker-b",
            )
            store.write_product_profile_record(profile)
            acquired = store.reserve_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner="worker-a",
                lease_seconds=1,
            )
            time.sleep(1.1)

            result = store.compare_and_write_product_profile_record(
                expected_record=profile,
                replacement_record=profile,
                mutation=mutation,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(result.status, "written")
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.attempt, 2)
        self.assertEqual(stored_reservation.lease_owner, mutation.lease_owner)
        self.assertEqual(stored_reservation.response_trace_id, mutation.response_trace_id)

    def test_route_binding_reconcile_serializes_distinct_keys_across_stores(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            record = _route_binding()
            first_mutation = DbOnlyMutationRequest(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-first",
                request_fingerprint="route-binding-fingerprint-first",
                lease_owner="worker-a",
                response_status_code=202,
                response_trace_id="trace-worker-a",
                response_payload={"status": "accepted", "trace_id": "trace-worker-a"},
            )
            second_mutation = DbOnlyMutationRequest(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-second",
                request_fingerprint="route-binding-fingerprint-second",
                lease_owner="worker-b",
                response_status_code=202,
                response_trace_id="trace-worker-b",
                response_payload={"status": "accepted", "trace_id": "trace-worker-b"},
            )
            barrier = threading.Barrier(2)

            def reconcile_binding(
                active_store: PostgresRecordStore,
                mutation: DbOnlyMutationRequest,
            ) -> str:
                barrier.wait()
                return active_store.reconcile_route_binding_record(
                    expected_record=None,
                    replacement_record=record,
                    mutation=mutation,
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reconcile_binding(*arguments),
                            (
                                (store, first_mutation),
                                (second_store, second_mutation),
                            ),
                        )
                    )
                stored_record = store.read_route_binding_record(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                )
                reservation_records = tuple(
                    store.read_idempotency_record(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                    )
                    for reservation in (first_mutation, second_mutation)
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["changed", "created"])
        self.assertEqual(stored_record, record)
        self.assertEqual(
            sorted(
                reservation.state if reservation is not None else "missing"
                for reservation in reservation_records
            ),
            ["completed", "missing"],
        )

    def test_external_route_binding_replacement_rejects_stale_competing_cas(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            current_record = _route_binding()
            store.write_route_binding_record(current_record)
            first_replacement = current_record.model_copy(
                update={
                    "source": current_record.source.model_copy(
                        update={"source_label": "external-operator-a"}
                    ),
                    "updated_at": "2026-07-22T00:01:00Z",
                }
            )
            second_replacement = current_record.model_copy(
                update={
                    "source": current_record.source.model_copy(
                        update={"source_label": "external-operator-b"}
                    ),
                    "updated_at": "2026-07-22T00:02:00Z",
                }
            )
            first_mutation = DbOnlyMutationRequest(
                scope="github-actions:external-route-binding-test",
                route_path="/v1/route-bindings/external/reconcile",
                idempotency_key="external-route-binding-first",
                request_fingerprint="external-route-binding-fingerprint-first",
                lease_owner="worker-a",
                response_status_code=202,
                response_trace_id="trace-worker-a",
                response_payload={"status": "accepted", "trace_id": "trace-worker-a"},
            )
            second_mutation = DbOnlyMutationRequest(
                scope="github-actions:external-route-binding-test",
                route_path="/v1/route-bindings/external/reconcile",
                idempotency_key="external-route-binding-second",
                request_fingerprint="external-route-binding-fingerprint-second",
                lease_owner="worker-b",
                response_status_code=202,
                response_trace_id="trace-worker-b",
                response_payload={"status": "accepted", "trace_id": "trace-worker-b"},
            )
            barrier = threading.Barrier(2)

            def replace_binding(
                active_store: PostgresRecordStore,
                replacement_record: EnvironmentRouteBindingRecord,
                mutation: DbOnlyMutationRequest,
            ) -> str:
                barrier.wait()
                return active_store.reconcile_route_binding_record(
                    expected_record=current_record,
                    replacement_record=replacement_record,
                    mutation=mutation,
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: replace_binding(*arguments),
                            (
                                (store, first_replacement, first_mutation),
                                (second_store, second_replacement, second_mutation),
                            ),
                        )
                    )
                stored_record = store.read_route_binding_record(
                    product=current_record.product,
                    context_name=current_record.context,
                    instance_name=current_record.instance,
                )
                reservation_records = tuple(
                    store.read_idempotency_record(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                    )
                    for reservation in (first_mutation, second_mutation)
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["changed", "refreshed"])
        self.assertIn(stored_record, (first_replacement, second_replacement))
        self.assertEqual(
            sorted(
                reservation.state if reservation is not None else "missing"
                for reservation in reservation_records
            ),
            ["completed", "missing"],
        )

    def test_profile_write_rolls_back_when_completion_persistence_fails(self) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            replacement = profile.model_copy(
                update={
                    "display_name": "Changed Before Injected Failure",
                    "updated_at": "2026-07-13T00:01:00Z",
                }
            )
            mutation = _db_only_mutation(
                lease_owner="worker-a",
                idempotency_key="product-preview-tls:postgres:fault",
                response_trace_id="trace-injected-failure",
            )
            store.write_product_profile_record(profile)

            with patch.object(
                store,
                "_sync_idempotency_row",
                side_effect=RuntimeError("injected completion persistence failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected completion persistence failure",
                ):
                    store.compare_and_write_product_profile_record(
                        expected_record=profile,
                        replacement_record=replacement,
                        mutation=mutation,
                    )
            stored_profile = store.read_product_profile_record(profile.product)
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(stored_profile, profile)
        self.assertIsNone(stored_reservation)

    def test_partial_unique_active_operation_index_rejects_second_active_lane(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            first_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-first",
                idempotency_key="bootstrap-first",
            )
            second_active_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-second",
                idempotency_key="bootstrap-second",
            )
            terminal_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-terminal",
                idempotency_key="bootstrap-terminal",
                status="fail",
                phase="failed",
                created_at="2026-05-17T00:02:00Z",
                updated_at="2026-05-17T00:02:00Z",
                finished_at="2026-05-17T00:02:00Z",
                error_message="terminal record does not reserve active lane",
            )
            store.write_odoo_stable_bootstrap_operation_record(first_record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                existing_record, created = (
                    second_store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
                        second_active_record
                    )
                )
                second_store.write_odoo_stable_bootstrap_operation_record(terminal_record)
                terminal_records = second_store.list_odoo_stable_bootstrap_operation_records(
                    statuses=("fail",),
                )
            finally:
                second_store.close()

        self.assertFalse(created)
        self.assertEqual(existing_record.operation_id, first_record.operation_id)
        self.assertEqual(
            [record.operation_id for record in terminal_records], [terminal_record.operation_id]
        )

    def test_cross_kind_stable_lane_creation_is_serialized(self) -> None:
        with _store_for_fresh_head_database() as store:
            start_barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _create_cross_kind_stable_lane_operation,
                        database_url=store.database_url,
                        operation_kind=operation_kind,
                        start_barrier=start_barrier,
                    )
                    for operation_kind in (
                        "prod_backup_restore",
                        "retained_volume_backup_import",
                    )
                ]
                results = [future.result(timeout=10) for future in futures]

            active_restore_records = store.list_odoo_prod_backup_restore_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="prod",
                statuses=("pending", "running"),
            )
            active_import_records = (
                store.list_odoo_prod_retained_volume_backup_import_operation_records(
                    product="odoo-tenant-cm",
                    context_name="cm",
                    instance_name="prod",
                    statuses=("pending", "running"),
                )
            )

        self.assertEqual([result[0] for result in results].count("created"), 1)
        self.assertEqual([result[0] for result in results].count("conflict"), 1)
        self.assertEqual(len(active_restore_records) + len(active_import_records), 1)

    def test_cross_kind_stable_lane_claim_is_serialized_for_preexisting_queue(self) -> None:
        with _store_for_fresh_head_database() as store:
            restore_operation = _restore_operation("operation-cm-prod-restore-preexisting")
            retained_operation = _retained_operation_for_restore_lane().model_copy(
                update={"operation_id": "retained-plan-operation-cm-prod-preexisting"}
            )
            store.write_odoo_prod_backup_restore_operation_record(restore_operation)
            store.write_odoo_prod_retained_volume_backup_import_operation_record(retained_operation)

            start_barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _claim_cross_kind_stable_lane_operation,
                        database_url=store.database_url,
                        operation_kind=operation_kind,
                        start_barrier=start_barrier,
                    )
                    for operation_kind in (
                        "prod_backup_restore",
                        "retained_volume_backup_import",
                    )
                ]
                results = dict(future.result(timeout=10) for future in futures)

            stored_restore = store.read_odoo_prod_backup_restore_operation_record(
                restore_operation.operation_id
            )
            stored_import = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                retained_operation.operation_id
            )

        self.assertEqual(results["prod_backup_restore"], restore_operation.operation_id)
        self.assertEqual(results["retained_volume_backup_import"], "")
        self.assertEqual(stored_restore.status, "running")
        self.assertEqual(stored_import.status, "pending")


def _attempt_stale_owner_completion(
    database_url: str,
    results: list[bool | BaseException],
    started: threading.Event,
) -> None:
    worker_url = make_url(database_url).update_query_dict(
        {"options": f"-c lock_timeout={LOCK_WAIT_TIMEOUT}"}
    )
    store = PostgresRecordStore(database_url=worker_url.render_as_string(hide_password=False))
    try:
        started.set()
        result = store.complete_odoo_stable_bootstrap_operation_record(
            record=_bootstrap_operation(
                status="pass",
                phase="completed",
                updated_at="2026-05-17T00:04:00Z",
                finished_at="2026-05-17T00:04:00Z",
            ),
            lease_owner="stale-worker",
        )
        results.append(result)
    except BaseException as error:
        results.append(error)
    finally:
        store.close()


def _current_alembic_version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("select version_num from alembic_version")).scalar_one())


def _column_type(engine: Engine, *, table_name: str, column_name: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "select data_type from information_schema.columns "
                    "where table_schema = current_schema() "
                    "and table_name = :table_name and column_name = :column_name"
                ),
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one()
        )


_PROVIDER_OPERATION_SCOPE = "github-actions:provider-operation-integration"
_PROVIDER_OPERATION_ROUTE = "/v1/drivers/provider-operation-integration"
_PROVIDER_OPERATION_KEY = "provider-operation:integration:1"
_PROVIDER_OPERATION_FINGERPRINT = "provider-operation-integration-fingerprint"
_PROVIDER_OPERATION_RECONCILIATION_KEY = "dokploy-compose:integration-preview"
_PROVIDER_OPERATION_TARGET_KEY = "dokploy-target:integration-preview"


class _IntegrationProviderAdapter:
    def __init__(
        self,
        *,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        observation: ProviderObservation | None = None,
    ) -> None:
        self._started = started
        self._release = release
        self._observation = observation or ProviderObservation(outcome="unknown")
        self.apply_calls = 0
        self.observe_calls = 0

    def reconciliation_key(self) -> str:
        return _PROVIDER_OPERATION_RECONCILIATION_KEY

    def target_key(self) -> str:
        return _PROVIDER_OPERATION_TARGET_KEY

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        self.observe_calls += 1
        return self._observation

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        self.apply_calls += 1
        lease.checkpoint_effect("integration_effect")
        if self._started is not None:
            self._started.set()
        if self._release is not None and not self._release.wait(timeout=5):
            raise AssertionError("provider apply was not released")
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload={"trace_id": "integration-effect", "status": "accepted"},
        )


def _run_integration_provider_operation(
    store: PostgresRecordStore,
    adapter: _IntegrationProviderAdapter,
    *,
    lease_owner: str,
    response_trace_id: str,
    lease_seconds: int = 300,
    heartbeat_interval_seconds: float | None = None,
    idempotency_key: str = _PROVIDER_OPERATION_KEY,
    request_fingerprint: str = _PROVIDER_OPERATION_FINGERPRINT,
    target_supersession: ProviderTargetSupersession | None = None,
) -> DurableProviderOperationResult:
    return run_durable_provider_operation(
        store=store,
        scope=_PROVIDER_OPERATION_SCOPE,
        route_path=_PROVIDER_OPERATION_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=lease_owner,
        response_trace_id=response_trace_id,
        adapter=adapter,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        target_supersession=target_supersession,
    )


class RealPostgresProviderOperationTests(unittest.TestCase):
    def test_two_instances_apply_provider_effect_exactly_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                started = threading.Event()
                release = threading.Event()
                holder = _IntegrationProviderAdapter(started=started, release=release)
                holder_result: dict[str, DurableProviderOperationResult] = {}

                def run_holder() -> None:
                    holder_result["value"] = _run_integration_provider_operation(
                        store,
                        holder,
                        lease_owner="instance-a",
                        response_trace_id="integration-trace-a",
                    )

                holder_thread = threading.Thread(target=run_holder)
                holder_thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    second = _IntegrationProviderAdapter()
                    second_result = _run_integration_provider_operation(
                        second_store,
                        second,
                        lease_owner="instance-b",
                        response_trace_id="integration-trace-b",
                    )
                    self.assertEqual(second_result.status, "in_progress")
                    self.assertEqual(second.apply_calls, 0)
                finally:
                    release.set()
                    holder_thread.join(5)

                self.assertEqual(holder_result["value"].status, "completed")
                self.assertEqual(holder.apply_calls, 1)
                stored = store.read_idempotency_record(
                    scope=_PROVIDER_OPERATION_SCOPE,
                    route_path=_PROVIDER_OPERATION_ROUTE,
                    idempotency_key=_PROVIDER_OPERATION_KEY,
                )
                assert stored is not None
                self.assertEqual(stored.state, "completed")
            finally:
                second_store.close()

    def test_different_idempotency_key_cannot_bypass_active_target_fence(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key="provider-operation:integration:first",
                request_fingerprint="provider-operation-integration-first",
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            second = _run_integration_provider_operation(
                store,
                _IntegrationProviderAdapter(),
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
                idempotency_key="provider-operation:integration:second",
                request_fingerprint="provider-operation-integration-second",
            )

        self.assertEqual(first.status, "acquired")
        self.assertEqual(second.status, "target_busy")

    def test_destroy_supersedes_expired_reconcile_required_target_fence(self) -> None:
        with _store_for_fresh_head_database() as store:
            stale = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key="provider-operation:integration:stale-refresh",
                request_fingerprint="provider-operation-integration-stale-refresh",
                lease_owner="instance-a",
                lease_seconds=1,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            reconciled = store.mark_mutation_reconcile_required(
                reservation=stale.record,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
            )
            time.sleep(1.2)
            destroy_adapter = _IntegrationProviderAdapter()
            result = _run_integration_provider_operation(
                store,
                destroy_adapter,
                lease_owner="instance-b",
                response_trace_id="integration-destroy-trace",
                idempotency_key="provider-operation:integration:destroy",
                request_fingerprint="provider-operation-integration-destroy",
                target_supersession=ProviderTargetSupersession(
                    response_status_code=409,
                    response_payload={"status": "superseded"},
                    quiescence_check=lambda _reservation: True,
                ),
            )
            stored_stale = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key="provider-operation:integration:stale-refresh",
            )

        self.assertEqual(reconciled.status, "updated")
        self.assertEqual(result.status, "completed")
        self.assertEqual(destroy_adapter.apply_calls, 1)
        assert stored_stale is not None
        self.assertEqual(stored_stale.state, "completed")
        self.assertEqual(stored_stale.response_status_code, 409)
        self.assertEqual(stored_stale.response_payload, {"status": "superseded"})

    def test_heartbeats_hold_target_fence_past_original_lease(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            started = threading.Event()
            release = threading.Event()
            holder = _IntegrationProviderAdapter(started=started, release=release)
            holder_result: dict[str, DurableProviderOperationResult] = {}

            def run_holder() -> None:
                holder_result["value"] = _run_integration_provider_operation(
                    store,
                    holder,
                    lease_owner="instance-a",
                    response_trace_id="integration-trace-a",
                    lease_seconds=2,
                    heartbeat_interval_seconds=0.2,
                )

            holder_thread = threading.Thread(target=run_holder)
            holder_thread.start()
            try:
                self.assertTrue(started.wait(5))
                time.sleep(2.2)
                second_result = _run_integration_provider_operation(
                    second_store,
                    _IntegrationProviderAdapter(),
                    lease_owner="instance-b",
                    response_trace_id="integration-trace-b",
                )
                self.assertEqual(second_result.status, "in_progress")
            finally:
                release.set()
                holder_thread.join(5)
                second_store.close()

            self.assertEqual(holder_result["value"].status, "completed")

    def test_restart_adopts_observed_effect_without_reapplying(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=1,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            self.assertEqual(reservation.status, "acquired")
            time.sleep(1.2)

            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(
                    outcome="present",
                    response_status_code=202,
                    response_payload={"trace_id": "adopted", "status": "accepted"},
                )
            )
            adopted = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(adopted.status, "adopted")
            self.assertEqual(recovery.apply_calls, 0)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "completed")

    def test_reconcile_required_fails_closed_when_effect_unknown(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            store.mark_mutation_reconcile_required(
                reservation=reservation.record,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
            )

            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(outcome="unknown")
            )
            result = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(result.status, "reconcile_required")
            self.assertEqual(recovery.apply_calls, 0)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "reconcile_required")

    def test_reconcile_absent_releases_and_reapplies_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            store.mark_mutation_reconcile_required(
                reservation=reservation.record,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
            )
            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(outcome="absent")
            )

            result = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(recovery.observe_calls, 1)
            self.assertEqual(recovery.apply_calls, 1)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "completed")


if __name__ == "__main__":
    unittest.main()
