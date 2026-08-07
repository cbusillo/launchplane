from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
    OwnerAcceptanceDecision,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceAuthorizationError,
    OwnerAcceptanceBindingConflictError,
    OwnerAcceptanceEvaluationUnavailableError,
    OwnerAcceptanceEventConflictError,
    OwnerAcceptanceWriteResult,
    evaluate_owner_acceptance,
    record_owner_acceptance_event,
    require_owner_acceptance_event_store,
)
from control_plane.owner_acceptance_queue import (
    OwnerAcceptanceQueueEntry,
    build_owner_acceptance_queue,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity


OWNER_ACCEPTANCE_EVALUATION_ROUTE = "/v1/owner-acceptance/evaluation"
OWNER_ACCEPTANCE_EVENTS_ROUTE = "/v1/owner-acceptance/events"
OWNER_ACCEPTANCE_EVENT_ROUTE = "/v1/owner-acceptance/events/{event_id}"
OWNER_ACCEPTANCE_QUEUE_ROUTE = "/v1/owner-acceptance/queue"


@dataclass(frozen=True, slots=True)
class OwnerAcceptanceRouteDependencies:
    common: ReadRouteDependencies
    read_browser_mutation_identity: Callable[..., LaunchplaneIdentity]
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider


class OwnerAcceptanceEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference
    action: Literal["accepted", "changes_requested", "revoked"]
    expected_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _validate_reason(self) -> "OwnerAcceptanceEventEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance event envelope schema version.")
        self.reason = self.reason.strip()
        if self.action != "accepted" and not self.reason:
            raise ValueError(f"Owner acceptance action {self.action!r} requires a reason")
        return self


class OwnerAcceptanceEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    decision: OwnerAcceptanceDecision


class OwnerAcceptanceEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    write_status: Literal["written", "replayed"]
    record: OwnerAcceptanceEventRecord
    decision: OwnerAcceptanceDecision


class OwnerAcceptanceEventReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: OwnerAcceptanceEventRecord


class OwnerAcceptanceQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    generated_at: str
    entry_count: int
    entries: tuple[OwnerAcceptanceQueueEntry, ...]


def register_owner_acceptance_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: OwnerAcceptanceRouteDependencies,
) -> None:
    common = dependencies.common

    def evaluate(
        repository: Annotated[
            str,
            Query(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$"),
        ],
        pull_request_number: Annotated[int, Query(ge=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEvaluationResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance evaluation.",
            )
        try:
            decision = evaluate_owner_acceptance(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                target=ChangeImpactTargetReference(
                    repository=repository,
                    pull_request_number=pull_request_number,
                ),
            )
        except (OwnerAcceptanceEvaluationUnavailableError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_evidence_unavailable",
                message="Owner acceptance evidence is unavailable.",
            ) from None
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return OwnerAcceptanceEvaluationResponse(trace_id=trace_id, decision=decision)

    def write_event(
        envelope: OwnerAcceptanceEventEnvelope,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            ),
        ],
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(dependencies.read_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEventResponse:
        trace_id = common.next_trace_id()
        if not isinstance(identity, GitHubHumanIdentity):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="github_human_required",
                message="Owner acceptance events require a browser-authenticated GitHub human.",
            )
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot write Owner acceptance events.",
            )
        try:
            result: OwnerAcceptanceWriteResult = record_owner_acceptance_event(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                target=envelope.target,
                identity=identity,
                action=envelope.action,
                expected_binding_sha256=envelope.expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id=idempotency_key,
                reason=envelope.reason,
            )
        except OwnerAcceptanceAuthorizationError as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="owner_acceptance_authorization_denied",
                message=str(error),
            ) from error
        except OwnerAcceptanceEventConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_acceptance_event_conflict",
                message=str(error),
            ) from error
        except OwnerAcceptanceBindingConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_acceptance_binding_changed",
                message=str(error),
            ) from error
        except (OwnerAcceptanceEvaluationUnavailableError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_evidence_unavailable",
                message="Owner acceptance evidence is unavailable.",
            ) from None
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return OwnerAcceptanceEventResponse(
            trace_id=trace_id,
            write_status=result.status,
            record=result.record,
            decision=result.decision,
        )

    def read_event(
        event_id: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEventReadResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance events.",
            )
        try:
            record = require_owner_acceptance_event_store(
                record_store
            ).read_owner_acceptance_event_record(event_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Owner acceptance event was not found.",
            ) from error
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return OwnerAcceptanceEventReadResponse(trace_id=trace_id, record=record)

    def read_queue(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        repository: Annotated[str, Query(max_length=256)] = "",
        status: Annotated[str, Query(max_length=64)] = "",
    ) -> OwnerAcceptanceQueueResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance queue.",
            )
        try:
            entries = build_owner_acceptance_queue(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        normalized_repository = repository.strip().lower()
        normalized_status = status.strip().lower()
        filtered = tuple(
            entry
            for entry in entries
            if (not normalized_repository or entry.repository == normalized_repository)
            and (not normalized_status or entry.owner_acceptance_decision.status == normalized_status)
        )
        from control_plane.owner_acceptance_queue import _now_utc
        return OwnerAcceptanceQueueResponse(
            trace_id=trace_id,
            generated_at=_now_utc(),
            entry_count=len(filtered),
            entries=filtered,
        )

    errors = {
        400: {"model": common.error_response_model},
        401: {"model": common.error_response_model},
        403: {"model": common.error_response_model},
        404: {"model": common.error_response_model},
        409: {"model": common.error_response_model},
        503: {"model": common.error_response_model},
    }
    app.add_api_route(
        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
        evaluate,
        methods=["GET"],
        response_model=OwnerAcceptanceEvaluationResponse,
        tags=["owner-acceptance"],
        operation_id="evaluate_owner_acceptance",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_EVENTS_ROUTE,
        write_event,
        methods=["POST"],
        response_model=OwnerAcceptanceEventResponse,
        status_code=202,
        tags=["owner-acceptance"],
        operation_id="write_owner_acceptance_event",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_EVENT_ROUTE,
        read_event,
        methods=["GET"],
        response_model=OwnerAcceptanceEventReadResponse,
        tags=["owner-acceptance"],
        operation_id="read_owner_acceptance_event",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_QUEUE_ROUTE,
        read_queue,
        methods=["GET"],
        response_model=OwnerAcceptanceQueueResponse,
        tags=["owner-acceptance"],
        operation_id="list_owner_acceptance_queue",
        responses=errors,
    )
