import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Response

from control_plane.authz_grant_service import (
    _authz_policy_principal_rule_counts,
    _authz_policy_rule_collections,
    _authz_rule_sha256,
    export_managed_authz_policy_set,
    summarize_active_authz_policy_health_record,
)
from control_plane.contracts.authz_administration import (
    AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT,
    AUTHZ_POLICY_ADMINISTRATION_MANAGED_RULE_LIMIT,
    AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
    AuthzActivePolicyExportResponse,
    AuthzManagedRuleIdentityCollection,
    AuthzManagedRuleIdentitySummary,
    AuthzManagedSetRollbackProposalPayload,
    AuthzManagedSetRollbackProposalRequest,
    AuthzManagedSetRollbackProposalResponse,
    AuthzPolicyAdministrationProvenance,
    AuthzPolicyAdministrationReadResponse,
    AuthzPolicyRevisionAuditSummary,
    AuthzPolicyRevisionHistoryEntry,
    AuthzPolicyRevisionHistoryResponse,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    ManagedAuthzPolicySetProposalInput,
)
from control_plane.durable_operation_authorization import (
    ManagedRuleAuthorizationError,
    require_single_managed_github_id_rule_identity,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore


AUTHZ_POLICY_ADMINISTRATION_ROUTE = "/v1/authz-policies/administration"
AUTHZ_POLICY_REVISION_HISTORY_ROUTE = "/v1/authz-policies/revisions"
AUTHZ_ACTIVE_POLICY_EXPORT_ROUTE = "/v1/authz-policies/active/export"
AUTHZ_MANAGED_SET_ROLLBACK_PROPOSAL_ROUTE = "/v1/authz-policies/managed-rule-sets/rollback-proposal"


@dataclass(frozen=True, slots=True)
class AuthzAdministrationRouteDependencies:
    common: ReadRouteDependencies
    read_nonrenewing_identity: Callable[..., LaunchplaneIdentity]
    runtime_policy_reader: Callable[[], LaunchplaneAuthzPolicy]


def register_authz_administration_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: AuthzAdministrationRouteDependencies,
) -> None:
    def policy_provenance(
        record: LaunchplaneAuthzPolicyRecord,
    ) -> AuthzPolicyAdministrationProvenance:
        return AuthzPolicyAdministrationProvenance(
            record_id=record.record_id,
            revision=record.revision,
            status=record.status,
            source=record.source,
            updated_at=record.updated_at,
            policy_sha256=record.policy_sha256,
            schema_version=record.policy.schema_version,
        )

    def read_single_active_record(
        *, record_store: object, trace_id: str
    ) -> LaunchplaneAuthzPolicyRecord:
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Authz policy administration reads require database record storage.",
            )
        list_records = getattr(record_store, "list_authz_policy_records", None)
        if not callable(list_records):
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Authz policy administration reads require database record storage.",
            )
        records = tuple(list_records(status="active", limit=2))
        if not records:
            raise dependencies.common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            )
        if len(records) != 1:
            raise dependencies.common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="active_authz_policy_ambiguous",
                message="Launchplane requires exactly one active authorization policy.",
            )
        return LaunchplaneAuthzPolicyRecord.model_validate(records[0])

    def require_administration_read(
        *, identity: LaunchplaneIdentity, record_store: object, trace_id: str
    ) -> LaunchplaneAuthzPolicyRecord:
        eligible = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        if not eligible or not dependencies.runtime_policy_reader().allows(
            identity=identity,
            action=AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
            product="launchplane",
            context="launchplane",
        ):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authorization policy administration.",
            )
        active_record = read_single_active_record(record_store=record_store, trace_id=trace_id)
        if not active_record.policy.allows(
            identity=identity,
            action=AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
            product="launchplane",
            context="launchplane",
        ):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read Launchplane authorization policy administration.",
            )
        return active_record

    def require_proposal_authority(
        *, identity: LaunchplaneIdentity, active_record: LaunchplaneAuthzPolicyRecord, trace_id: str
    ) -> None:
        if not isinstance(identity, GitHubHumanIdentity) or identity.role != "admin":
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="This operation requires a GitHub-human administrator.",
            )
        if not dependencies.runtime_policy_reader().allows(
            identity=identity,
            action=AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="global"),
        ):
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot propose Launchplane authorization policy operations.",
            )
        try:
            require_single_managed_github_id_rule_identity(
                policy=active_record.policy,
                identity=identity,
                action=AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
                product="launchplane",
                context="launchplane",
                target=AuthorizationTarget(scope="global"),
            )
        except ManagedRuleAuthorizationError as error:
            raise dependencies.common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot propose Launchplane authorization policy operations.",
            ) from error

    def managed_rule_identities(
        record: LaunchplaneAuthzPolicyRecord,
    ) -> AuthzManagedRuleIdentityCollection:
        items = tuple(
            AuthzManagedRuleIdentitySummary(
                managed_set_id=rule.managed_set_id,
                managed_rule_id=rule.managed_rule_id,
                principal_type=principal_type,
                rule_sha256=_authz_rule_sha256(rule),
            )
            for principal_type, rules in _authz_policy_rule_collections(record.policy)
            for rule in rules
            if rule.managed_set_id is not None and rule.managed_rule_id is not None
        )
        bounded_items = items[:AUTHZ_POLICY_ADMINISTRATION_MANAGED_RULE_LIMIT]
        return AuthzManagedRuleIdentityCollection(
            total_count=len(items),
            returned_count=len(bounded_items),
            truncated=len(items) > len(bounded_items),
            items=bounded_items,
        )

    def audit_summary(audit: dict[str, object]) -> AuthzPolicyRevisionAuditSummary:
        if not audit:
            return AuthzPolicyRevisionAuditSummary(audit_present=False)
        serialized = json.dumps(audit, sort_keys=True, separators=(",", ":"), default=str)
        diff = audit.get("diff")
        allowed_counts = (
            "added_rule_count",
            "adopted_rule_count",
            "updated_rule_count",
            "removed_rule_count",
            "unchanged_rule_count",
            "policy_safety_blocker_count",
            "operational_readiness_blocked_rule_count",
        )
        diff_counts = (
            {name: value for name in allowed_counts if isinstance(value := diff.get(name), int)}
            if isinstance(diff, dict)
            else {}
        )
        changed = audit.get("changed")
        return AuthzPolicyRevisionAuditSummary(
            audit_present=True,
            audit_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
            operation=str(audit.get("operation") or ""),
            mode=str(audit.get("mode") or ""),
            managed_set_id=str(audit.get("managed_set_id") or ""),
            changed=changed if isinstance(changed, bool) else None,
            diff_counts=diff_counts,
        )

    def read_administration(
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_nonrenewing_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> AuthzPolicyAdministrationReadResponse:
        trace_id = dependencies.common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        active_record = require_administration_read(
            identity=identity, record_store=record_store, trace_id=trace_id
        )
        snapshot = summarize_active_authz_policy_health_record(
            record=active_record, caller_identity=identity
        )
        return AuthzPolicyAdministrationReadResponse(
            trace_id=trace_id,
            policy=policy_provenance(active_record),
            principal_rule_counts=_authz_policy_principal_rule_counts(active_record.policy),
            health=snapshot.health,
            managed_sets=snapshot.managed_sets,
            reachable_administrators=snapshot.reachable_administrators,
            managed_rules=managed_rule_identities(active_record),
        )

    def read_history(
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_nonrenewing_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> AuthzPolicyRevisionHistoryResponse:
        trace_id = dependencies.common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        require_administration_read(identity=identity, record_store=record_store, trace_id=trace_id)
        list_records = getattr(record_store, "list_authz_policy_records")
        records = tuple(list_records(limit=AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT + 1))
        bounded_records = tuple(
            LaunchplaneAuthzPolicyRecord.model_validate(record)
            for record in records[:AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT]
        )
        return AuthzPolicyRevisionHistoryResponse(
            trace_id=trace_id,
            returned_count=len(bounded_records),
            truncated=len(records) > len(bounded_records),
            revisions=tuple(
                AuthzPolicyRevisionHistoryEntry(
                    policy=policy_provenance(record), audit=audit_summary(record.audit)
                )
                for record in bounded_records
            ),
        )

    def export_active_policy(
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_nonrenewing_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> AuthzActivePolicyExportResponse:
        trace_id = dependencies.common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        active_record = require_administration_read(
            identity=identity, record_store=record_store, trace_id=trace_id
        )
        require_proposal_authority(
            identity=identity, active_record=active_record, trace_id=trace_id
        )
        return AuthzActivePolicyExportResponse(
            trace_id=trace_id,
            policy=policy_provenance(active_record),
            canonical_policy=active_record.policy,
        )

    def build_rollback_proposal(
        request: AuthzManagedSetRollbackProposalRequest,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_nonrenewing_identity)],
        record_store: Annotated[object, Depends(dependencies.common.get_record_store)],
    ) -> AuthzManagedSetRollbackProposalResponse:
        trace_id = dependencies.common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        active_record = require_administration_read(
            identity=identity, record_store=record_store, trace_id=trace_id
        )
        require_proposal_authority(
            identity=identity, active_record=active_record, trace_id=trace_id
        )
        list_records = getattr(record_store, "list_authz_policy_records")
        records = tuple(
            LaunchplaneAuthzPolicyRecord.model_validate(record)
            for record in list_records(limit=AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT)
        )
        target_record = next(
            (record for record in records if record.revision == request.target_revision), None
        )
        if target_record is None:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="authz_policy_revision_not_found",
                message="The requested authorization policy revision is unavailable in bounded history.",
            )
        try:
            desired_policy = export_managed_authz_policy_set(
                policy=target_record.policy,
                managed_set_id=request.managed_set_id,
            )
        except LookupError as error:
            raise dependencies.common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="authz_managed_set_not_found",
                message="The requested managed authorization rule set is absent from that revision.",
            ) from error
        return AuthzManagedSetRollbackProposalResponse(
            trace_id=trace_id,
            target_policy=policy_provenance(target_record),
            proposal=AuthzManagedSetRollbackProposalPayload(
                source_event_id=request.source_event_id,
                request=ManagedAuthzPolicySetProposalInput(
                    managed_set_id=request.managed_set_id,
                    desired_policy=desired_policy,
                    reason=request.reason,
                    related_issue=request.related_issue,
                ),
            ),
        )

    responses = {
        401: {"model": dependencies.common.error_response_model},
        403: {"model": dependencies.common.error_response_model},
        404: {"model": dependencies.common.error_response_model},
        409: {"model": dependencies.common.error_response_model},
        503: {"model": dependencies.common.error_response_model},
    }
    app.add_api_route(
        AUTHZ_POLICY_ADMINISTRATION_ROUTE,
        read_administration,
        methods=["GET"],
        response_model=AuthzPolicyAdministrationReadResponse,
        operation_id="read_authz_policy_administration",
        summary="Read redacted active authorization policy administration evidence",
        responses=responses,
    )
    app.add_api_route(
        AUTHZ_POLICY_REVISION_HISTORY_ROUTE,
        read_history,
        methods=["GET"],
        response_model=AuthzPolicyRevisionHistoryResponse,
        operation_id="read_authz_policy_revision_history",
        summary="Read bounded redacted authorization policy revision history",
        responses=responses,
    )
    app.add_api_route(
        AUTHZ_ACTIVE_POLICY_EXPORT_ROUTE,
        export_active_policy,
        methods=["GET"],
        response_model=AuthzActivePolicyExportResponse,
        operation_id="export_active_authz_policy",
        summary="Export the full-fidelity active authorization policy",
        responses=responses,
    )
    app.add_api_route(
        AUTHZ_MANAGED_SET_ROLLBACK_PROPOSAL_ROUTE,
        build_rollback_proposal,
        methods=["POST"],
        response_model=AuthzManagedSetRollbackProposalResponse,
        operation_id="build_authz_managed_set_rollback_proposal",
        summary="Build a managed authorization rollback proposal for existing submission",
        responses=responses,
    )
