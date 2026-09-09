from __future__ import annotations

import unittest
from typing import Literal, TypedDict

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentAction,
    PrincipalProfile,
    PrincipalStatus,
    OrdinaryAgentBudget,
    OrdinaryAgentCredentialEvidence,
    OrdinaryAgentEligibilityResult,
    OrdinaryAgentLease,
    OrdinaryAgentPolicyEvaluation,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
    OrdinaryAgentPullRequest,
    OrdinaryAgentRequest,
    OrdinaryAgentSession,
    OrdinaryAgentTarget,
)
from control_plane.ordinary_agent_eligibility import (
    evaluate_ordinary_agent_eligibility,
    evaluate_ordinary_agent_policy,
)


class InertFields(TypedDict):
    record_kind: Literal["proposed_ordinary_agent_v1"]
    authority_state: Literal["inert"]
    authorizes_execution: Literal[False]


INERT: InertFields = {
    "record_kind": "proposed_ordinary_agent_v1",
    "authority_state": "inert",
    "authorizes_execution": False,
}
DIGEST = "a" * 64
HEAD = "b" * 40
BASE = "c" * 40
NOW = 100


def make_target(*, repository_id: int = 101) -> OrdinaryAgentTarget:
    return OrdinaryAgentTarget(
        repository_id=repository_id, repository="example/project", base_branch="main"
    )


def make_principal(
    *, profile: PrincipalProfile = "guarded_executor", status: PrincipalStatus = "active"
) -> OrdinaryAgentPrincipal:
    return OrdinaryAgentPrincipal(
        **INERT,
        record_id="principal-record",
        principal_id="agent_one",
        execution_profile=profile,
        status=status,
    )


def make_rule(
    *,
    managed_rule_id: str = "guarded.merge",
    rule_target: OrdinaryAgentTarget | None = None,
    actions: tuple[OrdinaryAgentAction, ...] = ("guarded_merge",),
) -> OrdinaryAgentPolicyRule:
    return OrdinaryAgentPolicyRule(
        managed_set_id="ordinary.agents",
        managed_rule_id=managed_rule_id,
        principal_id="agent_one",
        target=rule_target or make_target(),
        actions=actions,
    )


def make_snapshot(
    *, revision: int = 1, rules: tuple[OrdinaryAgentPolicyRule, ...] | None = None
) -> OrdinaryAgentPolicySnapshot:
    return OrdinaryAgentPolicySnapshot(
        **INERT,
        record_id=f"policy-{revision}",
        revision=revision,
        policy_digest=f"{revision:064x}",
        input_domain_id="ordinary-agent-effective-inputs-v1",
        evaluator_semantics_version="ordinary-agent-eligibility-v1",
        rules=rules if rules is not None else (make_rule(),),
    )


def make_credential(**changes: object) -> OrdinaryAgentCredentialEvidence:
    values: dict[str, object] = {
        **INERT,
        "record_id": "credential-record",
        "credential_id": "credential_one",
        "credential_version": 1,
        "credential_digest": DIGEST,
        "principal_id": "agent_one",
        "valid_from": 10,
        "expires_at": 200,
        "revoked_at": None,
    }
    values.update(changes)
    return OrdinaryAgentCredentialEvidence.model_validate(values)


def make_session(**changes: object) -> OrdinaryAgentSession:
    values: dict[str, object] = {
        **INERT,
        "record_id": "session-record",
        "session_id": "session_one",
        "principal_id": "agent_one",
        "credential_id": "credential_one",
        "credential_version": 1,
        "credential_digest": DIGEST,
        "valid_from": 20,
        "expires_at": 180,
        "revoked_at": None,
    }
    values.update(changes)
    return OrdinaryAgentSession.model_validate(values)


def make_request(
    *, action: OrdinaryAgentAction = "guarded_merge", pull_request_count: int = 1, **changes: object
) -> OrdinaryAgentRequest:
    pull_requests = tuple(
        OrdinaryAgentPullRequest(number=index + 1, head_sha=f"{index + 1:040x}")
        for index in range(pull_request_count)
    )
    values: dict[str, object] = {
        **INERT,
        "record_id": "request-record",
        "request_id": "request_one",
        "idempotency_key": "request-key",
        "lease_id": "lease_one",
        "session_id": "session_one",
        "principal_id": "agent_one",
        "target": make_target(),
        "base_sha": BASE,
        "action": action,
        "pull_requests": pull_requests,
        "permitted_stack_edit_pull_requests": (1,) if action == "guarded_merge" else (),
    }
    values.update(changes)
    return OrdinaryAgentRequest.model_validate(values)


