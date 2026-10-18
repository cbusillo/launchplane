"""The signed-in administrator's testing-only adapter to the native worker."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.build_provenance import BuildProvenanceError, record_verified_build_artifact
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.drivers.native_routes import _descriptor_driver_route_authz_action
from control_plane.durable_operation_authorization import (
    capture_durable_operation_authorization,
    read_active_authz_policy_record,
)
from control_plane.http_routes.mutation_support import idempotency_scope, request_fingerprint
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope,
    OdooTargetReplacementApplyIdempotencyKeyReusedError,
    OdooTargetReplacementApplyCurrentArtifactChangedError,
    OdooTargetReplacementApplyLaneBusyError,
    OdooTargetReplacementApplyOperationActiveError,
    enqueue_odoo_target_replacement_apply_operation,
    find_odoo_target_replacement_apply_operation_by_idempotency_key,
)
from control_plane.odoo_target_replacement_plan_http import (
    resolve_odoo_target_replacement_plan_lane,
)
from control_plane.product_reconcile import ProductReconcileError, latest_verified_testing_build
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_preflight import TestingRebuildPreflight, inspect_testing_rebuild
from control_plane.testing_rebuild_service import TestingRebuildRefused
from control_plane.workflows.ship import utc_now_timestamp

TestingRebuildSource = Literal["empty", "production_restore", "upstream_restore"]
TESTING_REBUILD_ROUTE = "/v1/products/{product}/testing-rebuild"


class TestingRebuildSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_source_mode: TestingRebuildSource
    artifact_id: str = Field(min_length=1)
    source_git_ref: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def concrete_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A concrete rebuild reason is required.")
        return value.strip()


class TestingRebuildOperationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    product: str
    instance: Literal["testing"] = "testing"
    status: str
    phase: str
    updated_at: str


class TestingRebuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    operation: TestingRebuildOperationView


@dataclass(frozen=True)
class TestingRebuildRouteDependencies:
    common: ReadRouteDependencies
    mutation_identity: Callable[..., LaunchplaneIdentity]
    root: FilePath


def register_testing_rebuild_routes(
    app: ApiRouteRegistrar, *, dependencies: TestingRebuildRouteDependencies
) -> None:
    common = dependencies.common
    action = _descriptor_driver_route_authz_action(ODOO_TARGET_REPLACEMENT_APPLY_ROUTE)

    def require_admin(
        identity: LaunchplaneIdentity, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Testing rebuild storage is unavailable.",
            )
        try:
            policy = read_active_authz_policy_record(record_store).policy
        except (LookupError, TypeError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="The current administrator policy is unavailable.",
            ) from error
        if not isinstance(identity, GitHubHumanIdentity) or not policy.names_administrator(
            identity
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Only the signed-in administrator can use this testing control.",
            )
        return record_store

    def replacement(
        product: str, payload: TestingRebuildSubmit
    ) -> OdooStableTargetReplacementApplyRequest:
        return OdooStableTargetReplacementApplyRequest(
            product=product,
            instance="testing",
            strategy="replace-and-cutover",
            data_source_mode=payload.data_source_mode,
            allow_empty_data=payload.data_source_mode == "empty",
            artifact_id=payload.artifact_id,
            source_git_ref=payload.source_git_ref,
            reason=payload.reason,
        )

    def response(
        operation: OdooStableTargetReplacementOperationRecord, trace_id: str
    ) -> TestingRebuildResponse:
        return TestingRebuildResponse(
            trace_id=trace_id,
            operation=TestingRebuildOperationView(
                operation_id=operation.operation_id,
                product=operation.product,
                status=operation.status,
                phase=operation.phase,
                updated_at=operation.updated_at,
            ),
        )

    def inspect(
        product: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        data_source_mode: Annotated[TestingRebuildSource, Query()] = "empty",
    ) -> TestingRebuildPreflight:
        trace_id = common.next_trace_id()
        store = require_admin(identity, record_store, trace_id)
        try:
            profile = store.read_product_profile_record(product)
            lane = resolve_odoo_target_replacement_plan_lane(
                record_store=store, product=product, instance="testing"
            )
            verified = latest_verified_testing_build(store, profile, lane)
            request = replacement(
                product,
                TestingRebuildSubmit(
                    data_source_mode=data_source_mode,
                    artifact_id=verified.manifest.artifact_id,
                    source_git_ref=verified.manifest.source_commit,
                    reason="Inspect testing rebuild",
                ),
            )
            return inspect_testing_rebuild(
                root=dependencies.root,
                store=store,
                request=request,
                verified_manifest=verified.manifest,
            )
        except TestingRebuildRefused as error:
            raise common.http_error(
                status_code=409, trace_id=trace_id, code=error.code, message=str(error)
            ) from error
        except NativeSourceAuthorizationDenied as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="The existing provider identity refused the testing preflight read.",
            ) from error
        except (ValueError, LookupError, ProductReconcileError, BuildProvenanceError) as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="testing_rebuild_not_ready",
                message="Testing rebuild inputs are unavailable or protected. Check the lane evidence.",
            ) from error

    def submit(
        product: Annotated[str, Path(min_length=1)],
        payload: TestingRebuildSubmit,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> TestingRebuildResponse:
        trace_id = common.next_trace_id()
        store = require_admin(identity, record_store, trace_id)
        if not idempotency_key.strip():
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Testing rebuild requires its original request key.",
            )
        scope = idempotency_scope(identity)
        fingerprint = request_fingerprint({"product": product, **payload.model_dump(mode="json")})
        existing = find_odoo_target_replacement_apply_operation_by_idempotency_key(
            operation_store=store,
            idempotency_key=idempotency_key.strip(),
            idempotency_scope=scope,
        )
        if existing is not None:
            if (
                existing.request_fingerprint != fingerprint
                or existing.product != product
                or not existing.is_native_testing_rebuild
            ):
                raise common.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message="This request key already identifies another operation.",
                )
            return response(existing, trace_id)
        try:
            lane = resolve_odoo_target_replacement_plan_lane(
                record_store=store, product=product, instance="testing"
            )
            now = utc_now_timestamp()
            authorization = capture_durable_operation_authorization(
                identity=identity,
                action=action,
                product=product,
                context=lane.context,
                instances=("testing",),
                policy_record=read_active_authz_policy_record(store),
                authorized_at=now,
            )
            request = replacement(product, payload)
            verified = latest_verified_testing_build(
                store, store.read_product_profile_record(product), lane
            )
            if (verified.manifest.artifact_id, verified.manifest.source_commit) != (
                payload.artifact_id,
                payload.source_git_ref,
            ):
                raise ValueError("The inspected build changed.")
            inspect_testing_rebuild(
                root=dependencies.root,
                store=store,
                request=request,
                verified_manifest=verified.manifest,
            )
            record_verified_build_artifact(record_store=store, verified=verified)
        except TestingRebuildRefused as error:
            raise common.http_error(
                status_code=409, trace_id=trace_id, code=error.code, message=str(error)
            ) from error
        except NativeSourceAuthorizationDenied as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="The existing provider identity refused the testing preflight read.",
            ) from error
        except (ValueError, LookupError, ProductReconcileError, BuildProvenanceError) as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="testing_rebuild_not_ready",
                message="Testing rebuild was not admitted. Refresh the lane evidence before another request.",
            ) from error

        try:
            records, _ = enqueue_odoo_target_replacement_apply_operation(
                record_store=store,
                request=OdooTargetReplacementApplyEnvelope(product=product, replacement=request),
                context=lane.context,
                idempotency_key=idempotency_key.strip(),
                idempotency_scope=scope,
                request_fingerprint=fingerprint,
                created_at=now,
                authorization=authorization,
            )
        except OdooTargetReplacementApplyIdempotencyKeyReusedError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message="This request key already identifies another operation.",
            ) from error
        except (
            OdooTargetReplacementApplyCurrentArtifactChangedError,
            OdooTargetReplacementApplyLaneBusyError,
            OdooTargetReplacementApplyOperationActiveError,
        ) as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="testing_rebuild_not_ready",
                message="Testing changed or another operation holds the lane. Inspect again.",
            ) from error
        operation_id = str(records["odoo_stable_target_replacement_operation_id"])
        return response(
            store.read_odoo_stable_target_replacement_operation_record(operation_id), trace_id
        )

    def read_operation(
        product: Annotated[str, Path(min_length=1)],
        operation_id: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> TestingRebuildResponse:
        trace_id = common.next_trace_id()
        store = require_admin(identity, record_store, trace_id)
        try:
            operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
            if operation.product != product or not operation.is_native_testing_rebuild:
                raise FileNotFoundError(operation_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="This testing rebuild was not found.",
            ) from error
        return response(operation, trace_id)

    app.add_api_route(
        TESTING_REBUILD_ROUTE + "/inputs",
        inspect,
        methods=["GET"],
        operation_id="inspect_testing_rebuild",
        response_model=TestingRebuildPreflight,
    )
    app.add_api_route(
        TESTING_REBUILD_ROUTE,
        submit,
        methods=["POST"],
        operation_id="submit_testing_rebuild",
        response_model=TestingRebuildResponse,
    )
    app.add_api_route(
        TESTING_REBUILD_ROUTE + "/operations/{operation_id}",
        read_operation,
        methods=["GET"],
        operation_id="read_testing_rebuild",
        response_model=TestingRebuildResponse,
    )
