import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal, Protocol, cast
from uuid import uuid4
import click
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field

from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane import product_read_service as control_plane_product_read_service
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.product_environment_read_model import (
    ProductEnvironmentConfigStatus,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_evidence import (
    PreviewDestroyedEvidenceEnvelope,
    PreviewGenerationEvidenceEnvelope,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.protected_artifacts import (
    ProtectedArtifactStore,
    ProtectedArtifactSet,
    build_protected_artifact_set,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene_evidence import (
    RunnerHostHygieneAuditEvidenceEnvelope,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration_evidence import (
    RunnerLaneRegistrationAuditEvidenceEnvelope,
)
from control_plane.contracts.secret_record import SecretScope
from control_plane.drivers.registry import build_driver_context_view, list_driver_descriptors
from control_plane.drivers.registry import read_driver_descriptor as read_driver_descriptor_record
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    bearer_identity_from_token,
)
from control_plane.service_human_auth import HumanSessionManager, LaunchplaneHumanSession
from control_plane.launchplane_mutations import (
    LaunchplaneDestroyPreviewStore,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    PromotionEvidenceValidationError,
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.ship import utc_now_timestamp


_BEARER_CHALLENGE_HEADER = {"WWW-Authenticate": 'Bearer realm="Launchplane API"'}
_LAUNCHPLANE_DRIVER_READ_PRODUCT = "launchplane"
_LAUNCHPLANE_DRIVER_READ_CONTEXT = "launchplane"
_DEPLOYMENT_EVIDENCE_ROUTE = "/v1/evidence/deployments"
_BACKUP_GATE_EVIDENCE_ROUTE = "/v1/evidence/backup-gates"
_PROMOTION_EVIDENCE_ROUTE = "/v1/evidence/promotions"
_PREVIEW_GENERATION_EVIDENCE_ROUTE = "/v1/evidence/previews/generations"
_PREVIEW_DESTROYED_EVIDENCE_ROUTE = "/v1/evidence/previews/destroyed"
_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-host-hygiene/audits"
_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-lane-registration/audits"
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class LaunchplaneErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class LaunchplaneErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "storage_backend": "postgres",
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    storage_backend: str


class ProductEnvironmentConfigStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    config_status: ProductEnvironmentConfigStatus


class DriverDescriptorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    drivers: tuple[DriverDescriptor, ...]


class DriverDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver: DriverDescriptor


class DriverContextViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    view: DriverContextView


class DeploymentRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: DeploymentRecord


class PromotionRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PromotionRecord


class PreviewRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PreviewRecord


class PreviewHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    preview: PreviewRecord
    generations: tuple[PreviewGenerationRecord, ...]


class EnvironmentInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EnvironmentInventory


class RecentOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    storage_backend: str
    inventory: tuple[EnvironmentInventory, ...]
    recent_deployments: tuple[DeploymentRecord, ...]
    recent_promotions: tuple[PromotionRecord, ...]
    recent_previews: tuple[PreviewRecord, ...]


class SecretStatusBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str
    binding_type: str
    binding_key: str
    status: str
    context: str
    instance: str
    updated_at: str


class SecretStatusAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    recorded_at: str
    actor: str
    detail: str
    metadata: dict[str, str]


class SecretStatusReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str
    scope: SecretScope
    integration: str
    name: str
    context: str
    instance: str
    policy: str
    status: str
    description: str
    created_at: str
    updated_at: str
    updated_by: str
    last_validated_at: str
    current_version_id: str
    version_count: int
    current_version_created_at: str
    binding: SecretStatusBinding | None = None
    recent_audit_events: tuple[SecretStatusAuditEvent, ...]


class SecretStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    secret: SecretStatusReadModel


class SecretStatusListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    secrets: tuple[SecretStatusReadModel, ...]


class ProductContextCutoverAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    audit: dict[str, object]


class ProtectedArtifactsResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "protected_artifacts": {
                        "schema_version": 1,
                        "product": "example-product",
                        "context": "",
                        "entries": [],
                        "artifact_ids": ["artifact-example-prod"],
                        "image_references": ["ghcr.io/example-org/example-app@sha256:abc123"],
                        "image_digests": ["sha256:abc123"],
                        "warnings": [],
                    },
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    protected_artifacts: ProtectedArtifactSet


class DeploymentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")


class BackupGateEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    backup_gate: BackupGateRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("backup gate evidence requires product")


class PromotionEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    promotion: PromotionRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")


class AcceptedEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str]
    result: dict[str, object] | None = None
    replayed: bool | None = None
    original_trace_id: str | None = None


class _RecordStoreFactory(Protocol):
    def __call__(self) -> object: ...


class _IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


class _BackupGateEvidenceStore(Protocol):
    def write_backup_gate_record(self, record: BackupGateRecord) -> object: ...


class _RunnerHostHygieneAuditEvidenceStore(Protocol):
    def write_runner_host_hygiene_audit_record(
        self,
        record: RunnerHostHygieneApplyAuditRecord,
    ) -> object: ...


class _RunnerLaneRegistrationAuditEvidenceStore(Protocol):
    def write_runner_lane_registration_audit_record(
        self,
        record: RunnerLaneRegistrationAuditRecord,
    ) -> object: ...


class _DeploymentReadStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


class _PromotionReadStore(Protocol):
    def read_promotion_record(self, record_id: str) -> PromotionRecord: ...


class _PreviewReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...


class _PreviewHistoryReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]: ...


class _EnvironmentInventoryReadStore(Protocol):
    def read_environment_inventory(
        self,
        *,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentInventory: ...


class _RecentOperationsReadStore(Protocol):
    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]: ...

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...


def require_protected_artifact_store(record_store: object) -> ProtectedArtifactStore:
    required_methods = (
        "list_artifact_manifests",
        "list_environment_inventory",
        "list_product_profile_records",
        "list_release_tuple_records",
        "list_preview_records",
        "list_preview_generation_records",
        "list_preview_pr_feedback_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support protected artifact inventory "
            f"reads: {missing_summary}"
        )
    return cast(ProtectedArtifactStore, record_store)


def require_deployment_evidence_store(record_store: object) -> EvidenceIngestionStore:
    required_methods = (
        "write_deployment_record",
        "write_environment_inventory",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support deployment evidence writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def require_promotion_evidence_store(
    record_store: object,
    promotion_record: PromotionRecord,
) -> EvidenceIngestionStore:
    if promotion_record.deployment_record_id.strip():
        required_methods = ["read_deployment_record", "write_promotion_evidence_records"]
    else:
        required_methods = ["write_promotion_record"]
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support promotion evidence writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def require_preview_generation_evidence_store(record_store: object) -> LaunchplaneMutationStore:
    required_methods = (
        "list_preview_records",
        "list_preview_generation_records",
        "write_preview_record",
        "write_preview_generation_record",
        "write_preview_generation_evidence_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview generation evidence writes: "
            f"{missing_summary}"
        )
    return cast(LaunchplaneMutationStore, record_store)


def require_preview_destroyed_evidence_store(
    record_store: object,
) -> LaunchplaneDestroyPreviewStore:
    required_methods = (
        "list_preview_records",
        "write_preview_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview destroyed evidence writes: "
            f"{missing_summary}"
        )
    return cast(LaunchplaneDestroyPreviewStore, record_store)


def require_backup_gate_evidence_store(record_store: object) -> _BackupGateEvidenceStore:
    required_methods = ("write_backup_gate_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support backup gate evidence writes: "
            f"{missing_summary}"
        )
    return cast(_BackupGateEvidenceStore, record_store)


def require_runner_host_hygiene_audit_evidence_store(
    record_store: object,
) -> _RunnerHostHygieneAuditEvidenceStore:
    required_methods = ("write_runner_host_hygiene_audit_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support runner host hygiene audit "
            f"evidence writes: {missing_summary}"
        )
    return cast(_RunnerHostHygieneAuditEvidenceStore, record_store)


def require_runner_lane_registration_audit_evidence_store(
    record_store: object,
) -> _RunnerLaneRegistrationAuditEvidenceStore:
    required_methods = ("write_runner_lane_registration_audit_record",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support runner lane registration audit "
            f"evidence writes: {missing_summary}"
        )
    return cast(_RunnerLaneRegistrationAuditEvidenceStore, record_store)


def require_deployment_read_store(record_store: object) -> _DeploymentReadStore:
    read_record = getattr(record_store, "read_deployment_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support deployment record reads: "
            "read_deployment_record"
        )
    return cast(_DeploymentReadStore, record_store)


def require_promotion_read_store(record_store: object) -> _PromotionReadStore:
    read_record = getattr(record_store, "read_promotion_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support promotion record reads: "
            "read_promotion_record"
        )
    return cast(_PromotionReadStore, record_store)


def require_preview_read_store(record_store: object) -> _PreviewReadStore:
    read_record = getattr(record_store, "read_preview_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support preview reads: read_preview_record"
        )
    return cast(_PreviewReadStore, record_store)


def require_preview_history_read_store(record_store: object) -> _PreviewHistoryReadStore:
    required_methods = ("read_preview_record", "list_preview_generation_records")
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support preview history reads: {missing_summary}"
        )
    return cast(_PreviewHistoryReadStore, record_store)


def require_environment_inventory_read_store(
    record_store: object,
) -> _EnvironmentInventoryReadStore:
    read_record = getattr(record_store, "read_environment_inventory", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support environment inventory reads: "
            "read_environment_inventory"
        )
    return cast(_EnvironmentInventoryReadStore, record_store)


def require_recent_operations_read_store(record_store: object) -> _RecentOperationsReadStore:
    required_methods = (
        "list_environment_inventory",
        "list_deployment_records",
        "list_promotion_records",
        "list_preview_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support recent operation reads: {missing_summary}"
        )
    return cast(_RecentOperationsReadStore, record_store)


def require_secret_status_read_store(record_store: object) -> control_plane_secrets.SecretReadStore:
    required_methods = (
        "read_secret_record",
        "list_secret_records",
        "read_secret_version",
        "list_secret_versions",
        "list_secret_bindings",
        "list_secret_audit_events",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support secret status reads: {missing_summary}"
        )
    return cast(control_plane_secrets.SecretReadStore, record_store)


def require_product_context_audit_store(
    record_store: object,
) -> control_plane_product_context_audit.ProductContextAuditStore:
    required_methods = (
        "read_product_profile_record",
        "list_runtime_environment_records",
        "list_secret_records",
        "list_secret_bindings",
        "list_dokploy_target_records",
        "list_dokploy_target_id_records",
        "list_environment_inventory",
        "list_release_tuple_records",
        "list_backup_gate_records",
        "list_deployment_records",
        "list_promotion_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        missing_summary = ", ".join(missing_methods) or "postgres_storage"
        raise TypeError(
            "Launchplane record store does not support context cutover audit reads: "
            f"{missing_summary}"
        )
    return cast(control_plane_product_context_audit.ProductContextAuditStore, record_store)


def product_profile_context_cutover_allowed_contexts(
    profile: LaunchplaneProductProfileRecord,
) -> frozenset[str]:
    contexts = {profile.product.strip()}
    contexts.update(lane.context.strip() for lane in profile.lanes if lane.context.strip())
    if profile.preview.enabled and profile.preview.context.strip():
        contexts.add(profile.preview.context.strip())
    return frozenset(context for context in contexts if context)


def product_profile_context_cutover_contexts_allowed(
    *,
    profile: LaunchplaneProductProfileRecord,
    source_context: str,
    target_context: str,
    preview_context: str,
) -> bool:
    allowed_contexts = product_profile_context_cutover_allowed_contexts(profile)
    requested_contexts = {source_context.strip(), target_context.strip()}
    if preview_context.strip():
        requested_contexts.add(preview_context.strip())
    requested_contexts.discard("")
    return requested_contexts.issubset(allowed_contexts)


def idempotency_capable_store(record_store: object) -> _IdempotencyCapableStore | None:
    if callable(getattr(record_store, "read_idempotency_record", None)) and callable(
        getattr(record_store, "write_idempotency_record", None)
    ):
        return cast(_IdempotencyCapableStore, record_store)
    return None


def idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, LocalOperatorIdentity):
        return "|".join(("local-operator", identity.subject, identity.token_label))
    if isinstance(identity, LocalAdminIdentity):
        return "|".join(("local-admin", identity.subject, identity.token_label))
    if isinstance(identity, TerminalAgentIdentity):
        return "|".join(("terminal-agent", identity.subject, identity.token_label))
    if isinstance(identity, GitHubActionsIdentity):
        workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
        return "|".join(
            (
                str(identity.repository).strip(),
                str(workflow_ref).strip(),
                str(identity.subject).strip(),
            )
        )
    raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")


def request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LaunchplaneAuthzPolicyRuntime:
    def __init__(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy

    def update(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy


class ResolvedLaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: LaunchplaneAuthzPolicy
    policy_sha256: str
    source: str


def resolve_launchplane_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
    policy_source: str,
    now_timestamp: str,
) -> ResolvedLaunchplaneAuthzPolicy:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if callable(list_records):
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return ResolvedLaunchplaneAuthzPolicy(
                policy=record.policy,
                policy_sha256=record.policy_sha256,
                source="db",
            )

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    write_record = getattr(record_store, "write_authz_policy_record", None)
    if callable(write_record):
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                updated_at=now_timestamp,
                policy_sha256=policy_sha256,
            ),
            status="active",
            source=policy_source,
            updated_at=now_timestamp,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        write_record(record)
        return ResolvedLaunchplaneAuthzPolicy(
            policy=record.policy,
            policy_sha256=record.policy_sha256,
            source="bootstrap_seeded_store",
        )

    return ResolvedLaunchplaneAuthzPolicy(
        policy=bootstrap_policy,
        policy_sha256=policy_sha256,
        source="bootstrap",
    )


def create_launchplane_fastapi_app(
    *,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    authz_policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    database_url: str | None = None,
    record_store_factory: _RecordStoreFactory | None = None,
    bearer_identity_config: BearerIdentityConfig | None = None,
    human_session_manager: HumanSessionManager | None = None,
    control_plane_root_path: FilePath | None = None,
) -> FastAPI:
    resolved_authz_policy_runtime = authz_policy_runtime or LaunchplaneAuthzPolicyRuntime(
        authz_policy
    )
    resolved_control_plane_root = (
        control_plane_root_path or FilePath(__file__).resolve().parent.parent
    )
    shared_record_store: object | None = (
        None
        if record_store_factory is not None
        else build_shared_record_store(database_url=database_url)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if isinstance(shared_record_store, PostgresRecordStore):
                shared_record_store.close()

    app = FastAPI(title="Launchplane API", version="0.1.0", lifespan=lifespan)

    def next_trace_id() -> str:
        return f"launchplane_req_{uuid4().hex}"

    def get_record_store() -> object:
        if record_store_factory is not None:
            return record_store_factory()
        if shared_record_store is None:
            raise RuntimeError("Launchplane record store is not initialized.")
        return shared_record_store

    def read_human_session(
        *,
        cookie_header: str,
    ) -> tuple[LaunchplaneHumanSession, bool] | None:
        if human_session_manager is None:
            return None
        session = human_session_manager.read_cookie(cookie_header)
        if session is None:
            return None
        renewed_session = human_session_manager.renew_if_needed(session)
        if renewed_session is None:
            return None
        return renewed_session, renewed_session.expires_at != session.expires_at

    def read_human_session_identity(
        *,
        cookie_header: str,
        request: Request,
        response: Response,
    ) -> GitHubHumanIdentity | None:
        session_result = read_human_session(cookie_header=cookie_header)
        if session_result is None:
            return None
        session, was_renewed = session_result
        if was_renewed and human_session_manager is not None:
            session_cookie_header = human_session_manager.session_cookie_header(session)
            response.headers.append("Set-Cookie", session_cookie_header)
            request.state.launchplane_renewed_session_cookie = session_cookie_header
        return session.identity

    def read_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if header:
            scheme, _, token = header.partition(" ")
            bearer_token = token.strip()
            if scheme.lower() == "bearer" and bearer_token:
                try:
                    owner_agent_identity = bearer_identity_from_token(
                        token=bearer_token,
                        config=bearer_identity_config or BearerIdentityConfig(),
                    )
                except PermissionError as error:
                    raise _authentication_required_error(str(error)) from error
                if owner_agent_identity is not None:
                    return owner_agent_identity
                try:
                    return verifier.verify(bearer_token)
                except (InvalidTokenError, ValueError) as error:
                    raise _authentication_required_error(str(error)) from error
        human_identity = read_human_session_identity(
            cookie_header=cookie,
            request=request,
            response=response,
        )
        if human_identity is not None:
            return human_identity
        raise _authentication_required_error("Authorization header is required.")

    def read_write_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if not header:
            raise _authentication_required_error("Authorization header is required.")
        scheme, _, token = header.partition(" ")
        bearer_token = token.strip()
        if scheme.lower() != "bearer" or not bearer_token:
            raise _authentication_required_error("Bearer token is required.")
        try:
            owner_agent_identity = bearer_identity_from_token(
                token=bearer_token,
                config=bearer_identity_config or BearerIdentityConfig(),
            )
        except PermissionError as error:
            raise _authentication_required_error(str(error)) from error
        if isinstance(owner_agent_identity, LocalAdminIdentity | LocalOperatorIdentity):
            return owner_agent_identity
        if owner_agent_identity is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message=("Terminal agent credentials can only read redacted Launchplane context."),
            )
        try:
            oidc_identity = verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error
        if not isinstance(oidc_identity, GitHubActionsIdentity):
            raise _authentication_required_error("Mutation routes require GitHub Actions OIDC.")
        return oidc_identity

    def read_product_environment_config_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentConfigStatusResponse:
        trace_id = next_trace_id()

        def product_action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            product_read_result = (
                control_plane_product_read_service.build_product_environment_read_service_result(
                    record_store=product_read_store,
                    params={
                        "product": product,
                        "environment": environment,
                        "config_status": "true",
                    },
                    action_allowed=product_action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not product_action_allowed(
            "product_environment.read",
            product_read_result.authorization_product,
            product_read_result.authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=product_read_result.denial_message,
            )

        config_status = ProductEnvironmentConfigStatus.model_validate(
            product_read_result.payload["config_status"]
        )
        return ProductEnvironmentConfigStatusResponse(
            trace_id=trace_id,
            config_status=config_status,
        )

    def read_protected_artifacts(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> ProtectedArtifactsResponse:
        trace_id = next_trace_id()
        requested_product = product.strip()
        requested_context = context.strip()
        if not requested_product:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Protected artifact inventory requires a product query parameter.",
            )
        authz_context = requested_context or "*"
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="artifact_protection.read",
            product=requested_product,
            context=authz_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read protected artifact inventory.",
            )
        try:
            protected_artifact_store = require_protected_artifact_store(record_store)
            protected_artifacts = build_protected_artifact_set(
                protected_artifact_store,
                product=requested_product,
                context_name=requested_context,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ProtectedArtifactsResponse(
            trace_id=trace_id,
            protected_artifacts=protected_artifacts,
        )

    def ensure_driver_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        context: str = _LAUNCHPLANE_DRIVER_READ_CONTEXT,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="driver.read",
            product=_LAUNCHPLANE_DRIVER_READ_PRODUCT,
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read driver metadata for the requested context.",
            )

    def read_driver_descriptors(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorsResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        return DriverDescriptorsResponse(
            trace_id=trace_id,
            drivers=list_driver_descriptors(),
        )

    def read_driver_descriptor(
        driver_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        try:
            descriptor = read_driver_descriptor_record(driver_id)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DriverDescriptorResponse(trace_id=trace_id, driver=descriptor)

    def read_driver_context_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_driver_instance_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
            instance_name=instance,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_deployment_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DeploymentRecordResponse:
        trace_id = next_trace_id()
        try:
            deployment_store = require_deployment_read_store(record_store)
            deployment = deployment_store.read_deployment_record(record_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="deployment.read",
            product="launchplane",
            context=deployment.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read deployment records for the requested context.",
            )
        return DeploymentRecordResponse(trace_id=trace_id, record=deployment)

    def read_promotion_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PromotionRecordResponse:
        trace_id = next_trace_id()
        try:
            promotion_store = require_promotion_read_store(record_store)
            promotion = promotion_store.read_promotion_record(record_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="promotion.read",
            product="launchplane",
            context=promotion.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read promotion records for the requested context.",
            )
        return PromotionRecordResponse(trace_id=trace_id, record=promotion)

    def read_preview_record(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PreviewRecordResponse:
        trace_id = next_trace_id()
        try:
            preview_store = require_preview_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        return PreviewRecordResponse(trace_id=trace_id, record=preview)

    def read_preview_history(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> PreviewHistoryResponse:
        trace_id = next_trace_id()
        try:
            preview_store = require_preview_history_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        try:
            generations = preview_store.list_preview_generation_records(
                preview_id=preview.preview_id
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return PreviewHistoryResponse(
            trace_id=trace_id,
            preview=preview,
            generations=generations,
        )

    def read_environment_inventory(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> EnvironmentInventoryResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="inventory.read",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        try:
            inventory_store = require_environment_inventory_read_store(record_store)
            inventory = inventory_store.read_environment_inventory(
                context_name=context,
                instance_name=instance,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="inventory.read",
            product="launchplane",
            context=inventory.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        return EnvironmentInventoryResponse(trace_id=trace_id, record=inventory)

    def read_recent_operations(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> RecentOperationsResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="operations.read",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read recent operations for the requested context.",
            )
        try:
            operations_store = require_recent_operations_read_store(record_store)
            inventory = tuple(
                record
                for record in operations_store.list_environment_inventory()
                if record.context == context
            )
            recent_deployments = operations_store.list_deployment_records(
                context_name=context,
                limit=10,
            )
            recent_promotions = operations_store.list_promotion_records(
                context_name=context,
                limit=10,
            )
            recent_previews = operations_store.list_preview_records(
                context_name=context,
                limit=10,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return RecentOperationsResponse(
            trace_id=trace_id,
            context=context,
            storage_backend=storage_backend_name(record_store),
            inventory=inventory,
            recent_deployments=recent_deployments,
            recent_promotions=recent_promotions,
            recent_previews=recent_previews,
        )

    def read_product_context_cutover_audit(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        source_context: Annotated[str, Query()] = "",
        target_context: Annotated[str, Query()] = "",
        preview_context: Annotated[str, Query()] = "",
    ) -> ProductContextCutoverAuditResponse:
        trace_id = next_trace_id()
        try:
            audit_store = require_product_context_audit_store(record_store)
            profile = audit_store.read_product_profile_record(product)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="product_profile.read",
            product=profile.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested product profile.",
            )
        normalized_source_context = source_context.strip()
        normalized_target_context = target_context.strip()
        normalized_preview_context = preview_context.strip()
        if not product_profile_context_cutover_contexts_allowed(
            profile=profile,
            source_context=normalized_source_context,
            target_context=normalized_target_context,
            preview_context=normalized_preview_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="context_not_in_product_boundary",
                message="Requested audit contexts are not owned by the product profile.",
            )
        try:
            audit_payload = control_plane_product_context_audit.build_product_context_cutover_audit(
                record_store=audit_store,
                product=profile.product,
                source_context=normalized_source_context,
                target_context=normalized_target_context,
                preview_context=normalized_preview_context,
            )
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_context_cutover_audit_request",
                message="Context cutover audit request is invalid.",
            ) from error
        return ProductContextCutoverAuditResponse(trace_id=trace_id, audit=audit_payload)

    def read_secret_status(
        secret_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusResponse:
        trace_id = next_trace_id()
        try:
            secret_store = require_secret_status_read_store(record_store)
            secret_status = SecretStatusReadModel.model_validate(
                control_plane_secrets.build_secret_status(
                    secret_store,
                    secret_id=secret_id,
                )
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="secret.read",
            product="launchplane",
            context=secret_status.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read Launchplane managed secret status for the requested context.",
            )
        return SecretStatusResponse(trace_id=trace_id, secret=secret_status)

    def list_context_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance="",
            identity=identity,
            record_store=record_store,
        )

    def list_instance_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance=instance,
            identity=identity,
            record_store=record_store,
        )

    def list_secret_statuses_for_context(
        *,
        context: str,
        instance: str,
        identity: LaunchplaneIdentity,
        record_store: object,
    ) -> SecretStatusListResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="secret.list",
            product="launchplane",
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list Launchplane managed secret status for the requested context.",
            )
        try:
            secret_store = require_secret_status_read_store(record_store)
            statuses = tuple(
                SecretStatusReadModel.model_validate(status)
                for status in control_plane_secrets.list_secret_statuses(
                    secret_store,
                    context_name=context,
                    instance_name=instance,
                )
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return SecretStatusListResponse(
            trace_id=trace_id,
            context=context,
            instance=instance,
            secrets=statuses,
        )

    def accepted_evidence_response(
        *,
        trace_id: str,
        records: dict[str, str],
        result: dict[str, object] | None = None,
        replayed: bool = False,
        original_trace_id: str = "",
    ) -> AcceptedEvidenceResponse:
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records=records,
            result=result,
            replayed=True if replayed else None,
            original_trace_id=original_trace_id or None,
        )

    def replay_idempotent_response(
        *, trace_id: str, stored_record: LaunchplaneIdempotencyRecord
    ) -> AcceptedEvidenceResponse:
        stored_records = {
            str(key): str(value)
            for key, value in dict(stored_record.response_payload.get("records") or {}).items()
        }
        stored_result = stored_record.response_payload.get("result")
        return accepted_evidence_response(
            trace_id=trace_id,
            records=stored_records,
            result=stored_result if isinstance(stored_result, dict) else None,
            replayed=True,
            original_trace_id=stored_record.response_trace_id,
        )

    async def write_deployment_evidence(
        request: Request,
        deployment_request: DeploymentEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="deployment.write",
            product=deployment_request.product,
            context=deployment_request.deployment.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write deployment evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_deployment_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        records = {
            str(key): str(value)
            for key, value in apply_deployment_evidence(
                record_store=evidence_store,
                deployment_record=deployment_request.deployment,
            ).items()
        }
        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_backup_gate_evidence(
        request: Request,
        backup_gate_request: BackupGateEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="backup_gate.write",
            product=backup_gate_request.product,
            context=backup_gate_request.backup_gate.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write backup gate evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_BACKUP_GATE_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_backup_gate_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_backup_gate_record(backup_gate_request.backup_gate)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"backup_gate_record_id": backup_gate_request.backup_gate.record_id},
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_BACKUP_GATE_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_promotion_evidence(
        request: Request,
        promotion_request: PromotionEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="promotion.write",
            product=promotion_request.product,
            context=promotion_request.promotion.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write promotion evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PROMOTION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_promotion_evidence_store(
                record_store,
                promotion_request.promotion,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_promotion_evidence(
                    record_store=evidence_store,
                    promotion_record=promotion_request.promotion,
                ).items()
            }
        except PromotionEvidenceValidationError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PROMOTION_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_preview_generation_evidence(
        request: Request,
        preview_generation_request: PreviewGenerationEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_generation.write",
            product=preview_generation_request.product,
            context=preview_generation_request.preview.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write preview generation evidence for the "
                    "requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PREVIEW_GENERATION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_preview_generation_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_launchplane_generation_evidence(
                    control_plane_root_path=resolved_control_plane_root,
                    record_store=evidence_store,
                    preview_request=preview_generation_request.preview,
                    generation_request=preview_generation_request.generation,
                ).items()
            }
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PREVIEW_GENERATION_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_preview_destroyed_evidence(
        request: Request,
        preview_destroyed_request: PreviewDestroyedEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="preview_destroyed.write",
            product=preview_destroyed_request.product,
            context=preview_destroyed_request.destroy.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write preview destroyed evidence for the "
                    "requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_PREVIEW_DESTROYED_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_preview_destroyed_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_launchplane_destroy_preview(
                    record_store=evidence_store,
                    request=preview_destroyed_request.destroy,
                ).items()
            }
        except click.ClickException as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error).strip() or "Request could not be completed.",
            ) from error

        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_PREVIEW_DESTROYED_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_runner_host_hygiene_audit_evidence(
        request: Request,
        runner_host_hygiene_request: RunnerHostHygieneAuditEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="runner_host_hygiene_audit.write",
            product=runner_host_hygiene_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write runner host hygiene audit evidence.",
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_runner_host_hygiene_audit_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_runner_host_hygiene_audit_record(runner_host_hygiene_request.audit)
        records = {
            "runner_host_hygiene_audit_record_key": (
                runner_host_hygiene_request.audit.audit_record_key
            ),
        }
        result: dict[str, object] = {
            "runner_host_hygiene_audit_record_key": (
                runner_host_hygiene_request.audit.audit_record_key
            ),
            "host_name": runner_host_hygiene_request.audit.request.host_name,
            "audit_status": runner_host_hygiene_request.audit.status,
            "mutate": runner_host_hygiene_request.audit.request.mutate,
            "audit": runner_host_hygiene_request.audit.model_dump(mode="json"),
        }
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    async def write_runner_lane_registration_audit_evidence(
        request: Request,
        runner_lane_registration_request: RunnerLaneRegistrationAuditEvidenceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="runner_lane_registration_audit.write",
            product=runner_lane_registration_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot write runner lane registration audit evidence.",
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_runner_lane_registration_audit_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        evidence_store.write_runner_lane_registration_audit_record(
            runner_lane_registration_request.audit
        )
        records = {
            "runner_lane_registration_audit_record_key": (
                runner_lane_registration_request.audit.audit_record_key
            ),
        }
        result: dict[str, object] = {
            "runner_lane_registration_audit_record_key": (
                runner_lane_registration_request.audit.audit_record_key
            ),
            "repository": runner_lane_registration_request.audit.request.repository,
            "host_name": runner_lane_registration_request.audit.request.host_name,
            "lane_name": runner_lane_registration_request.audit.request.lane_name,
            "audit_status": runner_lane_registration_request.audit.status,
            "mutate": runner_lane_registration_request.audit.request.mutate,
            "audit": runner_lane_registration_request.audit.model_dump(mode="json"),
        }
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    def read_health(record_store: Annotated[object, Depends(get_record_store)]) -> HealthResponse:
        return HealthResponse(
            trace_id=next_trace_id(),
            storage_backend=storage_backend_name(record_store),
        )

    def preserve_renewed_session_cookie(request: Request, response: JSONResponse) -> None:
        renewed_session_cookie = getattr(
            request.state,
            "launchplane_renewed_session_cookie",
            "",
        )
        if renewed_session_cookie:
            response.headers.append("Set-Cookie", str(renewed_session_cookie))

    app.add_api_route(
        "/v1/health",
        read_health,
        methods=["GET"],
        response_model=HealthResponse,
        operation_id="read_launchplane_health",
        summary="Read Launchplane service health",
    )

    app.add_api_route(
        "/v1/artifacts/protected",
        read_protected_artifacts,
        methods=["GET"],
        response_model=ProtectedArtifactsResponse,
        operation_id="read_protected_artifacts",
        summary="Read protected artifact inventory",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers",
        read_driver_descriptors,
        methods=["GET"],
        response_model=DriverDescriptorsResponse,
        operation_id="read_driver_descriptors",
        summary="Read Launchplane driver descriptors",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers/{driver_id}",
        read_driver_descriptor,
        methods=["GET"],
        response_model=DriverDescriptorResponse,
        operation_id="read_driver_descriptor",
        summary="Read one Launchplane driver descriptor",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/driver-view",
        read_driver_context_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_context_view",
        summary="Read Launchplane driver view for a context",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/driver-view",
        read_driver_instance_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_instance_view",
        summary="Read Launchplane driver view for one context instance",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/deployments/{record_id}",
        read_deployment_record,
        methods=["GET"],
        response_model=DeploymentRecordResponse,
        operation_id="read_deployment_record",
        summary="Read one deployment record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/promotions/{record_id}",
        read_promotion_record,
        methods=["GET"],
        response_model=PromotionRecordResponse,
        operation_id="read_promotion_record",
        summary="Read one promotion record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/previews/{preview_id}",
        read_preview_record,
        methods=["GET"],
        response_model=PreviewRecordResponse,
        operation_id="read_preview_record",
        summary="Read one preview record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/previews/{preview_id}/history",
        read_preview_history,
        methods=["GET"],
        response_model=PreviewHistoryResponse,
        operation_id="read_preview_history",
        summary="Read one preview record with generation history",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/inventory/{context}/{instance}",
        read_environment_inventory,
        methods=["GET"],
        response_model=EnvironmentInventoryResponse,
        operation_id="read_environment_inventory",
        summary="Read one environment inventory record",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/operations/recent",
        read_recent_operations,
        methods=["GET"],
        response_model=RecentOperationsResponse,
        operation_id="read_recent_operations",
        summary="Read recent Launchplane operations for a context",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/product-profiles/{product}/context-cutover-audit",
        read_product_context_cutover_audit,
        methods=["GET"],
        response_model=ProductContextCutoverAuditResponse,
        operation_id="read_product_context_cutover_audit",
        summary="Read a product context cutover audit",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/secrets",
        list_context_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_context_secret_statuses",
        summary="List Launchplane managed secret status for a context",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/secrets",
        list_instance_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_instance_secret_statuses",
        summary="List Launchplane managed secret status for an instance",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/secrets/{secret_id}",
        read_secret_status,
        methods=["GET"],
        response_model=SecretStatusResponse,
        operation_id="read_secret_status",
        summary="Read Launchplane managed secret status",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _BACKUP_GATE_EVIDENCE_ROUTE,
        write_backup_gate_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_backup_gate_evidence",
        summary="Write backup gate evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PROMOTION_EVIDENCE_ROUTE,
        write_promotion_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_promotion_evidence",
        summary="Write promotion evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_GENERATION_EVIDENCE_ROUTE,
        write_preview_generation_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_generation_evidence",
        summary="Write preview generation evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _PREVIEW_DESTROYED_EVIDENCE_ROUTE,
        write_preview_destroyed_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_destroyed_evidence",
        summary="Write preview destroyed evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
        write_runner_host_hygiene_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_host_hygiene_audit_evidence",
        summary="Write runner host hygiene audit evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
        write_runner_lane_registration_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_lane_registration_audit_evidence",
        summary="Write runner lane registration audit evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _DEPLOYMENT_EVIDENCE_ROUTE,
        write_deployment_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_deployment_evidence",
        summary="Write deployment evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    def launchplane_http_exception_handler(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, HTTPException):
            raise error
        http_error = error
        trace_id = next_trace_id()
        code = "authentication_required" if http_error.status_code == 401 else "http_error"
        if isinstance(http_error.detail, dict):
            detail = http_error.detail
            trace_id = str(detail.get("trace_id", trace_id))
            code = str(detail.get("code", code))
            message = str(detail.get("message", "Launchplane request failed."))
        else:
            message = str(http_error.detail)
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        response = JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json"),
            headers=http_error.headers,
        )
        preserve_renewed_session_cookie(request, response)
        return response

    def launchplane_request_validation_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        payload = LaunchplaneErrorResponse(
            trace_id=next_trace_id(),
            error=LaunchplaneErrorDetail(
                code="invalid_request",
                message="Launchplane request validation failed.",
            ),
        )
        response = JSONResponse(
            status_code=400,
            content=payload.model_dump(mode="json"),
        )
        preserve_renewed_session_cookie(request, response)
        return response

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        response_model=ProductEnvironmentConfigStatusResponse,
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_exception_handler(HTTPException, launchplane_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def _launchplane_http_error(
    *, status_code: int, trace_id: str, code: str, message: str
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"trace_id": trace_id, "code": code, "message": message},
    )


def _authentication_required_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