def policy_evaluation(
    *,
    policy: OrdinaryAgentPolicySnapshot | None = None,
    actor: OrdinaryAgentPrincipal | None = None,
    action: OrdinaryAgentAction = "guarded_merge",
) -> OrdinaryAgentPolicyEvaluation:
    return evaluate_ordinary_agent_policy(
        snapshot=policy or make_snapshot(),
        principal=actor or make_principal(),
        target=make_target(),
        action=action,
        managed_set_id="ordinary.agents",
        managed_rule_id="guarded.merge",
    )


def make_lease(
    *,
    fingerprint: str | None = None,
    action: OrdinaryAgentAction = "guarded_merge",
    **changes: object,
) -> OrdinaryAgentLease:
    values: dict[str, object] = {
        **INERT,
        "record_id": "lease-record",
        "lease_id": "lease_one",
        "session_id": "session_one",
        "principal_id": "agent_one",
        "target": make_target(),
        "managed_set_id": "ordinary.agents",
        "managed_rule_id": "guarded.merge",
        "action": action,
        "effective_decision_fingerprint": fingerprint
        or policy_evaluation(action=action).effective_decision_fingerprint,
        "valid_from": 30,
        "expires_at": 170,
        "revoked_at": None,
        "budget": OrdinaryAgentBudget(
            window_start=30,
            window_end=170,
            action_limit=2,
            actions_used=0,
            pull_request_limit=3,
            pull_requests_used=0,
        ),
    }
    values.update(changes)
    return OrdinaryAgentLease.model_validate(values)


def evaluate(
    *,
    result_record_id: str = "result-record",
    now: int = NOW,
    snapshot: OrdinaryAgentPolicySnapshot | None = None,
    principal: OrdinaryAgentPrincipal | None = None,
    credential: OrdinaryAgentCredentialEvidence | None = None,
    session: OrdinaryAgentSession | None = None,
    lease: OrdinaryAgentLease | None = None,
    request: OrdinaryAgentRequest | None = None,
) -> OrdinaryAgentEligibilityResult:
    return evaluate_ordinary_agent_eligibility(
        result_record_id=result_record_id,
        now=now,
        snapshot=snapshot or make_snapshot(),
        principal=principal or make_principal(),
        credential=credential or make_credential(),
        session=session or make_session(),
        lease=lease or make_lease(),
        request=request or make_request(),
    )


