from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedMergeTrainPolicyPreparationContext,
    ManagedSecretReencryptionPlanInput,
    PrivilegedOperationApproval,
    PrivilegedOperationActor,
    PrivilegedOperationAgentActor,
    PrivilegedOperationConflictError,
    PrivilegedOperationDescriptorId,
    PrivilegedOperationEventRecord,
    PrivilegedOperationRecord,
    PrivilegedOperationRequest,
    PrivilegedOperationSemanticReview,
    PrivilegedOperationStatus,
    PrivilegedOperationSummary,
    OrdinaryAgentMergeTrainTargetIntent,
    build_privileged_operation_id_for_actor,
    normalize_privileged_operation_source_event_id,
    privileged_operation_agent_summary,
    privileged_operation_pre_state_digest,
    terminal_agent_principal_sha256,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import (
    MergeTrainGitHubTokenSource,
    MergeTrainPolicy,
    MergeTrainPolicyRecord,
    MergeTrainRepositoryPolicy,
    MergeTrainSchedulerPolicy,
    build_merge_train_policy_record_id,
    normalize_merge_train_policy_timestamp,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationDurationOption,
    OrdinaryAgentDeliveryActivationRevokeOption,
    OrdinaryAgentDeliveryActivationRevokeRequest,
    OrdinaryAgentDeliveryActivationSetupOption,
    OrdinaryAgentDeliveryActivationSetupRequest,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.ordinary_agent_delivery_authorization_inputs import (
    OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
)
from control_plane.authz_candidate_preparation import (
    AuthorizationCandidateId,
    AuthorizationCandidatePreparationError,
    authorization_candidate_request_matches,
    compile_authorization_candidate,
)
from control_plane.durable_operation_authorization import (
    ManagedRuleAuthorizationError,
    managed_github_id_action_allows,
    require_single_explicit_action_managed_github_id_rule_identity,
    require_single_explicit_action_managed_rule_identity,
    require_single_managed_github_id_rule_identity,
    require_single_managed_rule_identity,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationPlanningError,
    list_ordinary_agent_delivery_activation_options,
    ordinary_agent_delivery_activation_duration_options,
)
from control_plane.ordinary_agent_delivery_authorization_inputs import (
    _read_current_inventory,
    read_ordinary_agent_delivery_authorization_candidate_inputs,
)
from control_plane.privileged_operation_registry import (
    PrivilegedOperationPlannerError,
    PrivilegedOperationPlanningStoreError,
    read_privileged_operation_descriptor,
    _policy_key_payloads,
)
from control_plane.privileged_operation_service import (
    DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
    PrivilegedOperationNotCancellableError,
    PrivilegedOperationNotApprovableError,
    PrivilegedOperationNotRevocableError,
    approve_privileged_operation,
    PrivilegedOperationStoreUnavailableError,
    PrivilegedOperationSemanticReviewError,
    cancel_privileged_operation,
    create_typed_privileged_operation_plan,
    privileged_operation_semantic_review,
    read_privileged_operation,
    revoke_privileged_operation,
    require_privileged_operation_store,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
    authz_policy_allows_immutable_github_id_administration,
)


PRIVILEGED_OPERATION_PLANS_ROUTE = "/v1/privileged-operations/plans"
PRIVILEGED_OPERATION_PLAN_ROUTE = "/v1/privileged-operations/plans/{operation_id}"
PRIVILEGED_OPERATION_REVIEW_ROUTE = "/v1/privileged-operations/plans/{operation_id}/review"
PRIVILEGED_OPERATION_CANCEL_ROUTE = "/v1/privileged-operations/plans/{operation_id}/cancel"
PRIVILEGED_OPERATION_APPROVE_ROUTE = "/v1/privileged-operations/plans/{operation_id}/approve"
PRIVILEGED_OPERATION_REVOKE_ROUTE = "/v1/privileged-operations/plans/{operation_id}/revoke"
PRIVILEGED_OPERATION_AGENT_SUMMARY_ROUTE = "/v1/agent/privileged-operations/plans/{operation_id}"
PRIVILEGED_OPERATION_AGENT_PLANS_ROUTE = "/v1/agent/privileged-operations/plans"
ORDINARY_AGENT_DELIVERY_ACTIVATION_OPTIONS_ROUTE = (
    "/v1/privileged-operations/ordinary-agent-delivery-activation/options"
)
ORDINARY_AGENT_DELIVERY_ACTIVATION_PLANS_ROUTE = (
    "/v1/privileged-operations/ordinary-agent-delivery-activation/plans"
)
AUTHORIZATION_CANDIDATE_PREPARE_ROUTE = "/v1/privileged-operations/authorization-candidates/prepare"
ORDINARY_AGENT_DELIVERY_AUTHORIZATION_CANDIDATE_INPUTS_ROUTE = (
    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/inputs"
)


@dataclass(frozen=True, slots=True)
class PrivilegedOperationRouteDependencies:
    common: ReadRouteDependencies
    read_bearer_identity: Callable[..., LaunchplaneIdentity]
    read_github_human_identity: Callable[..., GitHubHumanIdentity]
    read_github_human_mutation_identity: Callable[..., GitHubHumanIdentity]
    policy_reader: Callable[[], LaunchplaneAuthzPolicy]
    policy_record_reader: Callable[[], object] | None = None


class PrivilegedOperationPlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    descriptor_id: PrivilegedOperationDescriptorId = "managed-secret-reencryption"
    source_event_id: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(
        default=DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
        ge=MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
        le=MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    )
    request: PrivilegedOperationRequest

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivilegedOperationPlanEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation plan envelope schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        if self.descriptor_id == "managed-secret-reencryption" and not isinstance(
            self.request, ManagedSecretReencryptionPlanInput
        ):
            raise ValueError("Managed-secret descriptor requires a managed-secret request.")
        if self.descriptor_id == "managed-authz-policy-set" and not isinstance(
            self.request, ManagedAuthzPolicySetProposalInput
        ):
            raise ValueError("Managed-policy descriptor requires a managed-policy request.")
        if self.descriptor_id == "managed-merge-train-policy-import" and not isinstance(
            self.request, ManagedMergeTrainPolicyImportProposalInput
        ):
            raise ValueError("Merge-train policy descriptor requires a merge-train policy request.")
        if (
            isinstance(self.request, ManagedMergeTrainPolicyImportProposalInput)
            and self.request.preparation_context is not None
        ):
            raise ValueError(
                "Merge-train target preparation context is accepted only by its dedicated endpoint."
            )
        if self.descriptor_id == "ordinary-agent-delivery-activation" and not isinstance(
            self.request,
            (
                OrdinaryAgentDeliveryActivationSetupRequest,
                OrdinaryAgentDeliveryActivationRevokeRequest,
            ),
        ):
            raise ValueError("Ordinary-agent activation descriptor requires an activation request.")
        return self


class OrdinaryAgentDeliveryActivationPlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_event_id: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(
        default=DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
        ge=MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
        le=MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    )
    request: Annotated[
        OrdinaryAgentDeliveryActivationSetupRequest | OrdinaryAgentDeliveryActivationRevokeRequest,
        Field(discriminator="action"),
    ]

    @model_validator(mode="after")
    def _validate_envelope(self) -> "OrdinaryAgentDeliveryActivationPlanEnvelope":
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        return self


class AuthorizationCandidatePrepareEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: AuthorizationCandidateId
    intent: Literal["add", "remove"]
    source_event_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "AuthorizationCandidatePrepareEnvelope":
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        return self


class AuthorizationCandidatePrepareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    state: Literal["planned", "already_satisfied"]
    operation_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


ORDINARY_AGENT_MERGE_TRAIN_TARGET_INPUTS_ROUTE = (
    "/v1/privileged-operations/merge-train-targets/inputs"
)
ORDINARY_AGENT_MERGE_TRAIN_TARGET_PREPARE_ROUTE = (
    "/v1/privileged-operations/merge-train-targets/prepare"
)


class OrdinaryAgentMergeTrainTargetPrepareEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    source_event_id: str = Field(min_length=1, max_length=128)
    intent: OrdinaryAgentMergeTrainTargetIntent

    @model_validator(mode="after")
    def _validate_envelope(self) -> "OrdinaryAgentMergeTrainTargetPrepareEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported ordinary-agent merge target preparation schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        return self


class OrdinaryAgentMergeTrainTargetPrepareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    state: Literal["planned", "already_satisfied"]
    operation_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class OrdinaryAgentMergeTrainTargetInventoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: str
    repository: str
    inventory_record_id: str
    inventory_digest: str


class OrdinaryAgentMergeTrainTargetPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    updated_at: str
    policy_sha256: str
    configured_policy_keys: tuple[str, ...]


class OrdinaryAgentMergeTrainTargetInputsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: OrdinaryAgentMergeTrainTargetPolicyInput
    tracked_repositories: tuple[OrdinaryAgentMergeTrainTargetInventoryInput, ...]


class PrivilegedPolicyOperationAgentProposalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    descriptor_id: Literal[
        "managed-authz-policy-set",
        "managed-merge-train-policy-import",
    ] = "managed-authz-policy-set"
    source_event_id: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(
        default=DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
        ge=MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
        le=MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    )
    request: ManagedAuthzPolicySetProposalInput | ManagedMergeTrainPolicyImportProposalInput

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivilegedPolicyOperationAgentProposalEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged policy proposal envelope schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        if self.descriptor_id == "managed-authz-policy-set" and not isinstance(
            self.request, ManagedAuthzPolicySetProposalInput
        ):
            raise ValueError("Managed-policy descriptor requires a managed-policy request.")
        if self.descriptor_id == "managed-merge-train-policy-import" and not isinstance(
            self.request, ManagedMergeTrainPolicyImportProposalInput
        ):
            raise ValueError("Merge-train policy descriptor requires a merge-train policy request.")
        if (
            isinstance(self.request, ManagedMergeTrainPolicyImportProposalInput)
            and self.request.preparation_context is not None
        ):
            raise ValueError(
                "Merge-train target preparation context is accepted only by its dedicated endpoint."
            )
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


class PrivilegedOperationApprovalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    source_event_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "PrivilegedOperationApprovalEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation approval envelope schema version.")
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Privileged-operation approval requires source_event_id and reason")
        return self


