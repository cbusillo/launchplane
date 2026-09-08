"""Exact, DB-policy-derived predicates for the feedback continuation contract.

Defining these predicates creates no grant. Callers must supply the single active
policy under the policy serialization boundary when committing execution.
"""

from __future__ import annotations

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.every_code_feedback_resume import (
    EVERY_CODE_FEEDBACK_RESUME_EXECUTE_ACTION,
    EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION,
    EveryCodeFeedbackPolicyDecisionProvenance,
    FeedbackPolicyAction,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    ScopedAuthzPolicyRule,
    TerminalAgentIdentity,
)


def _exact_scope(rule: ScopedAuthzPolicyRule, *, action: str, instance: str) -> bool:
    return (
        rule.managed_set_id is not None
        and rule.managed_rule_id is not None
        and rule.products == ("launchplane",)
        and rule.contexts == ("launchplane",)
        and rule.actions == (action,)
        and rule.instances == (instance,)
    )


def _literal_identity(value: str) -> bool:
    return (
        0 < len(value) <= 256
        and value == value.strip()
        and value.isprintable()
        and not any(c in value for c in "*?[]")
    )


def _provenance(
    policy_record: LaunchplaneAuthzPolicyRecord,
    rule: ScopedAuthzPolicyRule,
    *,
    action: FeedbackPolicyAction,
    instance: str,
) -> EveryCodeFeedbackPolicyDecisionProvenance:
    assert rule.managed_set_id is not None and rule.managed_rule_id is not None
    return EveryCodeFeedbackPolicyDecisionProvenance(
        action=action,
        instance=instance,
        managed_set_id=rule.managed_set_id,
        managed_rule_id=rule.managed_rule_id,
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
    )


def resolve_every_code_feedback_resume_actor(
    *,
    policy_record: LaunchplaneAuthzPolicyRecord,
    github_id: int,
    repository_id: int,
) -> EveryCodeFeedbackPolicyDecisionProvenance | None:
    """Resolve an immutable GitHub author using exactly one ID-only managed rule.

    A webhook supplies no authenticated browser role. Scan the active policy
    structurally; never fabricate a GitHubHumanIdentity for the generic evaluator.
    """
    if (
        type(github_id) is not int
        or github_id < 1
        or type(repository_id) is not int
        or repository_id < 1
        or policy_record.status != "active"
        or policy_record.policy.schema_version != 2
    ):
        return None
    action: FeedbackPolicyAction = EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION
    instance = f"github-repository:{repository_id}"
    rules = tuple(
        rule
        for rule in policy_record.policy.github_humans
        if _exact_scope(rule, action=action, instance=instance)
        and rule.github_ids == (github_id,)
        and not (rule.logins or rule.organizations or rule.teams or rule.roles)
    )
    if len(rules) != 1:
        return None
    return _provenance(policy_record, rules[0], action=action, instance=instance)


def resolve_every_code_feedback_resume_worker(
    *,
    policy_record: LaunchplaneAuthzPolicyRecord,
    identity: LaunchplaneIdentity,
    repository_id: int,
) -> EveryCodeFeedbackPolicyDecisionProvenance | None:
    """Resolve the authenticated shared worker subject and its exact token label."""
    if (
        not isinstance(identity, TerminalAgentIdentity)
        or not _literal_identity(identity.subject)
        or not _literal_identity(identity.token_label)
        or type(repository_id) is not int
        or repository_id < 1
        or policy_record.status != "active"
        or policy_record.policy.schema_version != 2
    ):
        return None
    action: FeedbackPolicyAction = EVERY_CODE_FEEDBACK_RESUME_EXECUTE_ACTION
    instance = f"github-repository:{repository_id}"
    rules = tuple(
        rule
        for rule in policy_record.policy.terminal_agents
        if _exact_scope(rule, action=action, instance=instance)
        and rule.subjects == (identity.subject,)
        and rule.token_labels == (identity.token_label,)
    )
    if len(rules) != 1:
        return None
    decision = policy_record.policy.evaluate(
        identity=identity,
        action=action,
        product="launchplane",
        context="launchplane",
        target=AuthorizationTarget(scope="instance", instances=(instance,)),
        record_context=False,
    )
    if decision.decision != "allowed":
        return None
    return _provenance(policy_record, rules[0], action=action, instance=instance)
