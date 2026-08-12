from typing import Annotated, Literal, cast

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.merge_train_policy import (
    MERGE_TRAIN_POLICY_TARGETS_READ_ACTION,
    MergeTrainSchedulerPolicy,
    MergeTrainServiceAuthz,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.merge_train_admission import (
    MergeTrainControllerStatusReadModel,
    MergeTrainRunHistoryStore,
    build_merge_train_controller_status_read_model,
    evaluate_merge_train_admission_from_store,
)
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.workflows.ship import utc_now_timestamp


class MergeTrainAdmissionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str = "main"

    @model_validator(mode="after")
    def _validate_query(self) -> "MergeTrainAdmissionQuery":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        if not self.repository:
            raise ValueError("merge train admission requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train admission requires base_branch")
        return self


class MergeTrainAdmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    admission: dict[str, object]


class MergeTrainControllerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    controller_status: MergeTrainControllerStatusReadModel


class MergeTrainPolicySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    updated_at: str
    policy_sha256: str


class MergeTrainPolicyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    policy_key: str
    scheduler: MergeTrainSchedulerPolicy
    service_authz: MergeTrainServiceAuthz


class MergeTrainPolicyTargetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: MergeTrainPolicySummary
    targets: tuple[MergeTrainPolicyTarget, ...]


def merge_train_policy_not_configured_error(
    *, dependencies: ReadRouteDependencies, trace_id: str, error: MergeTrainPolicyStoreMissingError
) -> Exception:
    return dependencies.http_error(
        status_code=503,
        trace_id=trace_id,
        code="merge_train_policy_not_configured",
        message=str(error) or "No active DB-backed merge train policy record is configured.",
    )


def merge_train_invalid_request_error(
    *, dependencies: ReadRouteDependencies, trace_id: str, error: ValueError
) -> Exception:
    return dependencies.http_error(
        status_code=400,
        trace_id=trace_id,
        code="invalid_request",
        message=str(error),
    )


def merge_train_admission_query(
    *,
    dependencies: ReadRouteDependencies,
    repository: str,
    base_branch: str,
    trace_id: str,
) -> MergeTrainAdmissionQuery:
    try:
        return MergeTrainAdmissionQuery.model_validate(
            {"repository": repository, "base_branch": base_branch}
        )
    except ValueError as error:
        raise merge_train_invalid_request_error(
            dependencies=dependencies,
            trace_id=trace_id,
            error=error,
        ) from error


def register_merge_train_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_merge_train_admission(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        base_branch: Annotated[str, Query()] = "main",
    ) -> MergeTrainAdmissionResponse:
        trace_id = dependencies.next_trace_id()
        admission_request = merge_train_admission_query(
            dependencies=dependencies,
            repository=repository,
            base_branch=base_branch,
            trace_id=trace_id,
        )
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=admission_request.repository,
                base_branch=admission_request.base_branch,
            )
        except ValueError as error:
            raise merge_train_invalid_request_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action=repository_policy.service_authz.action,
            product=repository_policy.service_authz.product,
            context=repository_policy.service_authz.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested merge train admission decision.",
            )
        merge_train_store = cast(MergeTrainRunHistoryStore, record_store)
        admission_decision = evaluate_merge_train_admission_from_store(
            store=merge_train_store,
            repository=admission_request.repository,
            base_branch=admission_request.base_branch,
            requested_at=utc_now_timestamp(),
            current_policy_key=repository_policy.policy_key,
            current_policy_sha256=policy_record.policy_sha256,
        )
        return MergeTrainAdmissionResponse(
            trace_id=trace_id,
            admission=admission_decision.model_dump(mode="json"),
        )

    def read_merge_train_controller_status(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        base_branch: Annotated[str, Query()] = "main",
    ) -> MergeTrainControllerStatusResponse:
        trace_id = dependencies.next_trace_id()
        status_request = merge_train_admission_query(
            dependencies=dependencies,
            repository=repository,
            base_branch=base_branch,
            trace_id=trace_id,
        )
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        try:
            repository_policy = policy_record.policy.find_repository_policy(
                repository=status_request.repository,
                base_branch=status_request.base_branch,
            )
        except ValueError as error:
            raise merge_train_invalid_request_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action=repository_policy.service_authz.action,
            product=repository_policy.service_authz.product,
            context=repository_policy.service_authz.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read the requested merge train controller status.",
            )
        merge_train_store = cast(MergeTrainRunHistoryStore, record_store)
        read_model = build_merge_train_controller_status_read_model(
            store=merge_train_store,
            repository=status_request.repository,
            base_branch=status_request.base_branch,
            generated_at=utc_now_timestamp(),
            current_policy_key=repository_policy.policy_key,
            current_policy_sha256=policy_record.policy_sha256,
        )
        return MergeTrainControllerStatusResponse(
            trace_id=trace_id,
            controller_status=read_model,
        )

    def read_merge_train_policy_targets(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> MergeTrainPolicyTargetsResponse:
        trace_id = dependencies.next_trace_id()
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
        except MergeTrainPolicyStoreMissingError as error:
            raise merge_train_policy_not_configured_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        targets: list[MergeTrainPolicyTarget] = []
        local_operator_can_read_targets = dependencies.authorization_allows(
            identity=identity,
            action=MERGE_TRAIN_POLICY_TARGETS_READ_ACTION,
            product="launchplane",
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        )
        for repository_policy in policy_record.policy.policies:
            service_authz_allowed = dependencies.authorization_allows(
                identity=identity,
                action=repository_policy.service_authz.action,
                product=repository_policy.service_authz.product,
                context=repository_policy.service_authz.context,
            )
            if not service_authz_allowed and not local_operator_can_read_targets:
                continue
            targets.append(
                MergeTrainPolicyTarget(
                    repository=repository_policy.repository,
                    base_branch=repository_policy.base_branch,
                    policy_key=repository_policy.policy_key,
                    scheduler=repository_policy.scheduler,
                    service_authz=repository_policy.service_authz,
                )
            )
        targets.sort(key=lambda target: (target.repository, target.base_branch))
        return MergeTrainPolicyTargetsResponse(
            trace_id=trace_id,
            policy=MergeTrainPolicySummary(
                record_id=policy_record.record_id,
                updated_at=policy_record.updated_at,
                policy_sha256=policy_record.policy_sha256,
            ),
            targets=tuple(targets),
        )

    responses = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    app.add_api_route(
        "/v1/work-graph/merge-train/admission",
        read_merge_train_admission,
        methods=["GET"],
        response_model=MergeTrainAdmissionResponse,
        operation_id="read_merge_train_admission",
        summary="Read merge train admission",
        responses=responses,
    )
    app.add_api_route(
        "/v1/work-graph/merge-train/controller/status",
        read_merge_train_controller_status,
        methods=["GET"],
        response_model=MergeTrainControllerStatusResponse,
        operation_id="read_merge_train_controller_status",
        summary="Read merge train controller status",
        responses=responses,
    )
    app.add_api_route(
        "/v1/work-graph/merge-train/policy-targets",
        read_merge_train_policy_targets,
        methods=["GET"],
        response_model=MergeTrainPolicyTargetsResponse,
        operation_id="read_merge_train_policy_targets",
        summary="Read merge train policy targets",
        responses=responses,
    )