class PrivilegedOperationRevocationEnvelope(PrivilegedOperationApprovalEnvelope):
    pass


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
    reviews: tuple[PrivilegedOperationSemanticReview, ...]


class OrdinaryAgentDeliveryActivationOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    duration_options: tuple[OrdinaryAgentDeliveryActivationDurationOption, ...]
    setup_options: tuple[OrdinaryAgentDeliveryActivationSetupOption, ...]
    revoke_options: tuple[OrdinaryAgentDeliveryActivationRevokeOption, ...]


class PrivilegedOperationSemanticReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    review: PrivilegedOperationSemanticReview


class PrivilegedOperationAgentSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    summary: PrivilegedOperationSummary


class PrivilegedPolicyOperationAgentProposalResponse(PrivilegedOperationAgentSummaryResponse):
    write_status: Literal["written", "replayed"]


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
        descriptor_id: PrivilegedOperationDescriptorId,
    ) -> None:
        try:
            rule_reader = (
                require_single_explicit_action_managed_rule_identity
                if descriptor_id == "ordinary-agent-delivery-activation"
                else require_single_managed_rule_identity
            )
            rule_reader(
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

    def read_active_policy_record(*, trace_id: str) -> LaunchplaneAuthzPolicyRecord:
        if dependencies.policy_record_reader is None:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="The active authorization policy record is unavailable.",
            )
        try:
            return LaunchplaneAuthzPolicyRecord.model_validate(dependencies.policy_record_reader())
        except (LookupError, TypeError, ValueError) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="The active authorization policy record is unavailable.",
            ) from error

    def require_immutable_approval_rule(
        *,
        identity: GitHubHumanIdentity,
        action: str,
        trace_id: str,
        descriptor_id: PrivilegedOperationDescriptorId,
        policy_record: LaunchplaneAuthzPolicyRecord | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, str, str]:
        resolved_policy_record = policy_record or read_active_policy_record(trace_id=trace_id)
        try:
            rule_reader = (
                require_single_explicit_action_managed_github_id_rule_identity
                if descriptor_id == "ordinary-agent-delivery-activation"
                else require_single_managed_github_id_rule_identity
            )
            managed_identity = rule_reader(
                policy=resolved_policy_record.policy,
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
                message="Identity cannot authorize this privileged-operation transition.",
            ) from error
        return (
            resolved_policy_record,
            managed_identity.managed_set_id,
            managed_identity.managed_rule_id,
        )

    def operation_events(
        record_store: object, operation_id: str
    ) -> tuple[PrivilegedOperationEventRecord, ...]:
        store = require_privileged_operation_store(record_store)
        return store.list_privileged_operation_event_records(
            operation_id=operation_id,
            limit=10,
        )

    def semantic_review_or_error(
        *,
        record: PrivilegedOperationRecord,
        events: tuple[PrivilegedOperationEventRecord, ...],
        generated_at: datetime,
        trace_id: str,
    ) -> PrivilegedOperationSemanticReview:
        try:
            return privileged_operation_semantic_review(
                record=record,
                events=events,
                generated_at=generated_at,
            )
        except PrivilegedOperationSemanticReviewError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_semantic_review_unsupported",
                message="Privileged-operation semantic review is unavailable for this stored descriptor state.",
            ) from error

    def require_current_policy_plan(
        *,
        record: PrivilegedOperationRecord,
        record_store: object,
        trace_id: str,
    ) -> None:
        if record.safety_class != "policy_admin":
            return
        try:
            current_evidence = read_privileged_operation_descriptor(record.descriptor_id).planner(
                record_store,
                record.request,
            )
        except PrivilegedOperationPlannerError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_plan_stale",
                message="The privileged-operation policy plan is stale and must be replanned.",
            ) from error
        except (
            PrivilegedOperationPlanningStoreError,
            TypeError,
            ValueError,
        ) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Privileged-operation policy planning is unavailable.",
            ) from error
        if current_evidence != record.evidence:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_plan_stale",
                message="The privileged-operation policy plan is stale and must be replanned.",
            )

    def read_operation_or_error(
        *,
        record_store: object,
        operation_id: str,
        trace_id: str,
    ) -> PrivilegedOperationRecord:
        try:
            return read_privileged_operation(
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

    def read_operation_projection_or_error(
        *,
        record_store: object,
        operation_id: str,
        trace_id: str,
    ) -> PrivilegedOperationRecord:
        try:
            return require_privileged_operation_store(
                record_store
            ).read_privileged_operation_record(operation_id)
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

    def descriptor_action(descriptor_id: PrivilegedOperationDescriptorId, field_name: str) -> str:
        descriptor = read_privileged_operation_descriptor(descriptor_id).descriptor
        action = getattr(descriptor, field_name)
        if not isinstance(action, str) or not action:
            raise ValueError("Privileged-operation descriptor does not expose this action.")
        return action

    def read_ordinary_agent_delivery_activation_options(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> OrdinaryAgentDeliveryActivationOptionsResponse:
        trace_id = dependencies.common.next_trace_id()
        descriptor_id: PrivilegedOperationDescriptorId = "ordinary-agent-delivery-activation"
        require_managed_rule(
            identity=identity,
            action=descriptor_action(descriptor_id, "human_read_action"),
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        try:
            observed_at = datetime.now(timezone.utc)
            setup_options, revoke_options = list_ordinary_agent_delivery_activation_options(
                record_store,
                observed_at=observed_at,
            )
        except OrdinaryAgentDeliveryActivationPlanningError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Ordinary-agent activation options are unavailable.",
            ) from error
        return OrdinaryAgentDeliveryActivationOptionsResponse(
            trace_id=trace_id,
            duration_options=ordinary_agent_delivery_activation_duration_options(
                observed_at=observed_at
            ),
            setup_options=setup_options,
            revoke_options=revoke_options,
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
            action=descriptor_action(envelope.descriptor_id, "plan_action"),
            trace_id=trace_id,
            descriptor_id=envelope.descriptor_id,
        )
        try:
            result = create_typed_privileged_operation_plan(
                record_store=record_store,
                descriptor_id=envelope.descriptor_id,
                actor=PrivilegedOperationActor(
                    identity_type="github_human",
                    github_id=identity.github_id,
                    login=identity.login,
                ),
                source_kind="browser_api",
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

    def plan_ordinary_agent_delivery_activation(
        envelope: OrdinaryAgentDeliveryActivationPlanEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> PrivilegedOperationHumanResponse:
        return plan_privileged_operation(
            PrivilegedOperationPlanEnvelope(
                descriptor_id="ordinary-agent-delivery-activation",
                source_event_id=envelope.source_event_id,
                expires_in_seconds=envelope.expires_in_seconds,
                request=envelope.request,
            ),
            identity,
            record_store,
        )

    def list_human_privileged_operations(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        status: Annotated[PrivilegedOperationStatus | None, Query()] = None,
        descriptor_id: Annotated[PrivilegedOperationDescriptorId, Query()] = (
            "managed-secret-reencryption"
        ),
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PrivilegedOperationListResponse:
        trace_id = dependencies.common.next_trace_id()
        generated_at = datetime.now(timezone.utc)
        require_managed_rule(
            identity=identity,
            action=descriptor_action(descriptor_id, "human_read_action"),
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        try:
            store = require_privileged_operation_store(record_store)
            records = store.list_privileged_operation_records(
                status=status or "",
                descriptor_id=descriptor_id,
                limit=limit,
            )
            reviews = tuple(
                semantic_review_or_error(
                    record=record,
                    events=store.list_privileged_operation_event_records(
                        operation_id=record.operation_id,
                        limit=10,
                    ),
                    generated_at=generated_at,
                    trace_id=trace_id,
                )
                for record in records
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
            total=len(reviews),
            reviews=reviews,
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
        record = read_operation_or_error(
            record_store=record_store,
            operation_id=operation_id,
            trace_id=trace_id,
        )
        require_managed_rule(
            identity=identity,
            action=descriptor_action(record.descriptor_id, "human_read_action"),
            trace_id=trace_id,
            descriptor_id=record.descriptor_id,
        )
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

    def read_human_privileged_operation_review(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationSemanticReviewResponse:
        trace_id = dependencies.common.next_trace_id()
        record = read_operation_projection_or_error(
            record_store=record_store,
            operation_id=operation_id,
            trace_id=trace_id,
        )
        require_managed_rule(
            identity=identity,
            action=descriptor_action(record.descriptor_id, "human_read_action"),
            trace_id=trace_id,
            descriptor_id=record.descriptor_id,
        )
        try:
            events = operation_events(record_store, record.operation_id)
        except PrivilegedOperationStoreUnavailableError as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_storage_unavailable",
                message="Privileged-operation planning storage is unavailable.",
            ) from error
        return PrivilegedOperationSemanticReviewResponse(
            trace_id=trace_id,
            review=semantic_review_or_error(
                record=record,
                events=events,
                generated_at=datetime.now(timezone.utc),
                trace_id=trace_id,
            ),
        )

    def approve_human_privileged_operation(
        envelope: PrivilegedOperationApprovalEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationHumanResponse:
        trace_id = dependencies.common.next_trace_id()
        policy_record = read_active_policy_record(trace_id=trace_id)
        try:
            record = read_privileged_operation(record_store=record_store, operation_id=operation_id)
            policy_record, managed_set_id, managed_rule_id = require_immutable_approval_rule(
                identity=identity,
                action=descriptor_action(record.descriptor_id, "approve_action"),
                trace_id=trace_id,
                descriptor_id=record.descriptor_id,
                policy_record=policy_record,
            )
            if record.descriptor_id == "managed-authz-policy-set" and (
                not policy_record.policy.allows(
                    identity=identity,
                    action="authz_policy_grant.write",
                    product="launchplane",
                    context="launchplane",
                    target=AuthorizationTarget(scope="global"),
                )
                or not managed_github_id_action_allows(
                    policy=policy_record.policy,
                    github_id=identity.github_id,
                    action="authz_policy_grant.write",
                    product="launchplane",
                    context="launchplane",
                    target=AuthorizationTarget(scope="global"),
                )
            ):
                raise dependencies.common.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message=(
                        "Policy-operation approval requires an existing immutable-ID DB policy "
                        "administrator."
                    ),
                )
            require_current_policy_plan(
                record=record,
                record_store=record_store,
                trace_id=trace_id,
            )
            approval = PrivilegedOperationApproval(
                approver=PrivilegedOperationActor(
                    identity_type="github_human",
                    github_id=identity.github_id,
                    login=identity.login,
                ),
                descriptor_id=record.descriptor_id,
                descriptor_version=record.descriptor_version,
                request_digest=record.request_digest,
                evidence_digest=record.evidence_digest,
                plan_digest=record.evidence.plan_digest,
                pre_state_digest=privileged_operation_pre_state_digest(record.evidence),
                policy_record_id=policy_record.record_id,
                policy_revision=policy_record.revision,
                policy_sha256=policy_record.policy_sha256,
                policy_source=policy_record.source,
                managed_set_id=managed_set_id,
                managed_rule_id=managed_rule_id,
                expires_at=record.expires_at,
                reason=envelope.reason,
                rollback_class=(
                    "activation_revoke"
                    if record.descriptor_id == "ordinary-agent-delivery-activation"
                    else "policy_cas"
                    if record.safety_class == "policy_admin"
                    else "key_retained"
                ),
            )
            result = approve_privileged_operation(
                record_store=record_store,
                operation_id=operation_id,
                approval=approval,
                source_event_id=envelope.source_event_id,
            )
            events = operation_events(record_store, operation_id)
        except FileNotFoundError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_not_found",
                message="Privileged-operation plan was not found.",
            ) from error
        except (PrivilegedOperationNotApprovableError, PrivilegedOperationConflictError) as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_approval_conflict",
                message="Privileged-operation approval conflicts with current state.",
            ) from error
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

    def revoke_human_privileged_operation(
        envelope: PrivilegedOperationRevocationEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationHumanResponse:
        trace_id = dependencies.common.next_trace_id()
        try:
            record = read_privileged_operation(record_store=record_store, operation_id=operation_id)
            require_immutable_approval_rule(
                identity=identity,
                action=descriptor_action(record.descriptor_id, "revoke_action"),
                trace_id=trace_id,
                descriptor_id=record.descriptor_id,
            )
            result = revoke_privileged_operation(
                record_store=record_store,
                operation_id=operation_id,
                actor_github_id=identity.github_id,
                actor_login=identity.login,
                source_event_id=envelope.source_event_id,
                reason=envelope.reason,
            )
            events = operation_events(record_store, operation_id)
        except FileNotFoundError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_not_found",
                message="Privileged-operation plan was not found.",
            ) from error
        except (PrivilegedOperationNotRevocableError, PrivilegedOperationConflictError) as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_revocation_conflict",
                message="Privileged-operation revocation conflicts with current state.",
            ) from error
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
        try:
            record = read_privileged_operation(record_store=record_store, operation_id=operation_id)
            require_managed_rule(
                identity=identity,
                action=descriptor_action(record.descriptor_id, "cancel_action"),
                trace_id=trace_id,
                descriptor_id=record.descriptor_id,
            )
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
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_bearer_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> PrivilegedOperationAgentSummaryResponse:
        trace_id = dependencies.common.next_trace_id()
        if not isinstance(identity, TerminalAgentIdentity):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Only terminal agents can read the agent operation projection.",
            )
        record = read_operation_or_error(
            record_store=record_store,
            operation_id=operation_id,
            trace_id=trace_id,
        )
        if (
            read_privileged_operation_descriptor(
                record.descriptor_id
            ).descriptor.agent_summary_read_action
            is None
        ):
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="privileged_operation_agent_summary_unavailable",
                message="This privileged-operation descriptor has no agent summary capability.",
            )
        require_managed_rule(
            identity=identity,
            action=descriptor_action(record.descriptor_id, "agent_summary_read_action"),
            trace_id=trace_id,
            descriptor_id=record.descriptor_id,
        )
        if record.descriptor_id in {
            "managed-authz-policy-set",
            "managed-merge-train-policy-import",
        }:
            principal_sha256 = terminal_agent_principal_sha256(
                subject=identity.subject,
                token_label=identity.token_label,
            )
            if (
                record.requested_by.identity_type != "terminal_agent"
                or record.requested_by.principal_sha256 != principal_sha256
            ):
                raise dependencies.common.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Terminal agents can read only their own policy operation proposals.",
                )
        return PrivilegedOperationAgentSummaryResponse(
            trace_id=trace_id,
            summary=privileged_operation_agent_summary(record),
        )

    def propose_agent_privileged_policy_operation(
        envelope: PrivilegedPolicyOperationAgentProposalEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_bearer_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> PrivilegedPolicyOperationAgentProposalResponse:
        trace_id = dependencies.common.next_trace_id()
        if not isinstance(identity, TerminalAgentIdentity):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Only terminal agents can propose agent policy operations.",
            )
        require_managed_rule(
            identity=identity,
            action=descriptor_action(envelope.descriptor_id, "plan_action"),
            trace_id=trace_id,
            descriptor_id=envelope.descriptor_id,
        )
        actor = PrivilegedOperationAgentActor(
            principal_sha256=terminal_agent_principal_sha256(
                subject=identity.subject,
                token_label=identity.token_label,
            ),
        )
        try:
            result = create_typed_privileged_operation_plan(
                record_store=record_store,
                descriptor_id=envelope.descriptor_id,
                actor=actor,
                source_kind="agent_api",
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
        return PrivilegedPolicyOperationAgentProposalResponse(
            trace_id=trace_id,
            write_status=result.write_status,
            summary=privileged_operation_agent_summary(result.record),
        )

    def prepare_authorization_candidate(
        envelope: AuthorizationCandidatePrepareEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> AuthorizationCandidatePrepareResponse:
        trace_id = dependencies.common.next_trace_id()
        descriptor_id: PrivilegedOperationDescriptorId = "managed-authz-policy-set"
        propose_action = descriptor_action(descriptor_id, "plan_action")
        require_managed_rule(
            identity=identity,
            action=propose_action,
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        policy_record, _managed_set_id, _managed_rule_id = require_immutable_approval_rule(
            identity=identity,
            action=propose_action,
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        if not authz_policy_allows_immutable_github_id_administration(
            policy=policy_record.policy,
            github_id=identity.github_id,
        ):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot prepare this authorization candidate.",
            )
        actor = PrivilegedOperationActor(
            identity_type="github_human",
            github_id=identity.github_id,
            login=identity.login,
        )
        operation_id = build_privileged_operation_id_for_actor(
            descriptor_id=descriptor_id,
            actor=actor,
            source_event_id=envelope.source_event_id,
        )
        try:
            store = require_privileged_operation_store(record_store)
            try:
                replay = store.read_privileged_operation_record(operation_id)
            except FileNotFoundError:
                replay = None
            if replay is not None:
                request = replay.request
                if (
                    replay.requested_by != actor
                    or replay.source_event_id != envelope.source_event_id
                    or not isinstance(request, ManagedAuthzPolicySetProposalInput)
                    or not authorization_candidate_request_matches(
                        candidate_id=envelope.candidate_id,
                        request=request,
                        github_id=identity.github_id,
                        intent=envelope.intent,
                    )
                ):
                    raise PrivilegedOperationConflictError(
                        "Authorization candidate replay changed the original request."
                    )
                return AuthorizationCandidatePrepareResponse(
                    trace_id=trace_id,
                    state="planned",
                    operation_id=operation_id,
                )
            state, candidate = compile_authorization_candidate(
                candidate_id=envelope.candidate_id,
                current_policy=policy_record.policy,
                github_id=identity.github_id,
                intent=envelope.intent,
                record_store=record_store,
            )
            if state == "already_satisfied":
                return AuthorizationCandidatePrepareResponse(
                    trace_id=trace_id,
                    state=state,
                )
            if candidate is None:
                raise AuthorizationCandidatePreparationError(
                    "candidate_set_conflict", "Authorization candidate compiler returned no plan."
                )
            result = create_typed_privileged_operation_plan(
                record_store=record_store,
                descriptor_id=descriptor_id,
                actor=actor,
                source_kind="browser_api",
                source_event_id=envelope.source_event_id,
                request=candidate,
            )
        except AuthorizationCandidatePreparationError as error:
            preparation_errors = {
                "candidate_set_conflict": (
                    "authorization_candidate_set_conflict",
                    "The candidate administration set is occupied or has an unexpected shape.",
                ),
                "candidate_action_overlap": (
                    "authorization_candidate_action_overlap",
                    "Agent delivery administration already overlaps another explicit rule.",
                ),
                "current_activation_requires_stop": (
                    "authorization_candidate_activation_current",
                    "Stop the current agent delivery setup before removing its administration access.",
                ),
                "activation_storage_unavailable": (
                    "authorization_candidate_activation_state_unavailable",
                    "Agent delivery activation state is unavailable; removal cannot be prepared safely.",
                ),
                "activation_history_truncated": (
                    "authorization_candidate_activation_history_truncated",
                    "Agent delivery activation history exceeds the safe preparation window.",
                ),
            }
            code, message = preparation_errors.get(
                error.reason_code,
                (
                    "authorization_candidate_preparation_conflict",
                    "The authorization candidate cannot be prepared from current state.",
                ),
            )
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code=code,
                message=message,
            ) from error
        except PrivilegedOperationConflictError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="privileged_operation_plan_conflict",
                message="Privileged-operation plan request conflicts with existing state.",
            ) from error
        except (
            PrivilegedOperationPlannerError,
            PrivilegedOperationPlanningStoreError,
            PrivilegedOperationStoreUnavailableError,
            TypeError,
            ValueError,
        ) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Authorization candidate preparation is unavailable.",
            ) from error
        return AuthorizationCandidatePrepareResponse(
            trace_id=trace_id,
            state="planned",
            operation_id=result.record.operation_id,
        )

    def read_current_merge_train_policy(*, record_store: object) -> MergeTrainPolicyRecord:
        list_records = getattr(record_store, "list_merge_train_policy_records", None)
        if not callable(list_records):
            raise PrivilegedOperationPlanningStoreError(
                "Merge-train target preparation requires merge-train policy storage."
            )
        active_records = tuple(list_records(status="active", limit=2))
        if len(active_records) != 1:
            raise PrivilegedOperationPlannerError(
                "Merge-train target preparation requires exactly one active policy record."
            )
        return MergeTrainPolicyRecord.model_validate(active_records[0])

    def current_tracked_inventory(*, record_store: object) -> tuple[RepositoryInventoryRecord, ...]:
        inventory_state, tracked_records, _diagnostics = _read_current_inventory(record_store)
        if inventory_state != "complete":
            raise PrivilegedOperationPlanningStoreError(
                "Merge-train target preparation requires complete unambiguous repository inventory."
            )
        return tracked_records

    def read_ordinary_agent_merge_train_target_inputs(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> OrdinaryAgentMergeTrainTargetInputsResponse:
        trace_id = dependencies.common.next_trace_id()
        descriptor_id: PrivilegedOperationDescriptorId = "managed-merge-train-policy-import"
        require_managed_rule(
            identity=identity,
            action=descriptor_action(descriptor_id, "plan_action"),
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        try:
            active_record = read_current_merge_train_policy(record_store=record_store)
            tracked_repositories = current_tracked_inventory(record_store=record_store)
        except (
            PrivilegedOperationPlannerError,
            PrivilegedOperationPlanningStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Ordinary-agent merge target inputs are unavailable.",
            ) from error
        return OrdinaryAgentMergeTrainTargetInputsResponse(
            trace_id=trace_id,
            policy=OrdinaryAgentMergeTrainTargetPolicyInput(
                record_id=active_record.record_id,
                updated_at=normalize_merge_train_policy_timestamp(active_record.updated_at),
                policy_sha256=active_record.policy_sha256,
                configured_policy_keys=tuple(sorted(_policy_key_payloads(active_record))),
            ),
            tracked_repositories=tuple(
                OrdinaryAgentMergeTrainTargetInventoryInput(
                    repository_id=record.repository_id,
                    repository=record.repository,
                    inventory_record_id=record.record_id,
                    inventory_digest=record.inventory_digest,
                )
                for record in tracked_repositories
            ),
        )

    def prepare_ordinary_agent_merge_train_target(
        envelope: OrdinaryAgentMergeTrainTargetPrepareEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> OrdinaryAgentMergeTrainTargetPrepareResponse:
        trace_id = dependencies.common.next_trace_id()
        descriptor_id: PrivilegedOperationDescriptorId = "managed-merge-train-policy-import"
        propose_action = descriptor_action(descriptor_id, "plan_action")
        require_managed_rule(
            identity=identity,
            action=propose_action,
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        actor = PrivilegedOperationActor(
            identity_type="github_human",
            github_id=identity.github_id,
            login=identity.login,
        )
        operation_id = build_privileged_operation_id_for_actor(
            descriptor_id=descriptor_id,
            actor=actor,
            source_event_id=envelope.source_event_id,
        )
        try:
            store = require_privileged_operation_store(record_store)
            try:
                replay = store.read_privileged_operation_record(operation_id)
            except FileNotFoundError:
                replay = None
            if replay is not None:
                replay_request = replay.request
                if (
                    replay.requested_by != actor
                    or replay.source_event_id != envelope.source_event_id
                    or not isinstance(replay_request, ManagedMergeTrainPolicyImportProposalInput)
                    or replay_request.preparation_context is None
                    or replay_request.preparation_context.intent != envelope.intent
                ):
                    raise PrivilegedOperationConflictError(
                        "Ordinary-agent merge target replay changed the original intent."
                    )
                return OrdinaryAgentMergeTrainTargetPrepareResponse(
                    trace_id=trace_id,
                    state="planned",
                    operation_id=operation_id,
                )
            active_record = read_current_merge_train_policy(record_store=record_store)
            inventory_by_id = {
                record.repository_id: record
                for record in current_tracked_inventory(record_store=record_store)
            }
            inventory_record = inventory_by_id.get(envelope.intent.repository_id)
            if inventory_record is None:
                raise PrivilegedOperationConflictError(
                    "Ordinary-agent merge target requires current tracked repository inventory."
                )
            target = MergeTrainRepositoryPolicy(
                repository=inventory_record.repository,
                base_branch=envelope.intent.base_branch,
                enqueue_label=envelope.intent.enqueue_label,
                blocked_label=envelope.intent.blocked_label,
                stack_child_disposition_label=envelope.intent.stack_child_disposition_label,
                merge_method=envelope.intent.merge_method,
                engineering_review_mode=envelope.intent.engineering_review_mode,
                failure_policy=envelope.intent.failure_policy,
                enqueue=envelope.intent.enqueue,
                merge_identity=envelope.intent.merge_identity,
                github_token=MergeTrainGitHubTokenSource(),
                scheduler=MergeTrainSchedulerPolicy(enabled=False, mutate=False),
                provider_delivery_protection_expectation=(
                    envelope.intent.provider_delivery_protection_expectation
                ),
            )
            active_payloads = _policy_key_payloads(active_record)
            target_payload = target.model_dump(mode="json")
            existing_payload = active_payloads.get(target.policy_key)
            if existing_payload is not None:
                if existing_payload == target_payload:
                    return OrdinaryAgentMergeTrainTargetPrepareResponse(
                        trace_id=trace_id,
                        state="already_satisfied",
                    )
                raise PrivilegedOperationConflictError(
                    "Ordinary-agent merge target already has a different policy."
                )
            prepared_at = datetime.now(timezone.utc)
            updated_at = prepared_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
            candidate_policy = MergeTrainPolicy(
                policies=(*active_record.policy.policies, target),
            )
            candidate_record = MergeTrainPolicyRecord(
                record_id=build_merge_train_policy_record_id(
                    updated_at=updated_at,
                    policy_sha256=candidate_policy.policy_sha256,
                ),
                source="privileged-operation:ordinary-agent-merge-target-preparation",
                updated_at=updated_at,
                policy=candidate_policy,
            )
            request = ManagedMergeTrainPolicyImportProposalInput(
                record=candidate_record,
                reason=(
                    "Prepare ordinary-agent-only merge target "
                    f"{canonical_json_sha256(envelope.intent.model_dump(mode='json'))[:24]}."
                ),
                preparation_context=ManagedMergeTrainPolicyPreparationContext(
                    intent=envelope.intent,
                    expected_active_record_id=active_record.record_id,
                    expected_active_policy_sha256=active_record.policy_sha256,
                    expected_active_updated_at=normalize_merge_train_policy_timestamp(
                        active_record.updated_at
                    ),
                    target_policy_key=target.policy_key,
                ),
            )
            result = create_typed_privileged_operation_plan(
                record_store=record_store,
                descriptor_id=descriptor_id,
                actor=actor,
                source_kind="browser_api",
                source_event_id=envelope.source_event_id,
                request=request,
                now=lambda: prepared_at,
            )
        except PrivilegedOperationConflictError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="ordinary_agent_merge_target_conflict",
                message="Ordinary-agent merge target preparation conflicts with current state.",
            ) from error
        except PrivilegedOperationPlannerError as error:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="ordinary_agent_merge_target_baseline_drift",
                message="Ordinary-agent merge target preparation baseline changed; retry from inputs.",
            ) from error
        except (
            PrivilegedOperationPlanningStoreError,
            PrivilegedOperationStoreUnavailableError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="privileged_operation_planning_unavailable",
                message="Ordinary-agent merge target preparation is unavailable.",
            ) from error
        return OrdinaryAgentMergeTrainTargetPrepareResponse(
            trace_id=trace_id,
            state="planned",
            operation_id=result.record.operation_id,
        )

    def read_ordinary_agent_delivery_authorization_inputs(
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse:
        trace_id = dependencies.common.next_trace_id()
        descriptor_id: PrivilegedOperationDescriptorId = "managed-authz-policy-set"
        propose_action = descriptor_action(descriptor_id, "plan_action")
        if not isinstance(identity, GitHubHumanIdentity):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Only authorized humans can inspect authorization candidate inputs.",
            )
        require_managed_rule(
            identity=identity,
            action=propose_action,
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        policy_record, _managed_set_id, _managed_rule_id = require_immutable_approval_rule(
            identity=identity,
            action=propose_action,
            trace_id=trace_id,
            descriptor_id=descriptor_id,
        )
        if policy_record.status != "active":
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="The active authorization policy record is unavailable.",
            )
        if not authz_policy_allows_immutable_github_id_administration(
            policy=policy_record.policy,
            github_id=identity.github_id,
        ):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot inspect authorization candidate inputs.",
            )
        observed_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        return read_ordinary_agent_delivery_authorization_candidate_inputs(
            record_store=record_store,
            policy_record=policy_record,
            trace_id=trace_id,
            observed_at=observed_at,
        )

    app.add_api_route(
        ORDINARY_AGENT_MERGE_TRAIN_TARGET_INPUTS_ROUTE,
        read_ordinary_agent_merge_train_target_inputs,
        methods=["GET"],
        response_model=OrdinaryAgentMergeTrainTargetInputsResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Read ordinary-agent merge target preparation inputs",
        operation_id="read_ordinary_agent_merge_train_target_inputs",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        ORDINARY_AGENT_MERGE_TRAIN_TARGET_PREPARE_ROUTE,
        prepare_ordinary_agent_merge_train_target,
        methods=["POST"],
        response_model=OrdinaryAgentMergeTrainTargetPrepareResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Prepare one ordinary-agent-only merge target",
        operation_id="prepare_ordinary_agent_merge_train_target",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        ORDINARY_AGENT_DELIVERY_AUTHORIZATION_CANDIDATE_INPUTS_ROUTE,
        read_ordinary_agent_delivery_authorization_inputs,
        methods=["GET"],
        response_model=OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Read ordinary-agent delivery authorization candidate inputs",
        operation_id="read_ordinary_agent_delivery_authorization_candidate_inputs",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        AUTHORIZATION_CANDIDATE_PREPARE_ROUTE,
        prepare_authorization_candidate,
        methods=["POST"],
        response_model=AuthorizationCandidatePrepareResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Prepare a closed authorization candidate",
        operation_id="prepare_authorization_candidate",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_PLANS_ROUTE,
        plan_ordinary_agent_delivery_activation,
        methods=["POST"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Plan ordinary-agent delivery activation setup or revocation",
        operation_id="plan_ordinary_agent_delivery_activation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        ORDINARY_AGENT_DELIVERY_ACTIVATION_OPTIONS_ROUTE,
        read_ordinary_agent_delivery_activation_options,
        methods=["GET"],
        response_model=OrdinaryAgentDeliveryActivationOptionsResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="List server-resolved ordinary-agent activation choices",
        operation_id="read_ordinary_agent_delivery_activation_options",
        tags=["privileged-operations"],
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
        PRIVILEGED_OPERATION_REVIEW_ROUTE,
        read_human_privileged_operation_review,
        methods=["GET"],
        response_model=PrivilegedOperationSemanticReviewResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Read a semantic privileged-operation review",
        operation_id="read_human_privileged_operation_review",
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
        PRIVILEGED_OPERATION_APPROVE_ROUTE,
        approve_human_privileged_operation,
        methods=["POST"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Approve a privileged-operation plan",
        operation_id="approve_human_privileged_operation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_REVOKE_ROUTE,
        revoke_human_privileged_operation,
        methods=["POST"],
        response_model=PrivilegedOperationHumanResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            404: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Revoke a privileged-operation approval",
        operation_id="revoke_human_privileged_operation",
        tags=["privileged-operations"],
    )
    app.add_api_route(
        PRIVILEGED_OPERATION_AGENT_PLANS_ROUTE,
        propose_agent_privileged_policy_operation,
        methods=["POST"],
        response_model=PrivilegedPolicyOperationAgentProposalResponse,
        responses={
            403: {"model": dependencies.common.error_response_model},
            409: {"model": dependencies.common.error_response_model},
            503: {"model": dependencies.common.error_response_model},
        },
        summary="Propose an inert managed authorization policy operation",
        operation_id="propose_agent_privileged_policy_operation",
        tags=["agent", "privileged-operations"],
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
