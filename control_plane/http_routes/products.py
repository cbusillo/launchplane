from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane import product_read_service as control_plane_product_read_service
from control_plane.agent_context_service import (
    AgentContextPayload,
    build_agent_context_service_payload,
)
from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_environment_read_model import (
    ProductActivityReadModel,
    ProductEnvironmentConfigStatus,
    ProductEnvironmentDetail,
    ProductEnvironmentSummary,
    ProductReadModelStore,
    ProductSiteOverview,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.protected_artifacts import (
    ProtectedArtifactSet,
    ProtectedArtifactStore,
    build_protected_artifact_set,
)
from control_plane.contracts.repo_product_mapping_read_model import RepoProductMapping
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import LaunchplaneIdentity, TerminalAgentIdentity
from control_plane.product_promotion_http import (
    PRODUCT_PROMOTION_STATUS_ROUTE,
    PRODUCT_PROMOTION_WORKFLOW_STATUS_ROUTE,
    ProductPromotionStatus,
    ProductPromotionWorkflowDeliveryStatusResponse,
    build_product_promotion_status,
    product_promotion_delivery_status,
    resolve_product_promotion_target,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.work_graph_service import (
    WorkGraphPlanningFactsProvider,
    build_repo_product_mapping_service_payload,
)
from control_plane.workflows.ship import utc_now_timestamp


@dataclass(frozen=True, slots=True)
class ProductReadRouteDependencies:
    common: ReadRouteDependencies
    read_product_profile_list_identity: Callable[..., LaunchplaneIdentity | None]
    work_graph_planning_facts_provider: WorkGraphPlanningFactsProvider | None
    workflow_credentials_ready: Callable[[str], bool]


class ProductEnvironmentConfigStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    config_status: ProductEnvironmentConfigStatus


class ProductPromotionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    promotion_status: ProductPromotionStatus


class ProductEnvironmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    products: tuple[ProductSiteOverview, ...]


class ProductOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: ProductSiteOverview


class ProductActivityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    activity: ProductActivityReadModel


class ProductEnvironmentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    display_name: str
    repository: str
    driver_id: str
    base_driver_id: str = ""
    environments: tuple[ProductEnvironmentSummary, ...]
    trust_state: FreshnessStatus
    provenance: DataProvenance


class ProductEnvironmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    environment: ProductEnvironmentDetail


class RepoProductMappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    mapping: RepoProductMapping
    source: dict[str, object]


class AgentContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: AgentContextPayload


class ProductProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver_id: str
    profiles: tuple[LaunchplaneProductProfileRecord, ...]


class ProductProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    profile: LaunchplaneProductProfileRecord


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


class RepoProductMappingReadStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class AgentContextReadStore(ProductReadModelStore, Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


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


def require_product_profile_list_store(record_store: object) -> ProductReadModelStore:
    list_records = getattr(record_store, "list_product_profile_records", None)
    if not callable(list_records):
        raise TypeError(
            "Launchplane record store does not support product profile list reads: "
            "list_product_profile_records"
        )
    return cast(ProductReadModelStore, record_store)


def require_product_profile_read_store(record_store: object) -> ProductReadModelStore:
    read_record = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support product profile reads: "
            "read_product_profile_record"
        )
    return cast(ProductReadModelStore, record_store)


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


def _build_product_environment_read_result(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    record_store: object,
    trace_id: str,
    params: dict[str, str],
) -> control_plane_product_read_service.ProductEnvironmentReadServiceResult:
    def action_allowed(
        requested_action: str,
        requested_product: str,
        requested_context: str,
    ) -> bool:
        if requested_action.startswith("product_config.") and isinstance(
            identity, TerminalAgentIdentity
        ):
            return False
        return dependencies.authorization_allows(
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
        return control_plane_product_read_service.build_product_environment_read_service_result(
            record_store=product_read_store,
            params=params,
            action_allowed=action_allowed,
        )
    except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
        raise dependencies.http_error(
            status_code=503,
            trace_id=trace_id,
            code="database_storage_required",
            message=str(error),
        ) from error
    except FileNotFoundError as error:
        raise dependencies.http_error(
            status_code=404,
            trace_id=trace_id,
            code="not_found",
            message=str(error),
        ) from error
    except ValueError as error:
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message=str(error),
        ) from error


def _require_product_environment_result_authorization(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    result: control_plane_product_read_service.ProductEnvironmentReadServiceResult,
) -> None:
    if dependencies.authorization_allows(
        identity=identity,
        action="product_environment.read",
        product=result.authorization_product,
        context=result.authorization_context,
    ):
        return
    raise dependencies.http_error(
        status_code=403,
        trace_id=trace_id,
        code="authorization_denied",
        message=result.denial_message,
    )


def _require_agent_context_read_authorization(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    message: str,
) -> None:
    if dependencies.authorization_allows(
        identity=identity,
        action="product_environment.read",
        product=LAUNCHPLANE_SERVICE_CONTEXT,
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return
    raise dependencies.http_error(
        status_code=403,
        trace_id=trace_id,
        code="authorization_denied",
        message=message,
    )


def _require_database_read_store_methods(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
    method_names: tuple[str, ...],
    message: str,
) -> None:
    missing_methods = tuple(
        method_name
        for method_name in method_names
        if not callable(getattr(record_store, method_name, None))
    )
    if not missing_methods:
        if isinstance(record_store, PostgresRecordStore):
            return
        missing_methods = ("postgres_storage",)
    missing_method_list = ", ".join(missing_methods)
    raise dependencies.http_error(
        status_code=503,
        trace_id=trace_id,
        code="database_storage_required",
        message=f"{message} Missing store method(s): {missing_method_list}.",
    )


def _require_repo_product_mapping_read_store(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
) -> RepoProductMappingReadStore:
    _require_database_read_store_methods(
        record_store,
        dependencies=dependencies,
        trace_id=trace_id,
        method_names=(
            "list_product_profile_records",
            "list_every_code_work_request_records",
        ),
        message="Repo product mapping reads require a database-backed record store.",
    )
    return cast(RepoProductMappingReadStore, record_store)


def _require_agent_context_read_store(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
) -> AgentContextReadStore:
    _require_database_read_store_methods(
        record_store,
        dependencies=dependencies,
        trace_id=trace_id,
        method_names=(
            "list_product_profile_records",
            "read_product_profile_record",
            "list_every_code_work_request_records",
            "list_every_code_preview_gate_records",
        ),
        message="Agent context reads require a database-backed record store.",
    )
    return cast(AgentContextReadStore, record_store)


def register_protected_artifact_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_protected_artifacts(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> ProtectedArtifactsResponse:
        trace_id = common.next_trace_id()
        requested_product = product.strip()
        requested_context = context.strip()
        if not requested_product:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Protected artifact inventory requires a product query parameter.",
            )
        authz_context = requested_context or "*"
        if not common.authorization_allows(
            identity=identity,
            action="artifact_protection.read",
            product=requested_product,
            context=authz_context,
        ):
            raise common.http_error(
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
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ProtectedArtifactsResponse(
            trace_id=trace_id,
            protected_artifacts=protected_artifacts,
        )

    app.add_api_route(
        "/v1/artifacts/protected",
        read_protected_artifacts,
        methods=["GET"],
        response_model=ProtectedArtifactsResponse,
        operation_id="read_protected_artifacts",
        summary="Read protected artifact inventory",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_agent_context_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_repo_product_mapping(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> RepoProductMappingResponse:
        trace_id = common.next_trace_id()
        _require_agent_context_read_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane repo product mapping.",
        )
        mapping_store = _require_repo_product_mapping_read_store(
            record_store,
            dependencies=common,
            trace_id=trace_id,
        )
        payload = build_repo_product_mapping_service_payload(
            generated_at=utc_now_timestamp(),
            product_store=mapping_store,
            work_request_store=mapping_store,
        )
        return RepoProductMappingResponse(
            trace_id=trace_id,
            mapping=RepoProductMapping.model_validate(payload["mapping"]),
            source=cast(dict[str, object], payload["source"]),
        )

    def read_agent_context(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        repository: Annotated[str, Query()] = "",
    ) -> AgentContextResponse:
        trace_id = common.next_trace_id()
        _require_agent_context_read_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read Launchplane agent context.",
        )
        context_store = _require_agent_context_read_store(
            record_store,
            dependencies=common,
            trace_id=trace_id,
        )

        def action_allowed(
            requested_action: str,
            requested_product: str,
            requested_context: str,
        ) -> bool:
            return common.authorization_allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        context = build_agent_context_service_payload(
            generated_at=utc_now_timestamp(),
            repository=repository,
            product_store=context_store,
            work_request_store=context_store,
            preview_readiness_store=context_store,
            action_allowed=action_allowed,
            planning_facts_provider=dependencies.work_graph_planning_facts_provider,
        )
        return AgentContextResponse(trace_id=trace_id, context=context)

    error_responses: dict[int | str, dict[str, object]] = {
        401: {"model": common.error_response_model},
        403: {"model": common.error_response_model},
        503: {"model": common.error_response_model},
    }

    app.add_api_route(
        "/v1/repo-product-mapping",
        read_repo_product_mapping,
        methods=["GET"],
        response_model=RepoProductMappingResponse,
        operation_id="read_repo_product_mapping",
        summary="Read repository product mapping",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/agent/context",
        read_agent_context,
        methods=["GET"],
        response_model=AgentContextResponse,
        operation_id="read_agent_context",
        summary="Read Launchplane agent context",
        responses=error_responses,
    )


def register_product_environment_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def list_product_environment_overviews(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductEnvironmentListResponse:
        trace_id = common.next_trace_id()

        def action_allowed(
            requested_action: str,
            requested_product: str,
            requested_context: str,
        ) -> bool:
            return common.authorization_allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        if not common.authorization_allows(
            identity=identity,
            action="product_environment.read",
            product=LAUNCHPLANE_SERVICE_CONTEXT,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list product overviews.",
            )
        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            payload = (
                control_plane_product_read_service.build_product_environment_list_service_payload(
                    record_store=product_read_store,
                    action_allowed=action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        products = tuple(
            ProductSiteOverview.model_validate(product)
            for product in cast(list[object], payload["products"])
        )
        return ProductEnvironmentListResponse(trace_id=trace_id, products=products)

    def read_product_overview(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductOverviewResponse:
        trace_id = common.next_trace_id()
        result = _build_product_environment_read_result(
            dependencies=common,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product},
        )
        _require_product_environment_result_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        overview = ProductSiteOverview.model_validate(result.payload["product"])
        return ProductOverviewResponse(trace_id=trace_id, product=overview)

    def read_product_activity(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductActivityResponse:
        trace_id = common.next_trace_id()
        result = _build_product_environment_read_result(
            dependencies=common,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "activity": "true"},
        )
        _require_product_environment_result_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        activity = ProductActivityReadModel.model_validate(result.payload["activity"])
        return ProductActivityResponse(trace_id=trace_id, activity=activity)

    def list_product_environments(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductEnvironmentsResponse:
        trace_id = common.next_trace_id()
        result = _build_product_environment_read_result(
            dependencies=common,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "environments": "true"},
        )
        _require_product_environment_result_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        payload = result.payload
        environments = tuple(
            ProductEnvironmentSummary.model_validate(environment)
            for environment in cast(list[object], payload["environments"])
        )
        provenance = DataProvenance.model_validate(payload["provenance"])
        return ProductEnvironmentsResponse(
            trace_id=trace_id,
            product=str(payload["product"]),
            display_name=str(payload["display_name"]),
            repository=str(payload["repository"]),
            driver_id=str(payload["driver_id"]),
            base_driver_id=str(payload.get("base_driver_id", "")),
            environments=environments,
            trust_state=cast(FreshnessStatus, payload["trust_state"]),
            provenance=provenance,
        )

    def read_product_environment(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductEnvironmentResponse:
        trace_id = common.next_trace_id()
        result = _build_product_environment_read_result(
            dependencies=common,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={"product": product, "environment": environment},
        )
        _require_product_environment_result_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            result=result,
        )
        environment_detail = ProductEnvironmentDetail.model_validate(result.payload["environment"])
        return ProductEnvironmentResponse(
            trace_id=trace_id,
            environment=environment_detail,
        )

    def list_product_profiles(
        identity: Annotated[
            LaunchplaneIdentity | None,
            Depends(dependencies.read_product_profile_list_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
        driver_id: Annotated[str, Query()] = "",
    ) -> ProductProfileListResponse:
        trace_id = common.next_trace_id()
        if identity is not None and not common.authorization_allows(
            identity=identity,
            action="product_profile.read",
            product="launchplane",
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot list Launchplane product profiles.",
            )
        normalized_driver_id = driver_id.strip()
        try:
            profile_store = require_product_profile_list_store(record_store)
            product_profile_payload = (
                control_plane_product_read_service.build_product_profile_list_service_payload(
                    record_store=profile_store,
                    driver_id=normalized_driver_id,
                )
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        payload_profiles = cast(list[object], product_profile_payload["profiles"])
        profiles = tuple(
            LaunchplaneProductProfileRecord.model_validate(profile) for profile in payload_profiles
        )
        return ProductProfileListResponse(
            trace_id=trace_id,
            driver_id=str(product_profile_payload["driver_id"]),
            profiles=profiles,
        )

    error_responses: dict[int | str, dict[str, object]] = {
        400: {"model": common.error_response_model},
        401: {"model": common.error_response_model},
        403: {"model": common.error_response_model},
        404: {"model": common.error_response_model},
        503: {"model": common.error_response_model},
    }

    app.add_api_route(
        "/v1/products",
        list_product_environment_overviews,
        methods=["GET"],
        response_model=ProductEnvironmentListResponse,
        operation_id="list_products",
        summary="List product environment overviews",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/products/{product}",
        read_product_overview,
        methods=["GET"],
        response_model=ProductOverviewResponse,
        operation_id="read_product",
        summary="Read a product environment overview",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/products/{product}/activity",
        read_product_activity,
        methods=["GET"],
        response_model=ProductActivityResponse,
        operation_id="read_product_activity",
        summary="Read product activity",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/products/{product}/environments",
        list_product_environments,
        methods=["GET"],
        response_model=ProductEnvironmentsResponse,
        operation_id="list_product_environments",
        summary="List product environments",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/products/{product}/environments/{environment}",
        read_product_environment,
        methods=["GET"],
        response_model=ProductEnvironmentResponse,
        operation_id="read_product_environment",
        summary="Read one product environment",
        responses=error_responses,
    )
    app.add_api_route(
        "/v1/product-profiles",
        list_product_profiles,
        methods=["GET"],
        response_model=ProductProfileListResponse,
        operation_id="list_product_profiles",
        summary="List Launchplane product profiles",
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_product_profile_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_product_profile(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductProfileResponse:
        trace_id = common.next_trace_id()
        try:
            profile_store = require_product_profile_read_store(record_store)
            profile = profile_store.read_product_profile_record(product)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not common.authorization_allows(
            identity=identity,
            action="product_profile.read",
            product=profile.product,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested product profile.",
            )
        return ProductProfileResponse(trace_id=trace_id, profile=profile)

    app.add_api_route(
        "/v1/product-profiles/{product}",
        read_product_profile,
        methods=["GET"],
        response_model=ProductProfileResponse,
        operation_id="read_product_profile",
        summary="Read a Launchplane product profile",
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_product_context_audit_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_product_context_cutover_audit(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        source_context: Annotated[str, Query()] = "",
        target_context: Annotated[str, Query()] = "",
        preview_context: Annotated[str, Query()] = "",
    ) -> ProductContextCutoverAuditResponse:
        trace_id = common.next_trace_id()
        try:
            audit_store = require_product_context_audit_store(record_store)
            profile = audit_store.read_product_profile_record(product)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if not common.authorization_allows(
            identity=identity,
            action="product_profile.read",
            product=profile.product,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
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
            raise common.http_error(
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
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_context_cutover_audit_request",
                message="Context cutover audit request is invalid.",
            ) from error
        return ProductContextCutoverAuditResponse(trace_id=trace_id, audit=audit_payload)

    app.add_api_route(
        "/v1/product-profiles/{product}/context-cutover-audit",
        read_product_context_cutover_audit,
        methods=["GET"],
        response_model=ProductContextCutoverAuditResponse,
        operation_id="read_product_context_cutover_audit",
        summary="Read a product context cutover audit",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_product_config_status_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_product_environment_config_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductEnvironmentConfigStatusResponse:
        trace_id = common.next_trace_id()
        product_read_result = _build_product_environment_read_result(
            dependencies=common,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
            params={
                "product": product,
                "environment": environment,
                "config_status": "true",
            },
        )
        _require_product_environment_result_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            result=product_read_result,
        )
        config_status = ProductEnvironmentConfigStatus.model_validate(
            product_read_result.payload["config_status"]
        )
        return ProductEnvironmentConfigStatusResponse(
            trace_id=trace_id,
            config_status=config_status,
        )

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        operation_id="read_product_environment_config_status",
        response_model=ProductEnvironmentConfigStatusResponse,
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_product_promotion_status_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_product_promotion_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductPromotionStatusResponse:
        trace_id = common.next_trace_id()

        try:
            profile, lane = resolve_product_promotion_target(
                record_store=record_store,
                product=product,
                destination_environment=environment,
            )
        except (AttributeError, FileNotFoundError) as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion status was not found.",
            ) from error
        if not common.authorization_allows(
            identity=identity,
            action="product_environment.read",
            product=profile.product,
            context=lane.context,
        ):
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion status was not found.",
            )

        def action_allowed(action: str, requested_product: str, context: str) -> bool:
            return common.authorization_allows(
                identity=identity,
                action=action,
                product=requested_product,
                context=context,
            )

        try:
            _, _, promotion_status = build_product_promotion_status(
                record_store=record_store,
                product=profile.product,
                destination_environment=lane.instance,
                action_allowed=action_allowed,
                workflow_credentials_ready=dependencies.workflow_credentials_ready,
            )
        except (AttributeError, FileNotFoundError) as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion status was not found.",
            ) from error
        return ProductPromotionStatusResponse(
            trace_id=trace_id,
            promotion_status=promotion_status,
        )

    app.add_api_route(
        PRODUCT_PROMOTION_STATUS_ROUTE,
        read_product_promotion_status,
        methods=["GET"],
        operation_id="read_product_promotion_status",
        response_model=ProductPromotionStatusResponse,
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
        },
    )

    def read_product_promotion_workflow_delivery(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        delivery_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductPromotionWorkflowDeliveryStatusResponse:
        trace_id = common.next_trace_id()
        try:
            profile, lane = resolve_product_promotion_target(
                record_store=record_store,
                product=product,
                destination_environment=environment,
            )
        except (AttributeError, FileNotFoundError) as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion workflow delivery was not found.",
            ) from error
        if not common.authorization_allows(
            identity=identity,
            action="product_environment.read",
            product=profile.product,
            context=lane.context,
        ):
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion workflow delivery was not found.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message="Product promotion workflow status requires PostgreSQL storage.",
            )
        try:
            delivery = record_store.read_outbox_delivery_record(delivery_id.strip())
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion workflow delivery was not found.",
            ) from error
        if (
            delivery.kind != "github_workflow_dispatch"
            or delivery.aggregate_type != "generic_web_promotion_workflow"
            or delivery.aggregate_id != f"{profile.product}:{lane.context}"
        ):
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product promotion workflow delivery was not found.",
            )
        return ProductPromotionWorkflowDeliveryStatusResponse(
            trace_id=trace_id,
            delivery=product_promotion_delivery_status(delivery),
        )

    app.add_api_route(
        PRODUCT_PROMOTION_WORKFLOW_STATUS_ROUTE,
        read_product_promotion_workflow_delivery,
        methods=["GET"],
        operation_id="read_product_promotion_workflow_delivery",
        response_model=ProductPromotionWorkflowDeliveryStatusResponse,
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )
