from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, cast

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.change_impact_service import (
    ChangeImpactRepositoryEvidenceProvider,
    require_change_impact_policy_read_store,
)
from control_plane.contracts.change_impact import ChangeImpactTarget, ChangeImpactTargetReference
from control_plane.contracts.advisory_check_projection import AdvisoryCheckProjectionResult
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_PROJECT_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
    OwnerAcceptanceDecision,
    OwnerAcceptanceEventRecord,
)
from control_plane.github_app_identity import (
    GitHubAppInstallationToken,
    revoke_installation_token,
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
from control_plane.owner_acceptance_current_items import (
    OwnerAcceptanceCurrentItem,
    OwnerAcceptanceCurrentItemsProvider,
    OwnerAcceptanceCurrentItemsRepositoryFailure,
    build_owner_acceptance_current_items,
)
from control_plane.owner_acceptance_projection import project_owner_acceptance_decision
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.workflows.launchplane import github_api_request


OWNER_ACCEPTANCE_EVALUATION_ROUTE = "/v1/owner-acceptance/evaluation"
OWNER_ACCEPTANCE_EVENTS_ROUTE = "/v1/owner-acceptance/events"
OWNER_ACCEPTANCE_EVENT_ROUTE = "/v1/owner-acceptance/events/{event_id}"
OWNER_ACCEPTANCE_QUEUE_ROUTE = "/v1/owner-acceptance/queue"
OWNER_ACCEPTANCE_CURRENT_ITEMS_ROUTE = "/v1/owner-acceptance/current-items"
OWNER_ACCEPTANCE_PROJECT_ROUTE = "/v1/owner-acceptance/project"


@dataclass(frozen=True, slots=True)
class OwnerAcceptanceRouteDependencies:
    common: ReadRouteDependencies
    read_browser_mutation_identity: Callable[..., LaunchplaneIdentity]
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    read_write_identity: Callable[..., LaunchplaneIdentity] | None = None
    github_app_token: Callable[[str, str], GitHubAppInstallationToken] | None = None
    github_api: Callable[..., object] = github_api_request


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


class OwnerAcceptanceViewerCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_write_authorized: bool = Field(
        description=(
            "Whether the evaluated identity currently has route-level "
            "owner_acceptance_event.write authorization. Browser session and current product "
            "Owner authority are revalidated separately when an event is submitted."
        )
    )


class OwnerAcceptanceEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    decision: OwnerAcceptanceDecision
    viewer_capabilities: OwnerAcceptanceViewerCapabilities


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
    derivation: Literal["ledger_only"] = "ledger_only"
    generated_at: str
    total: int
    candidate: int
    truncated: bool
    has_more: bool
    entry_count: int
    entries: tuple[OwnerAcceptanceQueueEntry, ...]


class OwnerAcceptanceCurrentItemsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    derivation: Literal["active_change_impact_open_pull_requests"] = (
        "active_change_impact_open_pull_requests"
    )
    generated_at: str
    viewer_capabilities: OwnerAcceptanceViewerCapabilities
    repository_count: int
    repository_failure_count: int
    candidate_count: int
    evaluated_count: int
    unavailable_count: int
    truncated: bool
    items: tuple[OwnerAcceptanceCurrentItem, ...]
    repository_failures: tuple[OwnerAcceptanceCurrentItemsRepositoryFailure, ...]


class OwnerAcceptanceProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference

    @model_validator(mode="after")
    def _validate_request(self) -> "OwnerAcceptanceProjectionRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance projection schema version.")
        return self


class OwnerAcceptanceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    decision: OwnerAcceptanceDecision
    result: AdvisoryCheckProjectionResult


def _validate_projection_target(
    decision: OwnerAcceptanceDecision,
    target: ChangeImpactTarget,
) -> None:
    bindings = tuple(
        product.binding for product in decision.products if product.binding is not None
    )
    if decision.binding is not None:
        bindings = (decision.binding, *bindings)
    for binding in bindings:
        if (
            binding.repository.casefold() != target.repository.casefold()
            or binding.repository_id != target.repository_id
            or binding.repository_owner_id != target.repository_owner_id
            or binding.pull_request_number != target.pull_request_number
            or binding.head_sha != target.head_sha
            or binding.tree_sha != target.tree_sha
        ):
            raise ValueError("Owner acceptance projection target changed during evaluation.")


