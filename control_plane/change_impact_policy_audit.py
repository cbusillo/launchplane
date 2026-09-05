"""Audited policy administration by the write route's existing principals."""

from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from control_plane.change_impact_service import (
    ChangeImpactApplyMode,
    ChangeImpactPolicyApplyResult,
    ChangeImpactPolicyReadStore,
    _validate_policy_append,
    validate_change_impact_policy_apply_request,
)
from control_plane.contracts.change_impact import ChangeImpactPolicyRecord
from control_plane.contracts.change_impact_audit import (
    ChangeImpactPolicyAuditRecord,
    ChangeImpactPolicyAuditedWriteResult,
    ChangeImpactPolicyWorkflowIdentity,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
)


class ChangeImpactPolicyAttributionError(ValueError):
    """The authenticated principal cannot supply policy-writer provenance."""


class ChangeImpactPolicyAuditedStore(ChangeImpactPolicyReadStore, Protocol):
    def compare_and_write_change_impact_policy_record_with_audit(
        self,
        record: ChangeImpactPolicyRecord,
        *,
        audit: ChangeImpactPolicyAuditRecord,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> ChangeImpactPolicyAuditedWriteResult: ...


def require_change_impact_policy_audited_store(store: object) -> ChangeImpactPolicyAuditedStore:
    if not all(
        callable(getattr(store, method, None))
        for method in (
            "list_change_impact_policy_records",
            "compare_and_write_change_impact_policy_record_with_audit",
        )
    ):
        raise TypeError("Audited change-impact policy writes require database storage.")
    return cast(ChangeImpactPolicyAuditedStore, store)


def derive_change_impact_policy_audit(
    *,
    identity: LaunchplaneIdentity,
    record: ChangeImpactPolicyRecord,
    trace_id: str,
) -> ChangeImpactPolicyAuditRecord:
    workflow = None
    kind: Literal["local_admin", "local_operator", "github_actions"]
    if isinstance(identity, LocalAdminIdentity):
        kind = "local_admin"
    elif isinstance(identity, LocalOperatorIdentity):
        kind = "local_operator"
    elif isinstance(identity, GitHubActionsIdentity):
        kind = "github_actions"
        workflow = ChangeImpactPolicyWorkflowIdentity(
            repository=identity.repository,
            repository_id=identity.repository_id,
            repository_owner_id=identity.repository_owner_id,
            workflow_ref=identity.workflow_ref,
            job_workflow_ref=identity.job_workflow_ref,
            ref=identity.ref,
            sha=identity.sha,
        )
    else:
        raise ChangeImpactPolicyAttributionError(
            "Caller identity cannot be attributed to a policy administration write."
        )
    return ChangeImpactPolicyAuditRecord(
        record_id=record.record_id,
        policy_digest=record.policy_digest,
        actor_kind=kind,
        actor_subject=identity.subject,
        workflow_identity=workflow,
        trace_id=trace_id,
        recorded_at=datetime.now(timezone.utc),
    )


def apply_change_impact_policy_with_audit(
    *,
    store: ChangeImpactPolicyAuditedStore,
    record: ChangeImpactPolicyRecord,
    audit: ChangeImpactPolicyAuditRecord,
    expected_current_record_id: str = "",
    expected_current_policy_digest: str = "",
    mode: ChangeImpactApplyMode = "apply",
) -> ChangeImpactPolicyApplyResult:
    validate_change_impact_policy_apply_request(record=record, mode=mode)
    replay = _validate_policy_append(
        records=store.list_change_impact_policy_records(repository_id=record.repository_id),
        record=record,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
    )
    if mode == "dry_run":
        return ChangeImpactPolicyApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
            attribution_status="not_applied",
        )
    result = store.compare_and_write_change_impact_policy_record_with_audit(
        record,
        audit=audit,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
    )
    return ChangeImpactPolicyApplyResult(
        status="replayed" if result.status == "replayed" else "applied",
        record=record,
        audit=result.audit,
        attribution_status="attributed" if result.audit is not None else "legacy_unattributed",
    )