class OrdinaryAgentPolicyEvaluationTests(unittest.TestCase):
    def test_policy_record_identity_uses_unambiguous_tuple_binding(self) -> None:
        first = OrdinaryAgentPolicyRule.model_validate(
            {**make_rule().model_dump(), "managed_set_id": "team:a", "managed_rule_id": "b"}
        )
        second = OrdinaryAgentPolicyRule.model_validate(
            {**make_rule().model_dump(), "managed_set_id": "team", "managed_rule_id": "a:b"}
        )
        policy = make_snapshot(rules=(first, second))
        records = [
            evaluate_ordinary_agent_policy(
                snapshot=policy,
                principal=make_principal(),
                target=make_target(),
                action="guarded_merge",
                managed_set_id=item.managed_set_id,
                managed_rule_id=item.managed_rule_id,
            )
            for item in (first, second)
        ]
        self.assertNotEqual(records[0].record_id, records[1].record_id)
        self.assertTrue(all(item.decision == "allow" for item in records))

    def test_fingerprint_ignores_record_metadata_and_action_order(self) -> None:
        first = policy_evaluation(
            policy=make_snapshot(rules=(make_rule(actions=("guarded_merge", "preflight")),))
        )
        actor = OrdinaryAgentPrincipal.model_validate(
            {**make_principal().model_dump(), "record_id": "new-observation"}
        )
        reordered = policy_evaluation(
            actor=actor,
            policy=make_snapshot(
                revision=2, rules=(make_rule(actions=("preflight", "guarded_merge")),)
            ),
        )
        self.assertEqual(
            first.effective_decision_fingerprint, reordered.effective_decision_fingerprint
        )

    def test_exact_policy_allow_is_inert_and_fingerprinted(self) -> None:
        evaluation = policy_evaluation()
        self.assertEqual(evaluation.decision, "allow")
        self.assertEqual(evaluation.reason_code, "policy_allowed")
        self.assertRegex(evaluation.effective_decision_fingerprint, r"^oae-fp-v1:[0-9a-f]{64}$")
        self.assertEqual(evaluation.authority_state, "inert")
        self.assertFalse(evaluation.authorizes_execution)

    def test_missing_wrong_and_duplicate_bound_rules_deny(self) -> None:
        missing = make_snapshot(rules=(make_rule(managed_rule_id="different.rule"),))
        duplicate = make_snapshot(rules=(make_rule(), make_rule()))
        self.assertEqual(policy_evaluation(policy=missing).reason_code, "bound_rule_missing")
        self.assertEqual(policy_evaluation(policy=duplicate).reason_code, "bound_rule_ambiguous")
        wrong_target = make_snapshot(rules=(make_rule(rule_target=make_target(repository_id=202)),))
        self.assertEqual(policy_evaluation(policy=wrong_target).reason_code, "rule_target_mismatch")

    def test_read_only_can_only_receive_closed_read_or_preflight_actions(self) -> None:
        actor = make_principal(profile="read_only")
        merge = policy_evaluation(actor=actor)
        self.assertEqual(merge.reason_code, "principal_read_only")
        read_policy = make_snapshot(rules=(make_rule(actions=("self_read", "preflight")),))
        read = policy_evaluation(policy=read_policy, actor=actor, action="self_read")
        self.assertEqual(read.decision, "allow")

    def test_unrelated_revision_and_rule_preserve_effective_fingerprint(self) -> None:
        first = policy_evaluation()
        changed = make_snapshot(
            revision=2,
            rules=(
                make_rule(),
                make_rule(managed_rule_id="unrelated.read", actions=("self_read",)),
            ),
        )
        second = policy_evaluation(policy=changed)
        self.assertEqual(
            first.effective_decision_fingerprint, second.effective_decision_fingerprint
        )
        bound_changed = make_snapshot(
            revision=3, rules=(make_rule(actions=("guarded_merge", "preflight")),)
        )
        self.assertNotEqual(
            first.effective_decision_fingerprint,
            policy_evaluation(policy=bound_changed).effective_decision_fingerprint,
        )