def register_owner_acceptance_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: OwnerAcceptanceRouteDependencies,
) -> None:
    common = dependencies.common
    projection_identity = dependencies.read_write_identity or common.read_identity

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
        event_write_authorized = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        return OwnerAcceptanceEvaluationResponse(
            trace_id=trace_id,
            decision=decision,
            viewer_capabilities=OwnerAcceptanceViewerCapabilities(
                event_write_authorized=event_write_authorized
            ),
        )

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
            result = build_owner_acceptance_queue(
                store=record_store,
                repository=repository,
                status=status,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        return OwnerAcceptanceQueueResponse(
            trace_id=trace_id,
            generated_at=generated_at,
            total=result.total,
            candidate=result.candidate,
            truncated=result.truncated,
            has_more=result.has_more,
            entry_count=len(result.entries),
            entries=result.entries,
        )

    def read_current_items(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> OwnerAcceptanceCurrentItemsResponse:
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
                message="Caller cannot read current Owner acceptance items.",
            )
        provider = dependencies.repository_evidence_provider
        if not callable(getattr(provider, "list_open_pull_requests", None)) or not callable(
            getattr(provider, "resolve_current_item", None)
        ):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_current_items_unavailable",
                message="Current Owner acceptance item discovery is unavailable.",
            )
        try:
            result = build_owner_acceptance_current_items(
                store=require_change_impact_policy_read_store(record_store),
                repository_evidence_provider=cast(OwnerAcceptanceCurrentItemsProvider, provider),
                limit=limit,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        event_write_authorized = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        return OwnerAcceptanceCurrentItemsResponse(
            trace_id=trace_id,
            generated_at=generated_at,
            viewer_capabilities=OwnerAcceptanceViewerCapabilities(
                event_write_authorized=event_write_authorized
            ),
            repository_count=result.repository_count,
            repository_failure_count=result.repository_failure_count,
            candidate_count=result.candidate_count,
            evaluated_count=result.evaluated_count,
            unavailable_count=result.unavailable_count,
            truncated=result.truncated,
            items=result.items,
            repository_failures=result.repository_failures,
        )

    async def project(
        request: OwnerAcceptanceProjectionRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(projection_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceProjectionResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_PROJECT_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot project Owner acceptance decisions.",
            )
        try:
            if dependencies.github_app_token is None:
                raise ValueError("Owner acceptance projection identity is unavailable.")
            initial_evidence = dependencies.repository_evidence_provider.resolve(request.target)
            decision = evaluate_owner_acceptance(
                store=record_store,
                target=request.target,
                repository_evidence_provider=dependencies.repository_evidence_provider,
            )
            evidence = dependencies.repository_evidence_provider.resolve(request.target)
            if initial_evidence.target != evidence.target:
                raise ValueError("Owner acceptance projection target changed during evaluation.")
            _validate_projection_target(decision, evidence.target)
            installation_token = dependencies.github_app_token(
                evidence.target.repository,
                evidence.target.repository_id,
            )
            try:
                result = project_owner_acceptance_decision(
                    decision=decision,
                    target=evidence.target,
                    installation_token=installation_token,
                    api_request=dependencies.github_api,
                )
            finally:
                revoke_installation_token(
                    installation_token=installation_token,
                    api_request=dependencies.github_api,
                )
        except (OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_projection_unavailable",
                message=str(error),
            ) from error
        return OwnerAcceptanceProjectionResponse(
            trace_id=trace_id,
            decision=decision,
            result=result,
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
        OWNER_ACCEPTANCE_CURRENT_ITEMS_ROUTE,
        read_current_items,
        methods=["GET"],
        response_model=OwnerAcceptanceCurrentItemsResponse,
        tags=["owner-acceptance"],
        operation_id="list_owner_acceptance_current_items",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_PROJECT_ROUTE,
        project,
        methods=["POST"],
        response_model=OwnerAcceptanceProjectionResponse,
        operation_id="project_owner_acceptance_decision",
        tags=["owner-acceptance"],
        responses={
            status: {"model": common.error_response_model} for status in (400, 401, 403, 503)
        },
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
