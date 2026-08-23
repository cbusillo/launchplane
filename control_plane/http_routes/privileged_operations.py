from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    PrivilegedOperationAgentSummary,
    PrivilegedOperationConflictError,
    PrivilegedOperationEventRecord,
    PrivilegedOperationRecord,
    PrivilegedOperationStatus,
    normalize_privileged_operation_source_event_id,
    privileged_operation_agent_summary,
)
from control_plane.durable_operation_authorization import (
    ManagedRuleAuthorizationError,
    require_single_managed_rule_identity,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.privileged_operation_registry import (
    PrivilegedOperationPlannerError,
    PrivilegedOperationPlanningStoreError,
)
from control_plane.privileged_operation_service import (
    DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
    PrivilegedOperationNotCancellableError,
    PrivilegedOperationStoreUnavailableError,
    cancel_privileged_operation,
    create_privileged_operation_plan,
    list_privileged_operations,
    read_privileged_operation,
    require_privileged_operation_store,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
)


PRIVILEGED_OPERATION_PLANS_ROUTE = "/v1/privileged-operations/plans"
PRIVILEGED_OPERATION_PLAN_ROUTE = "/v1/privileged-operations/plans/{operation_id}"
PRIVILEGED_OPERATION_CANCEL_ROUTE = "/v1/privileged-operations/plans/{operation_id}/cancel"
PRIVILEGED_OPERATION_AGENT_SUMMARY_ROUTE = "/v1/agent/privileged-operations/plans/{operation_id}"


@dataclass(frozen=True, slots=True)
class PrivilegedOperationRouteDependencies:
    common: ReadRouteDependencies
    read_github_human_identity: Callable[..., GitHubHumanIdentity]
    read_github_human_mutation_identity: Callable[..., GitHubHumanIdentity]
    policy_reader: Callable[[], LaunchplaneAuthzPolicy]


class PrivilegedOperationPlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    descriptor_id: Literal["managed-secret-reencryption"] = "managed-secret-reencryption"
    source_event_id: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(
        default=DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
        ge=MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
        le=MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    )
    request: ManagedSecretReencryptionPlanInput

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivilegedOperationPlanEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation plan envelope schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        return self


class PrivilegedOperationCancelEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    source_event_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivilegedOperationCancelEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation cancel envelope schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError(
                "Privileged-operation cancellation requires source_event_id and reason"
            )
        return self


class PrivilegedOperationHumanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    write_status: Literal["written", "replayed", "not_applicable"] = "not_applicable"
    record: PrivilegedOperationRecord
    events: tuple[PrivilegedOperationEventRecord, ...]


class PrivilegedOperationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    total: int = Field(ge=0)
    records: tuple[PrivilegedOperationRecord, ...]


class PrivilegedOperationAgentSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    summary: PrivilegedOperationAgentSummary


def register_privileged_operation_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: PrivilegedOperationRouteDependencies,
) -> None:
    def require_managed_rule(
        *,
        identity: LaunchplaneIdentity,
        action: str,
        trace_id: str,
    ) -> None:
        try:
            require_single_managed_rule_identity(
                policy=dependencies.policy_reader(),
                identity=identity,
                action=action,
                product="launchplane",
                context="launchplane",
                target=AuthorizationTarget(scope="global"),
            )
        except ManagedRuleAuthorizationError as error:
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot access privileged-operation planning.",
            ) from error

    def operation_events(
        record_store: object, operation_id: str
    ) -> tuple[PrivilegedOperationEventRecord, ...]:
        store = require_privileged_operation_store(record_store)
        return store.list_privileged_operation_event_records(
            operation_id=operation_id,
            limit=10,
        )

    def plan_privileged_operation(
        envelope: PrivilegedOperationPlanEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> PrivilegedOperationHumanResponse:
        trace_id = dependencies.common.next_trace_id()
        require_managed_rule(
            identity=identity,
            action=PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
            trace_id=trace_id,
        )
        try:
            result = create_privileged_operation_plan(
                record_store=record_store,
                requester_github_id=identity.github_id,
                requester_login=identity.login,
                source_event_id=envelope.source_event_id,
                request=envelope.request,
                expires_in_seconds=envelope.expires_in_seconds,
            )
        except (
            PrivilegedOperationPlannerError,
            PrivilegedOperationPlanningStoreError,
            PrivilegedOperationStoreUnavailableError,
        ) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Privileged-operation planning is unavailable.",
            ) from error
        except PrivilegedOperationConflictError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_plan_conflict",
                message="Privileged-operation plan request conflicts with existing state.",
            ) from error
        try:
            events = operation_events(record_store, result.record.operation_id)
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationHumanResponse(
            trace_id=trace_id,
            write_status=result.write_status,
            record=result.record,
            events=events,
        )

    def list_human_privileged_operations(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        status: Annotated[PrivilegedOperationStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PrivilegedOperationListResponse:
        trace_id = dependencies.common.next_trace_id()
        require_managed_rule(
            identity=identity,
            action=PRIVILEGED_SECRET_OPERATION_READ_ACTION,
            trace_id=trace_id,
        )
        try:
            records = list_privileged_operations(
                record_store=record_store,
                status=status or "",
                limit=limit,
            )
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationListResponse(
            trace_id=trace_id,
            total=len(records),
            records=records,
        )

    def read_human_privileged_operation(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationHumanResponse:
        trace_id = dependencies.common.next_trace_id()
        require_managed_rule(
            identity=identity,
            action=PRIVILEGED_SECRET_OPERATION_READ_ACTION,
            trace_id=trace_id,
        )
        try:
            record = read_privileged_operation(
                record_store=record_store,
                operation_id=operation_id,
            )
        except FileNotFoundError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_not_found",
                message="Privileged-operation plan was not found.",
            ) from error
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        try:
            events = operation_events(record_store, record.operation_id)
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationHumanResponse(
            trace_id=trace_id,
            record=record,
            events=events,
        )

    def cancel_human_privileged_operation(
        envelope: PrivilegedOperationCancelEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationHumanResponse:
        trace_id = dependencies.common.next_trace_id()
        require_managed_rule(
            identity=identity,
            action=PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
            trace_id=trace_id,
        )
        try:
            result = cancel_privileged_operation(
                record_store=record_store,
                operation_id=operation_id,
                actor_github_id=identity.github_id,
                actor_login=identity.login,
                source_event_id=envelope.source_event_id,
                reason=envelope.reason,
            )
        except FileNotFoundError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_not_found",
                message="Privileged-operation plan was not found.",
            ) from error
        except PrivilegedOperationNotCancellableError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_not_cancellable",
                message="Privileged-operation plan is no longer cancellable.",
            ) from error
        except PrivilegedOperationConflictError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_transition_conflict",
                message="Privileged-operation state changed concurrently.",
            ) from error
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        try:
            events = operation_events(record_store, result.record.operation_id)
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationHumanResponse(
            trace_id=trace_id,
            write_status=result.write_status,
            record=result.record,
            events=events,
        )

    def read_agent_privileged_operation_summary(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.common.read_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationAgentSummaryResponse:
        trace_id = dependencies.common.next_trace_id()
        require_managed_rule(
            identity=identity,
            action=PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
            trace_id=trace_id,
        )
        try:
            record = read_privileged_operation(
                record_store=record_store,
                operation_id=operation_id,
            )
        except FileNotFoundError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_not_found",
                message="Privileged-operation plan was not found.",
            ) from error
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationAgentSummaryResponse(
            trace_id=trace_id,
            summary=privileged_operation_agent_summary(record),
        )

    app.add_api_route(
        PRIVILEGED_OPERATION_PLANS_ROUTE,
        plan_privileged_operation,
        methods=["POST"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Plan a typed privileged operation",
        operation_id="plan_privileged_operation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_PLANS_ROUTE,
        list_human_privileged_operations,
        methods=["GET"],
        response_model=PrivilegedOperationListResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="List privileged-operation plans",
        operation_id="list_human_privileged_operations",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_PLAN_ROUTE,
        read_human_privileged_operation,
        methods=["GET"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Read a privileged-operation plan",
        operation_id="read_human_privileged_operation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_CANCEL_ROUTE,
        cancel_human_privileged_operation,
        methods=["POST"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Cancel a privileged-operation plan",
        operation_id="cancel_human_privileged_operation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_AGENT_SUMMARY_ROUTE,
        read_agent_privileged_operation_summary,
        methods=["GET"],
        response_model=PrivilegedOperationAgentSummaryResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Read a counts-only privileged-operation summary",
        operation_id="read_agent_privileged_operation_summary",
        tags=["agent", "privileged-operations"],
    )
