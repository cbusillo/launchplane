"""Redacted, lane-scoped review and human decisions for backup proposals."""

from typing import Annotated, Literal
from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from control_plane.backup_policy_proposals import decide_backup_policy
from control_plane.contracts.backup_policy_proposal import BackupPolicyHumanEvidence
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationRecord,
    PrivilegedOperationStatus,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.privileged_operation_registry import PrivilegedOperationPlannerError
from control_plane.privileged_operation_service import (
    read_privileged_operation,
    require_privileged_operation_store,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity
from collections.abc import Callable


class BackupPolicyProposalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    status: PrivilegedOperationStatus
    protection: BackupPolicyHumanEvidence
    can_approve: bool
    can_decline: bool


class BackupPolicyProposalListResponse(BaseModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    proposals: tuple[BackupPolicyProposalReview, ...]


class BackupPolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "decline"]
    source_event_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )


class BackupPolicyDecisionResponse(BaseModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    proposal: BackupPolicyProposalReview


def register_backup_policy_proposal_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., GitHubHumanIdentity],
) -> None:
    def authorize(
        identity: LaunchplaneIdentity,
        evidence: BackupPolicyHumanEvidence,
        action: str,
        trace_id: str,
    ) -> None:
        if not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=evidence.product,
            context=evidence.context,
            target=AuthorizationTarget(scope="instance", instances=(evidence.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot access this lane's backup proposals.",
            )

    def review(
        record: PrivilegedOperationRecord, identity: LaunchplaneIdentity
    ) -> BackupPolicyProposalReview:
        assert isinstance(record.evidence, BackupPolicyHumanEvidence)
        human_admin = isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        return BackupPolicyProposalReview(
            operation_id=record.operation_id,
            status=record.status,
            protection=record.evidence,
            can_approve=human_admin and record.status == "planned",
            can_decline=human_admin and record.status == "planned",
        )

    def list_backup_policy_proposals(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        instance: Annotated[str, Query(min_length=1)],
    ) -> BackupPolicyProposalListResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="production_backup_authority.read",
            product=product,
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this lane's backup proposals.",
            )
        try:
            store = require_privileged_operation_store(record_store)
            records = store.list_privileged_operation_records(
                descriptor_id="production-backup-policy"
            )
            return BackupPolicyProposalListResponse(
                trace_id=trace_id,
                proposals=tuple(
                    review(
                        read_privileged_operation(
                            record_store=store, operation_id=record.operation_id
                        ),
                        identity,
                    )
                    for record in records
                    if isinstance(record.evidence, BackupPolicyHumanEvidence)
                    and (record.evidence.product, record.evidence.context, record.evidence.instance)
                    == (product, context, instance)
                ),
            )
        except (TypeError, LookupError) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="backup_proposal_unavailable",
                message="Backup proposal storage is unavailable.",
            ) from error

    def decide_backup_policy_proposal(
        envelope: BackupPolicyDecision,
        identity: Annotated[GitHubHumanIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        operation_id: Annotated[str, Path(min_length=1, max_length=96)],
    ) -> BackupPolicyDecisionResponse:
        trace_id = dependencies.next_trace_id()
        try:
            record = read_privileged_operation(record_store=record_store, operation_id=operation_id)
            if record.descriptor_id != "production-backup-policy" or not isinstance(
                record.evidence, BackupPolicyHumanEvidence
            ):
                raise FileNotFoundError()
            authorize(identity, record.evidence, "production_backup_authority.write", trace_id)
            if identity.role != "admin":
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Only a signed-in Director administrator may decide backup proposals.",
                )
            result = decide_backup_policy(
                record_store=record_store,
                record=record,
                identity=identity,
                decision=envelope.decision,
                source_event_id=envelope.source_event_id,
                authorization_allows=dependencies.authorization_allows,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="backup_proposal_missing",
                message="Backup proposal was not found.",
            ) from error
        except (TypeError, LookupError) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="backup_proposal_unavailable",
                message="Backup proposal authority or storage is unavailable.",
            ) from error
        except (ValueError, PrivilegedOperationPlannerError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="backup_proposal_conflict",
                message="The proposal or its authority changed. Refresh and request a new proposal if needed.",
            ) from error
        return BackupPolicyDecisionResponse(
            trace_id=trace_id, proposal=review(result.record, identity)
        )

    app.add_api_route(
        "/v1/production-backup-authority/proposals",
        list_backup_policy_proposals,
        methods=["GET"],
        response_model=BackupPolicyProposalListResponse,
        operation_id="list_backup_policy_proposals",
    )
    app.add_api_route(
        "/v1/production-backup-authority/proposals/{operation_id}/decision",
        decide_backup_policy_proposal,
        methods=["POST"],
        response_model=BackupPolicyDecisionResponse,
        operation_id="decide_backup_policy_proposal",
    )
