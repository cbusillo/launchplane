from __future__ import annotations

from fnmatch import fnmatchcase

from control_plane.authz_grant_service import AuthzPolicyRule, _authz_rule_sha256

from control_plane.contracts.authz_access_read import (
    AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT,
    AuthzPolicyRecordSummary,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.authz_retirement_evidence import (
    AUTHZ_RETIREMENT_MAX_RULES,
    AuthzManagedSetEvidence,
    AuthzRetirementEvidenceRequest,
    AuthzRetirementEvidenceResponse,
    AuthzRetirementHistoryCoverage,
    AuthzSetMaintenanceObservation,
    AuthzUnmanagedWorkflowEvidence,
    AuthzWorkflowEvidenceCandidate,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


def _rules(policy: LaunchplaneAuthzPolicy) -> tuple[AuthzPolicyRule, ...]:
    return (
        *policy.github_actions,
        *policy.github_humans,
        *policy.terminal_agents,
        *policy.local_operators,
        *policy.local_admins,
        *policy.ordinary_agents,
    )


def _set_rules(record: LaunchplaneAuthzPolicyRecord, set_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            _authz_rule_sha256(rule)
            for rule in _rules(record.policy)
            if rule.managed_set_id == set_id
        )
    )


def _maintenance(
    current: LaunchplaneAuthzPolicyRecord, previous: LaunchplaneAuthzPolicyRecord, set_id: str
) -> AuthzSetMaintenanceObservation | None:
    audit = current.audit
    if (
        current.revision != previous.revision + 1
        or audit.get("operation") != "managed_rule_set_reconcile"
        or audit.get("mode") != "apply"
        or audit.get("changed") is not True
        or audit.get("managed_set_id") != set_id
        or audit.get("source") != current.source
        or audit.get("previous_policy_record_id") != previous.record_id
        or type(audit.get("previous_revision")) is not int
        or audit.get("previous_revision") != previous.revision
        or audit.get("previous_policy_sha256") != previous.policy_sha256
        or audit.get("new_policy_record_id") != current.record_id
        or type(audit.get("new_revision")) is not int
        or audit.get("new_revision") != current.revision
        or audit.get("new_policy_sha256") != current.policy_sha256
    ):
        return None
    before, after = _set_rules(previous, set_id), _set_rules(current, set_id)
    if before == after:
        return None
    operator = audit.get("operator")
    caller = operator.get("type") if isinstance(operator, dict) else None
    # The audit records the caller channel, never the origin of its input.
    observation = {
        "revision": current.revision,
        "policy_sha256": current.policy_sha256,
        "caller_type": caller
        if isinstance(caller, str)
        and caller
        in {"github_actions", "github_human", "terminal_agent", "local_operator", "local_admin"}
        else "unknown",
        "introduced": not before and bool(after),
    }
    return AuthzSetMaintenanceObservation.model_validate(observation)


def _unmanaged_workflow(
    policy: LaunchplaneAuthzPolicy, candidate: AuthzWorkflowEvidenceCandidate, index: int
) -> AuthzUnmanagedWorkflowEvidence:
    matched: list[str] = []
    incomplete = False
    for rule in policy.github_actions:
        if rule.managed_set_id or rule.repository != candidate.repository:
            continue
        selectors = (
            rule.workflow_refs if candidate.binding == "workflow_ref" else rule.job_workflow_refs
        )
        if not selectors:
            matched.append(_authz_rule_sha256(rule))
            continue
        for selector in selectors:
            path, separator, ref = selector.partition("@")
            if not separator or not ref or "@" in ref:
                incomplete = True
            elif fnmatchcase(candidate.workflow_path, path):
                matched.append(_authz_rule_sha256(rule))
                break
            elif any(character in path for character in "*?[]"):
                # A glob can consume the @ separator and part of a git ref.
                # A path-only mismatch cannot prove the full selector disjoint.
                incomplete = True
    return AuthzUnmanagedWorkflowEvidence(
        index=index,
        state="unknown"
        if incomplete
        else "potential_dependency"
        if matched
        else "no_matching_rule",
        rule_sha256s=tuple(sorted(set(matched))),
        reason_codes=("workflow_selector_not_classifiable",) if incomplete else (),
    )


def build_retirement_evidence(
    *,
    active: LaunchplaneAuthzPolicyRecord,
    records: tuple[LaunchplaneAuthzPolicyRecord, ...],
    request: AuthzRetirementEvidenceRequest,
    trace_id: str,
) -> AuthzRetirementEvidenceResponse:
    bounded = records[:AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT]
    gaps: list[str] = []
    if len(records) > len(bounded):
        gaps.append("history_truncated")
    if not bounded or bounded[0] != active:
        gaps.append("history_active_record_mismatch")
    revisions = tuple(record.revision for record in bounded)
    if any(left != right + 1 for left, right in zip(revisions, revisions[1:])):
        gaps.append("history_revision_gap_or_ambiguity")
    if not bounded or bounded[-1].revision != 1:
        gaps.append("history_baseline_missing")
    policy_complete = len(_rules(active.policy)) <= AUTHZ_RETIREMENT_MAX_RULES
    history_rules_complete = all(
        len(_rules(record.policy)) <= AUTHZ_RETIREMENT_MAX_RULES for record in bounded
    )
    if not history_rules_complete:
        gaps.append("history_rule_limit_exceeded")
    managed: list[AuthzManagedSetEvidence] = []
    for index, set_id in enumerate(request.managed_set_ids):
        count = (
            sum(rule.managed_set_id == set_id for rule in _rules(active.policy))
            if policy_complete
            else None
        )
        observations: list[AuthzSetMaintenanceObservation] = []
        unbound_change = False
        if history_rules_complete and not any(
            gap in gaps
            for gap in ("history_active_record_mismatch", "history_revision_gap_or_ambiguity")
        ):
            for current, previous in zip(bounded, bounded[1:]):
                observation = _maintenance(current, previous, set_id)
                if observation is not None:
                    observations.append(observation)
                elif _set_rules(previous, set_id) != _set_rules(current, set_id):
                    unbound_change = True
        baseline_present = bool(
            bounded
            and bounded[-1].revision == 1
            and history_rules_complete
            and _set_rules(bounded[-1], set_id)
        )
        managed.append(
            AuthzManagedSetEvidence(
                index=index,
                presence="unknown" if count is None else "present" if count else "absent",
                rule_count=count,
                reason_codes=(
                    "import_origin_not_attested",
                    "maintenance_independence_not_attested",
                    *(
                        ("maintenance_audit_missing_or_unbound",)
                        if unbound_change or not observations
                        else ()
                    ),
                    *(("set_present_at_history_baseline",) if baseline_present else ()),
                    *gaps,
                    *(("active_policy_rule_limit_exceeded",) if count is None else ()),
                ),
                observations=tuple(observations),
            )
        )
    workflows = tuple(
        _unmanaged_workflow(active.policy, candidate, index)
        if policy_complete
        else AuthzUnmanagedWorkflowEvidence(
            index=index, state="unknown", reason_codes=("active_policy_rule_limit_exceeded",)
        )
        for index, candidate in enumerate(request.workflows)
    )
    return AuthzRetirementEvidenceResponse(
        trace_id=trace_id,
        policy=AuthzPolicyRecordSummary(
            record_id=active.record_id,
            revision=active.revision,
            policy_sha256=active.policy_sha256,
            updated_at=active.updated_at,
            schema_version=active.policy.schema_version,
        ),
        history=AuthzRetirementHistoryCoverage(
            returned_count=len(bounded),
            oldest_revision=bounded[-1].revision if bounded else None,
            state="partial" if gaps else "complete",
            reason_codes=tuple(gaps),
        ),
        managed_sets=tuple(managed),
        unmanaged_workflows=workflows,
    )
