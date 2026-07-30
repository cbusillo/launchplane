from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, cast

import click
from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.preview_evidence import (
    PreviewDestroyedEvidenceEnvelope,
    PreviewGenerationEvidenceEnvelope,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import (
    sanitize_runner_host_hygiene_audit_record_for_persistence,
)
from control_plane.contracts.runner_host_hygiene_evidence import (
    RunnerHostHygieneAuditEvidenceEnvelope,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration_evidence import (
    RunnerLaneRegistrationAuditEvidenceEnvelope,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_capable_store,
    idempotency_scope,
    replay_idempotent_response,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT as _LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
)
from control_plane.launchplane_mutations import (
    LaunchplaneDestroyPreviewStore,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    PromotionEvidenceValidationError,
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.ship import utc_now_timestamp


DEPLOYMENT_EVIDENCE_ROUTE = "/v1/evidence/deployments"
BACKUP_GATE_EVIDENCE_ROUTE = "/v1/evidence/backup-gates"
PROMOTION_EVIDENCE_ROUTE = "/v1/evidence/promotions"
PREVIEW_GENERATION_EVIDENCE_ROUTE = "/v1/evidence/previews/generations"
PREVIEW_DESTROYED_EVIDENCE_ROUTE = "/v1/evidence/previews/destroyed"
RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-host-hygiene/audits"
RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE = "/v1/evidence/runner-lane-registration/audits"
EVIDENCE_INGRESS_ROUTES = frozenset(
    {
        DEPLOYMENT_EVIDENCE_ROUTE,
        BACKUP_GATE_EVIDENCE_ROUTE,
        PROMOTION_EVIDENCE_ROUTE,
        PREVIEW_GENERATION_EVIDENCE_ROUTE,
        PREVIEW_DESTROYED_EVIDENCE_ROUTE,
        RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
        RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
    }
)


class DeploymentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    @model_validator(mode="before")
    @classmethod
    def _reject_target_reference_compatibility_input(cls, data: object) -> object:
        if _mapping_path_contains_key(data, "deployment", "deploy", "target_reference"):
            raise ValueError(
                "deployment evidence ingress rejects target_reference compatibility input"
            )
        return data

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")


def _mapping_path_contains_key(payload: object, *path: str) -> bool:
    if not path or not isinstance(payload, Mapping):
        return False
    *parent_path, terminal_key = path
    current: object = payload
    for path_part in parent_path:
        if not isinstance(current, Mapping):
            return False
        current = current.get(path_part)
    return isinstance(current, Mapping) and terminal_key in current


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

    @model_validator(mode="before")
    @classmethod
    def _reject_target_reference_compatibility_input(cls, data: object) -> object:
        if _mapping_path_contains_key(data, "promotion", "deploy", "target_reference"):
            raise ValueError(
                "promotion evidence ingress rejects target_reference compatibility input"
            )
        return data

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")


class BackupGateEvidenceStore(Protocol):
    def write_backup_gate_record(self, record: BackupGateRecord) -> object: ...


class RunnerHostHygieneAuditEvidenceStore(Protocol):
    def write_runner_host_hygiene_audit_record(
        self,
        record: RunnerHostHygieneApplyAuditRecord,
    ) -> object: ...


class RunnerLaneRegistrationAuditEvidenceStore(Protocol):
    def write_runner_lane_registration_audit_record(
        self,
        record: RunnerLaneRegistrationAuditRecord,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class EvidenceWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]
    control_plane_root: Path


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


def require_backup_gate_evidence_store(record_store: object) -> BackupGateEvidenceStore:
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
    return cast(BackupGateEvidenceStore, record_store)


def require_runner_host_hygiene_audit_evidence_store(
    record_store: object,
) -> RunnerHostHygieneAuditEvidenceStore:
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
    return cast(RunnerHostHygieneAuditEvidenceStore, record_store)


def require_runner_lane_registration_audit_evidence_store(
    record_store: object,
) -> RunnerLaneRegistrationAuditEvidenceStore:
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
    return cast(RunnerLaneRegistrationAuditEvidenceStore, record_store)


def register_evidence_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: EvidenceWriteRouteDependencies,
) -> None:
    async def write_deployment_evidence(
        request: Request,
        deployment_request: DeploymentEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="deployment.write",
            product=deployment_request.product,
            context=deployment_request.deployment.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(deployment_request.deployment.instance,),
            ),
        ):
            raise dependencies.http_error(
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
                route_path=DEPLOYMENT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
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
                    route_path=DEPLOYMENT_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="backup_gate.write",
            product=backup_gate_request.product,
            context=backup_gate_request.backup_gate.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(backup_gate_request.backup_gate.instance,),
            ),
        ):
            raise dependencies.http_error(
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
                route_path=BACKUP_GATE_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
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
                    route_path=BACKUP_GATE_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="promotion.write",
            product=promotion_request.product,
            context=promotion_request.promotion.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(
                    promotion_request.promotion.from_instance,
                    promotion_request.promotion.to_instance,
                ),
            ),
        ):
            raise dependencies.http_error(
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
                route_path=PROMOTION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
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
            raise dependencies.http_error(
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
                    route_path=PROMOTION_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="preview_generation.write",
            product=preview_generation_request.product,
            context=preview_generation_request.preview.context,
        ):
            raise dependencies.http_error(
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
                route_path=PREVIEW_GENERATION_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            records = {
                str(key): str(value)
                for key, value in apply_launchplane_generation_evidence(
                    control_plane_root_path=dependencies.control_plane_root,
                    record_store=evidence_store,
                    preview_request=preview_generation_request.preview,
                    generation_request=preview_generation_request.generation,
                ).items()
            }
        except click.ClickException as error:
            raise dependencies.http_error(
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
                    route_path=PREVIEW_GENERATION_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="preview_destroyed.write",
            product=preview_destroyed_request.product,
            context=preview_destroyed_request.destroy.context,
        ):
            raise dependencies.http_error(
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
                route_path=PREVIEW_DESTROYED_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
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
            raise dependencies.http_error(
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
                    route_path=PREVIEW_DESTROYED_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="runner_host_hygiene_audit.write",
            product=runner_host_hygiene_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
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
                route_path=RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        persisted_audit = sanitize_runner_host_hygiene_audit_record_for_persistence(
            runner_host_hygiene_request.audit
        )
        evidence_store.write_runner_host_hygiene_audit_record(persisted_audit)
        records = {
            "runner_host_hygiene_audit_record_key": persisted_audit.audit_record_key,
        }
        result: dict[str, object] = {
            "runner_host_hygiene_audit_record_key": persisted_audit.audit_record_key,
            "host_name": persisted_audit.request.host_name,
            "audit_status": persisted_audit.status,
            "mutate": persisted_audit.request.mutate,
            "audit": persisted_audit.model_dump(mode="json"),
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
                    route_path=RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="runner_lane_registration_audit.write",
            product=runner_lane_registration_request.product,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
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
                route_path=RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise dependencies.http_error(
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
            raise dependencies.http_error(
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
            "operation": runner_lane_registration_request.audit.request.operation,
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
                    route_path=RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    evidence_ingress_error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        409: {"model": dependencies.error_response_model},
        413: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }

    app.add_api_route(
        BACKUP_GATE_EVIDENCE_ROUTE,
        write_backup_gate_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_backup_gate_evidence",
        summary="Write backup gate evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        PROMOTION_EVIDENCE_ROUTE,
        write_promotion_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_promotion_evidence",
        summary="Write promotion evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        PREVIEW_GENERATION_EVIDENCE_ROUTE,
        write_preview_generation_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_generation_evidence",
        summary="Write preview generation evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        PREVIEW_DESTROYED_EVIDENCE_ROUTE,
        write_preview_destroyed_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_preview_destroyed_evidence",
        summary="Write preview destroyed evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        RUNNER_HOST_HYGIENE_AUDIT_EVIDENCE_ROUTE,
        write_runner_host_hygiene_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_host_hygiene_audit_evidence",
        summary="Write runner host hygiene audit evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        RUNNER_LANE_REGISTRATION_AUDIT_EVIDENCE_ROUTE,
        write_runner_lane_registration_audit_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_runner_lane_registration_audit_evidence",
        summary="Write runner lane registration audit evidence",
        responses=evidence_ingress_error_responses,
    )

    app.add_api_route(
        DEPLOYMENT_EVIDENCE_ROUTE,
        write_deployment_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_deployment_evidence",
        summary="Write deployment evidence",
        responses=evidence_ingress_error_responses,
    )
