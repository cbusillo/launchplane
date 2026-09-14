"""Pure records for an atomic, observation-only legacy controller disposition."""

from dataclasses import dataclass

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionEvidence,
    MergeTrainHistoricalCompletionProviderEvidence,
    MergeTrainHistoricalCompletionSelector,
    MergeTrainHistoricalDispositionAuthorization,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainServiceAuthz,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.merge_train_historical_completion import HistoricalCompletionSnapshot
from control_plane.service_auth import LaunchplaneIdentity


@dataclass(frozen=True)
class HistoricalDispositionRequest:
    repository: str
    base_branch: str
    selector: MergeTrainHistoricalCompletionSelector
    identity: LaunchplaneIdentity
    scope: str
    route_path: str
    idempotency_key: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", self.repository.strip().lower())
        object.__setattr__(self, "base_branch", self.base_branch.strip())
        object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        if self.scope != idempotency_scope(self.identity):
            raise ValueError("historical disposition scope must match the authenticated caller")


@dataclass(frozen=True)
class HistoricalDispositionAuthority:
    policy: MergeTrainPolicyRecord
    authz: LaunchplaneAuthzPolicyRecord
    replay_record: LaunchplaneIdempotencyRecord | None = None

    def service_authz(self, request: HistoricalDispositionRequest) -> MergeTrainServiceAuthz:
        return self.policy.policy.find_repository_policy(
            repository=request.repository, base_branch=request.base_branch
        ).service_authz


class HistoricalDispositionError(Exception):
    def __init__(self, reason: str, status_code: int = 409, record_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.record_id = record_id


@dataclass(frozen=True)
class HistoricalDispositionBundle:
    successor: MergeTrainBatchLandingPlanRecord
    controller: MergeTrainControllerStateRecord
    idempotency_record: LaunchplaneIdempotencyRecord


def historical_disposition_record_id(request: HistoricalDispositionRequest) -> str:
    binding = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "selector": request.selector.model_dump(mode="json"),
    }
    return f"merge-train-historical-completion-{canonical_json_sha256(binding)}"


def build_historical_disposition(
    *,
    request: HistoricalDispositionRequest,
    authority: HistoricalDispositionAuthority,
    snapshot: HistoricalCompletionSnapshot,
    provider_evidence: MergeTrainHistoricalCompletionProviderEvidence,
    recorded_at: str,
    trace_id: str,
) -> HistoricalDispositionBundle:
    """Build a bundle; only the native guarded transaction may persist it."""
    source = snapshot.landing
    plan = source.landing_plan
    service_authz = authority.service_authz(request)
    authorization = MergeTrainHistoricalDispositionAuthorization(
        actor_scope=request.scope,
        idempotency_key=request.idempotency_key,
        recorded_at=recorded_at,
        action=service_authz.action,
        product=service_authz.product,
        context=service_authz.context,
        authz_policy_record_id=authority.authz.record_id,
        authz_policy_revision=authority.authz.revision,
        authz_policy_sha256=authority.authz.policy_sha256,
        merge_policy_record_id=authority.policy.record_id,
        merge_policy_sha256=authority.policy.policy_sha256,
    )
    historical = MergeTrainHistoricalCompletionEvidence(
        schema_version=2,
        classification="observed_merged_without_admission",
        authority_state="observation_only",
        source_landing_plan_record_id=source.record_id,
        source_landing_plan_sha256=plan.landing_plan_sha256,
        controller_key=snapshot.controller.controller_key,
        repository=plan.repository,
        base_branch=plan.base_branch,
        landing_plan_id=plan.plan_id,
        batch_id=plan.batch_id,
        candidate_sha=plan.candidate_sha,
        candidate_sha256=plan.candidate_sha256,
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        trace_id=trace_id,
        provider_evidence=provider_evidence,
        disposition_authorization=authorization,
    )
    stale_plan = MergeTrainBatchLandingPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "entries": [
                {
                    **entry.model_dump(mode="json"),
                    "status": "stale",
                    "recorded_rolling_base_sha": "",
                    "recorded_rolling_base_tree_sha": "",
                    "landed_head_sha": "",
                    "landed_head_tree_sha": "",
                    "merge_commit_sha": "",
                    "merge_commit_tree_sha": "",
                }
                for entry in plan.entries
            ],
        }
    )
    successor = MergeTrainBatchLandingPlanRecord(
        schema_version=2,
        record_id=historical_disposition_record_id(request),
        source="merge-train-controller:historical-completion",
        updated_at=recorded_at,
        landing_plan=stale_plan,
        historical_completion=historical,
    )
    controller = MergeTrainControllerStateRecord.model_validate(
        {
            **snapshot.controller.model_dump(mode="json"),
            "status": "idle",
            "updated_at": recorded_at,
            "lease_owner": "",
            "lease_acquired_at": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "active_action": "",
            "active_phase": "",
            "active_record_id": "",
            "active_pull_request_number": None,
            "step_payload": {},
            "last_owner": request.scope,
            "last_action": "record_historical_completion",
            "last_phase": "historical_completion_recorded",
            "last_record_id": successor.record_id,
            "last_pull_request_number": (
                plan.entries[0].pull_request_number if len(plan.entries) == 1 else None
            ),
            "last_transition_at": recorded_at,
            "reconciliation_status": "clean",
            "reconciliation_detail": f"Historical observation recorded in {successor.record_id}.",
        }
    )
    response = {
        "status": "accepted",
        "trace_id": trace_id,
        "records": {"merge_train_batch_landing_plan_record_id": successor.record_id},
        "result": {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "apply",
            "controller_action": "record_historical_completion",
            "historical_completion_disposition": {
                "record_id": successor.record_id,
                "source_landing_plan_record_id": source.record_id,
                "landing_plan_sha256": plan.landing_plan_sha256,
                "provider_evidence_sha256": canonical_json_sha256(
                    provider_evidence.model_dump(mode="json")
                ),
                "selector": request.selector.model_dump(mode="json"),
                "classification": historical.classification,
                "authority_state": historical.authority_state,
                "disposition_authorization": authorization.model_dump(mode="json"),
                "provider_effect_attempted": False,
                "admission_created": False,
                "fence_released": True,
                "automatic_retry_allowed": False,
            },
        },
    }
    return HistoricalDispositionBundle(
        successor=successor,
        controller=controller,
        idempotency_record=LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_idempotency_record_id(response_trace_id=trace_id),
            scope=request.scope,
            route_path=request.route_path,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.request_fingerprint,
            response_status_code=202,
            response_trace_id=trace_id,
            recorded_at=recorded_at,
            response_payload=response,
        ),
    )