class OrdinaryAgentEligibilityTests(unittest.TestCase):
    def test_changed_allowing_policy_invalidates_existing_lease_fingerprint(self) -> None:
        changed = make_snapshot(
            revision=2, rules=(make_rule(actions=("guarded_merge", "preflight")),)
        )
        result = evaluate(snapshot=changed, lease=make_lease())
        self.assertEqual(result.reason_code, "effective_decision_fingerprint_mismatch")
        self.assertEqual(result.decision, "denied")

    def test_lease_action_must_still_be_allowed_by_current_policy(self) -> None:
        result = evaluate(lease=make_lease(action="preflight"))
        self.assertEqual(result.reason_code, "action_not_allowed")
        self.assertEqual(result.decision, "denied")

    def test_rate_window_is_checked_independently_of_lease_expiry(self) -> None:
        for start, end in ((101, 160), (30, 100)):
            budget = OrdinaryAgentBudget(
                window_start=start,
                window_end=end,
                action_limit=2,
                actions_used=0,
                pull_request_limit=3,
                pull_requests_used=0,
            )
            self.assertEqual(
                evaluate(lease=make_lease(budget=budget)).reason_code, "budget_window_inactive"
            )

    def test_session_and_lease_expiry_and_revocation_are_independent(self) -> None:
        cases = (
            (
                "session_expired",
                evaluate(session=make_session(expires_at=100), lease=make_lease(expires_at=100)),
            ),
            ("lease_expired", evaluate(lease=make_lease(expires_at=100))),
            ("session_revoked", evaluate(session=make_session(revoked_at=100))),
            ("lease_revoked", evaluate(lease=make_lease(revoked_at=100))),
        )
        for reason, result in cases:
            with self.subTest(reason=reason):
                self.assertEqual(result.reason_code, reason)
        self.assertEqual(
            evaluate(
                session=make_session(revoked_at=101), lease=make_lease(revoked_at=101)
            ).decision,
            "eligible",
        )

    def test_exact_chain_is_eligible_but_never_authorizes_execution(self) -> None:
        result = evaluate()
        self.assertEqual(result.decision, "eligible")
        self.assertEqual(result.reason_code, "eligible")
        self.assertFalse(result.authorizes_execution)
        self.assertEqual(
            result.request_digest,
            canonical_json_sha256(make_request().model_dump(mode="json")),
        )

    def test_stable_refusal_precedence_starts_with_principal_then_target(self) -> None:
        result = evaluate(
            principal=make_principal(status="revoked"),
            lease=make_lease(target=make_target(repository_id=202)),
        )
        self.assertEqual(result.reason_code, "principal_revoked")
        self.assertEqual(
            evaluate(lease=make_lease(target=make_target(repository_id=202))).reason_code,
            "lease_target_mismatch",
        )

    def test_chain_mismatch_rotation_and_containment_deny(self) -> None:
        cases = (
            (
                "credential_principal_mismatch",
                evaluate(credential=make_credential(principal_id="agent_two")),
            ),
            (
                "session_principal_mismatch",
                evaluate(session=make_session(principal_id="agent_two")),
            ),
            (
                "credential_version_rotated",
                evaluate(credential=make_credential(credential_version=2)),
            ),
            (
                "credential_digest_rotated",
                evaluate(credential=make_credential(credential_digest="d" * 64)),
            ),
            ("lease_session_mismatch", evaluate(lease=make_lease(session_id="session_two"))),
            ("session_outside_credential_lifetime", evaluate(session=make_session(valid_from=5))),
            ("lease_outside_session_lifetime", evaluate(lease=make_lease(expires_at=190))),
        )
        for reason, result in cases:
            with self.subTest(reason=reason):
                self.assertEqual(result.reason_code, reason)

    def test_half_open_expiry_and_scheduled_revocation(self) -> None:
        self.assertEqual(evaluate(now=200).reason_code, "credential_expired")
        self.assertEqual(
            evaluate(credential=make_credential(revoked_at=NOW)).reason_code, "credential_revoked"
        )
        self.assertEqual(
            evaluate(credential=make_credential(revoked_at=NOW + 1)).decision, "eligible"
        )

    def test_policy_is_always_reevaluated_before_fingerprint_comparison(self) -> None:
        denied_policy = make_snapshot(rules=())
        result = evaluate(
            snapshot=denied_policy, lease=make_lease(fingerprint=f"oae-fp-v1:{'f' * 64}")
        )
        self.assertEqual(result.reason_code, "bound_rule_missing")

    def test_lease_action_request_action_and_fingerprint_are_exact(self) -> None:
        self.assertEqual(
            evaluate(request=make_request(action="preflight", pull_request_count=0)).reason_code,
            "request_action_mismatch",
        )
        changed_head = make_request(
            pull_requests=(OrdinaryAgentPullRequest(number=1, head_sha="d" * 40),),
            permitted_stack_edit_pull_requests=(1,),
        )
        self.assertNotEqual(
            evaluate(request=changed_head).request_digest, evaluate().request_digest
        )

    def test_budget_window_and_requested_cost_deny_without_decrementing(self) -> None:
        original = make_lease()
        over_pr_budget = original.model_copy(
            update={
                "budget": OrdinaryAgentBudget(
                    window_start=30,
                    window_end=170,
                    action_limit=1,
                    actions_used=0,
                    pull_request_limit=1,
                    pull_requests_used=0,
                )
            }
        )
        self.assertEqual(
            evaluate(lease=over_pr_budget, request=make_request(pull_request_count=2)).reason_code,
            "budget_exhausted",
        )
        self.assertEqual(evaluate(lease=make_lease(), now=29).reason_code, "lease_not_yet_valid")
        self.assertEqual(original.budget.actions_used, 0)
