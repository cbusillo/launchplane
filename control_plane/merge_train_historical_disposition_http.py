"""Explicit legacy-operator HTTP orchestration for historical disposition."""

from pathlib import Path

from starlette.responses import JSONResponse

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    idempotency_scope,
    replay_idempotent_response,
)
from control_plane.merge_train_controller_run_once import MergeTrainControllerRunOnceEnvelope
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.merge_train_github import GitHubMergeTrainClient, UrllibMergeTrainGitHubTransport
from control_plane.merge_train_historical_completion import (
    HistoricalCompletionAssessmentFailure,
    HistoricalCompletionPreflightResponse,
    HistoricalCompletionPreflightResult,
    assess_merge_train_historical_completion,
)
from control_plane.merge_train_historical_disposition import (
    HistoricalDispositionError,
    HistoricalDispositionRequest,
)
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore


def run_merge_train_historical_disposition(
    *,
    envelope: MergeTrainControllerRunOnceEnvelope,
    control_plane_root: Path,
    identity: LaunchplaneIdentity,
    store: PostgresRecordStore,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    generated_at: str,
) -> AcceptedEvidenceResponse | HistoricalCompletionPreflightResponse | JSONResponse:
    assert envelope.historical_completion is not None
    recovery = HistoricalDispositionRequest(
        repository=envelope.repository,
        base_branch=envelope.base_branch,
        selector=envelope.historical_completion,
        identity=identity,
        scope=idempotency_scope(identity),
        route_path="/v1/work-graph/merge-train/controller/run-once",
        idempotency_key=idempotency_key if envelope.mutate else "",
        request_fingerprint=request_fingerprint,
    )
    try:
        if envelope.mutate and not recovery.idempotency_key:
            raise HistoricalDispositionError("idempotency_key_required", status_code=400)
        authority = store.authorize_merge_train_historical_completion(recovery)
        if authority.replay_record is not None:
            return replay_idempotent_response(
                trace_id=trace_id,
                stored_record=authority.replay_record,
                route_path=recovery.route_path,
            )
        # Recovery proof uses the public GitHub adapter endpoint. Caller-selected
        # endpoints cannot supply historical truth or receive the service token.
        if envelope.github_api_base_url != "https://api.github.com":
            raise HistoricalDispositionError("provider_endpoint_unsupported")
        snapshot = store.read_merge_train_historical_completion_snapshot(recovery, authority)
        repository_policy = authority.policy.policy.find_repository_policy(
            repository=recovery.repository, base_branch=recovery.base_branch
        )
        token = resolve_merge_train_github_token(
            source=repository_policy.github_token,
            repository=repository_policy.repository,
            control_plane_root=control_plane_root,
        )
        if not token:
            raise HistoricalDispositionError("github_token_not_configured", status_code=503)
        preflight = assess_merge_train_historical_completion(
            store=store,
            repository=recovery.repository,
            base_branch=recovery.base_branch,
            selector=recovery.selector,
            generated_at=generated_at,
            validated_snapshot=snapshot,
            github_client=GitHubMergeTrainClient(
                transport=UrllibMergeTrainGitHubTransport(token=token),
            ),
        )
        preflight = preflight.model_copy(
            update={"disposition_supported": True, "mutation_enabled": True}
        )
        if not preflight.evidence_eligible or preflight.provider_evidence is None:
            if envelope.mutate:
                raise HistoricalDispositionError(preflight.reason_code)
        elif envelope.mutate:
            completed = store.finalize_merge_train_historical_completion(
                request=recovery,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=preflight.provider_evidence,
                trace_id=trace_id,
            )
            if completed.response_trace_id != trace_id:
                return replay_idempotent_response(
                    trace_id=trace_id, stored_record=completed, route_path=recovery.route_path
                )
            return AcceptedEvidenceResponse.model_validate(completed.response_payload)
        elif store.read_merge_train_historical_completion_snapshot(recovery, authority) != snapshot:
            raise HistoricalDispositionError("store_state_changed")
        return HistoricalCompletionPreflightResponse(
            trace_id=trace_id,
            result=HistoricalCompletionPreflightResult(
                repository=recovery.repository,
                base_branch=recovery.base_branch,
                historical_completion_preflight=preflight,
            ),
        )
    except (HistoricalDispositionError, HistoricalCompletionAssessmentFailure) as error:
        status_code = error.status_code if isinstance(error, HistoricalDispositionError) else 409
        record_id = error.record_id if isinstance(error, HistoricalDispositionError) else ""
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "authorization_denied"
                    if status_code == 403
                    else "historical_completion_not_applied",
                    "message": "Historical completion was not applied.",
                },
                "details": {
                    "reason_code": error.reason,
                    "record_id": record_id,
                    "retryable": error.reason == "recovery_busy",
                    "automatic_retry_allowed": False,
                    "provider_effect_attempted": False,
                    "admission_created": False,
                    "fence_released": False,
                },
            },
        )
