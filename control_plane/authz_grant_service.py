from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
)


TimestampProvider = Callable[[], str]


class AuthzPolicyRecordStore(Protocol):
    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> None: ...


class AuthzPolicyGitHubActionsGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    workflow_refs: tuple[str, ...] = ()
    job_workflow_refs: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyGitHubActionsGrant":
        self.repository = self.repository.strip()
        if not self.repository:
            raise ValueError("Authz policy grant requires repository.")
        self.workflow_refs = self._normalized_tuple(self.workflow_refs)
        self.job_workflow_refs = self._normalized_tuple(self.job_workflow_refs)
        self.event_names = self._normalized_tuple(self.event_names)
        self.refs = self._normalized_tuple(self.refs)
        self.environments = self._normalized_tuple(self.environments)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.actions = self._normalized_tuple(self.actions)
        if not self.actions:
            raise ValueError("Authz policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-policy-grant"
        return self

    def to_policy_rule(self) -> GitHubActionsPolicyRule:
        return GitHubActionsPolicyRule(
            repository=self.repository,
            workflow_refs=self.workflow_refs,
            job_workflow_refs=self.job_workflow_refs,
            event_names=self.event_names,
            refs=self.refs,
            environments=self.environments,
            products=self.products,
            contexts=self.contexts,
            actions=self.actions,
        )


class AuthzPolicyGitHubActionsGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyGitHubActionsGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyGitHubActionsGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz policy grant writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz policy grant apply requires reason.")
        return self


class AuthzPolicyGitHubHumanGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logins: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[Literal["read_only", "admin"], ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-human-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyGitHubHumanGrant":
        self.logins = self._normalized_tuple(self.logins)
        self.organizations = self._normalized_tuple(self.organizations)
        self.teams = self._normalized_tuple(self.teams)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.actions = self._normalized_tuple(self.actions)
        if not (self.logins or self.organizations or self.teams):
            raise ValueError("Authz human policy grant requires a login, organization, or team.")
        if not self.actions:
            raise ValueError("Authz human policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-human-policy-grant"
        return self

    def to_policy_rule(self) -> GitHubHumanPolicyRule:
        return GitHubHumanPolicyRule(
            logins=self.logins,
            organizations=self.organizations,
            teams=self.teams,
            roles=self.roles,
            products=self.products,
            contexts=self.contexts,
            actions=self.actions,
        )


class AuthzPolicyGitHubHumanGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyGitHubHumanGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyGitHubHumanGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz human policy grant writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz human policy grant apply requires reason.")
        return self


AuthzPolicyGrant = AuthzPolicyGitHubActionsGrant | AuthzPolicyGitHubHumanGrant
AuthzPolicyGrantEnvelope = (
    AuthzPolicyGitHubActionsGrantEnvelope | AuthzPolicyGitHubHumanGrantEnvelope
)


def summarize_authz_policy_record(record: LaunchplaneAuthzPolicyRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "status": record.status,
        "source": record.source,
        "updated_at": record.updated_at,
        "policy_sha256": record.policy_sha256,
        "github_actions_rule_count": len(record.policy.github_actions),
        "github_humans_rule_count": len(record.policy.github_humans),
    }


def authz_policy_operator_payload(identity: LaunchplaneIdentity) -> dict[str, object]:
    if isinstance(identity, GitHubHumanIdentity):
        return {
            "type": "github_human",
            "login": identity.login,
            "role": identity.role,
        }
    return {
        "type": "github_actions",
        "repository": identity.repository,
        "workflow_ref": identity.workflow_ref,
        "event_name": identity.event_name,
        "ref": identity.ref,
        "sha": identity.sha,
    }


def authz_policy_grant_diff(
    *, current_policy: LaunchplaneAuthzPolicy, grant: AuthzPolicyGrant
) -> dict[str, object]:
    desired_rule = grant.to_policy_rule()
    if isinstance(grant, AuthzPolicyGitHubHumanGrant):
        changed = not any(rule == desired_rule for rule in current_policy.github_humans)
    else:
        changed = not any(rule == desired_rule for rule in current_policy.github_actions)
    return {
        "changed": changed,
        "previous_github_actions_rule_count": len(current_policy.github_actions),
        "new_github_actions_rule_count": len(current_policy.github_actions)
        + int(changed and isinstance(grant, AuthzPolicyGitHubActionsGrant)),
        "previous_github_humans_rule_count": len(current_policy.github_humans),
        "new_github_humans_rule_count": len(current_policy.github_humans)
        + int(changed and isinstance(grant, AuthzPolicyGitHubHumanGrant)),
    }


def authz_policy_grant_audit_payload(
    *,
    request: AuthzPolicyGrantEnvelope,
    identity: LaunchplaneIdentity,
    previous_record: LaunchplaneAuthzPolicyRecord,
    new_record: LaunchplaneAuthzPolicyRecord | None,
    changed: bool,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> dict[str, object]:
    return {
        "mode": request.mode,
        "reason": request.reason,
        "related_issue": request.related_issue,
        "operator": authz_policy_operator_payload(identity),
        "requested_grant": request.grant.to_policy_rule().model_dump(mode="json"),
        "previous_policy_record_id": previous_record.record_id,
        "previous_policy_sha256": previous_record.policy_sha256,
        "new_policy_record_id": new_record.record_id
        if new_record is not None
        else previous_record.record_id,
        "new_policy_sha256": new_record.policy_sha256
        if new_record is not None
        else previous_record.policy_sha256,
        "changed": changed,
        "trace_id": trace_id,
        "updated_at": new_record.updated_at if new_record is not None else now_timestamp(),
    }


def authz_policy_grant_response_audit_payload(
    audit: dict[str, object],
) -> dict[str, object]:
    response_audit = dict(audit)
    requested_grant = response_audit.pop("requested_grant", None)
    if isinstance(requested_grant, dict):
        if "repository" in requested_grant:
            response_audit["requested_grant_summary"] = {
                "principal_type": "github_actions",
                "repository": requested_grant.get("repository", ""),
                "workflow_ref_count": len(requested_grant.get("workflow_refs") or ()),
                "job_workflow_ref_count": len(requested_grant.get("job_workflow_refs") or ()),
                "event_names": requested_grant.get("event_names") or (),
                "products": requested_grant.get("products") or (),
                "contexts": requested_grant.get("contexts") or (),
                "actions": requested_grant.get("actions") or (),
            }
        else:
            response_audit["requested_grant_summary"] = {
                "principal_type": "github_human",
                "login_count": len(requested_grant.get("logins") or ()),
                "organization_count": len(requested_grant.get("organizations") or ()),
                "team_count": len(requested_grant.get("teams") or ()),
                "roles": requested_grant.get("roles") or (),
                "products": requested_grant.get("products") or (),
                "contexts": requested_grant.get("contexts") or (),
                "actions": requested_grant.get("actions") or (),
            }
    return response_audit


def plan_github_actions_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyGitHubActionsGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def plan_github_human_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyGitHubHumanGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def write_github_actions_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyGitHubActionsGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_github_actions_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"github_actions": current_policy.github_actions + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            updated_at=updated_at,
            policy_sha256=policy_sha256,
        ),
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    record_store.write_authz_policy_record(record)
    return updated_policy, record, changed, diff, record.audit


def write_github_human_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyGitHubHumanGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_github_human_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"github_humans": current_policy.github_humans + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            updated_at=updated_at,
            policy_sha256=policy_sha256,
        ),
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    record_store.write_authz_policy_record(record)
    return updated_policy, record, changed, diff, record.audit


def build_authz_policy_grant_service_result(
    *,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    changed: bool,
    mode: Literal["dry_run", "apply"],
    diff: dict[str, object],
    audit: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    result: dict[str, object] = {
        "authz_policy_record_id": authz_policy_record.record_id,
        "authz_policy_changed": str(changed).lower(),
    }
    driver_result = {
        "authz_policy": summarize_authz_policy_record(authz_policy_record),
        "changed": changed,
        "mode": mode,
        "diff": diff,
        "audit": authz_policy_grant_response_audit_payload(audit),
    }
    return result, driver_result
