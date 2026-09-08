from __future__ import annotations

import unittest
from typing import Literal, TypedDict, cast

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
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


def target(*, repository_id: int = 101) -> OrdinaryAgentTarget:
    return OrdinaryAgentTarget(
        repository_id=repository_id, repository="example/project", base_branch="main"
    )


def principal(
    *, profile: str = "guarded_executor", status: str = "active"
) -> OrdinaryAgentPrincipal:
    return OrdinaryAgentPrincipal(
        **INERT,
        record_id="principal-record",
        principal_id="agent_one",
        execution_profile=cast(Literal["read_only", "guarded_executor"], profile),
        status=cast(Literal["active", "revoked"], status),
    )


def rule(
    *,
    managed_rule_id: str = "guarded.merge",
    rule_target: OrdinaryAgentTarget | None = None,
    actions: tuple[str, ...] = ("guarded_merge",),
) -> OrdinaryAgentPolicyRule:
    return OrdinaryAgentPolicyRule(
        managed_set_id="ordinary.agents",
        managed_rule_id=managed_rule_id,
        principal_id="agent_one",
        target=rule_target or target(),
        actions=cast(tuple[Literal["self_read", "preflight", "guarded_merge"], ...], actions),
    )


def snapshot(
    *, revision: int = 1, rules: tuple[OrdinaryAgentPolicyRule, ...] | None = None
) -> OrdinaryAgentPolicySnapshot:
    return OrdinaryAgentPolicySnapshot(
        **INERT,
        record_id=f"policy-{revision}",
        revision=revision,
        policy_digest=f"{revision:064x}",
        input_domain_id="ordinary-agent-effective-inputs-v1",
        evaluator_semantics_version="ordinary-agent-eligibility-v1",
        rules=rules if rules is not None else (rule(),),
    )


def credential(**changes: object) -> OrdinaryAgentCredentialEvidence:
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


def session(**changes: object) -> OrdinaryAgentSession:
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


def request(
    *, action: str = "guarded_merge", pull_request_count: int = 1, **changes: object
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
        "target": target(),
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
    action: str = "guarded_merge",
) -> OrdinaryAgentPolicyEvaluation:
    return evaluate_ordinary_agent_policy(
        snapshot=policy or snapshot(),
        principal=actor or principal(),
        target=target(),
        action=cast(Literal["self_read", "preflight", "guarded_merge"], action),
        managed_set_id="ordinary.agents",
        managed_rule_id="guarded.merge",
    )


def lease(
    *, fingerprint: str | None = None, action: str = "guarded_merge", **changes: object
) -> OrdinaryAgentLease:
    values: dict[str, object] = {
        **INERT,
        "record_id": "lease-record",
        "lease_id": "lease_one",
        "session_id": "session_one",
        "principal_id": "agent_one",
        "target": target(),
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
    policy_snapshot: OrdinaryAgentPolicySnapshot | None = None,
    actor: OrdinaryAgentPrincipal | None = None,
    credential_evidence: OrdinaryAgentCredentialEvidence | None = None,
    agent_session: OrdinaryAgentSession | None = None,
    agent_lease: OrdinaryAgentLease | None = None,
    agent_request: OrdinaryAgentRequest | None = None,
    **aliases: object,
) -> OrdinaryAgentEligibilityResult:
    return evaluate_ordinary_agent_eligibility(
        result_record_id=result_record_id,
        now=now,
        snapshot=cast(OrdinaryAgentPolicySnapshot, aliases.get("snapshot", policy_snapshot))
        if aliases.get("snapshot", policy_snapshot) is not None
        else snapshot(),
        principal=cast(OrdinaryAgentPrincipal, aliases.get("principal", actor))
        if aliases.get("principal", actor) is not None
        else principal(),
        credential=cast(
            OrdinaryAgentCredentialEvidence, aliases.get("credential", credential_evidence)
        )
        if aliases.get("credential", credential_evidence) is not None
        else credential(),
        session=cast(OrdinaryAgentSession, aliases.get("session", agent_session))
        if aliases.get("session", agent_session) is not None
        else session(),
        lease=cast(OrdinaryAgentLease, aliases.get("lease", agent_lease))
        if aliases.get("lease", agent_lease) is not None
        else lease(),
        request=cast(OrdinaryAgentRequest, aliases.get("request", agent_request))
        if aliases.get("request", agent_request) is not None
        else request(),
    )


class OrdinaryAgentPolicyEvaluationTests(unittest.TestCase):
    def test_policy_record_identity_uses_unambiguous_tuple_binding(self) -> None:
        first = OrdinaryAgentPolicyRule.model_validate(
            {**rule().model_dump(), "managed_set_id": "team:a", "managed_rule_id": "b"}
        )
        second = OrdinaryAgentPolicyRule.model_validate(
            {**rule().model_dump(), "managed_set_id": "team", "managed_rule_id": "a:b"}
        )
        policy = snapshot(rules=(first, second))
        records = [
            evaluate_ordinary_agent_policy(
                snapshot=policy,
                principal=principal(),
                target=target(),
                action="guarded_merge",
                managed_set_id=item.managed_set_id,
                managed_rule_id=item.managed_rule_id,
            )
            for item in (first, second)
        ]
        self.assertNotEqual(records[0].record_id, records[1].record_id)
        self.assertTrue(all(item.decision == "allow" for item in records))

    def test_exact_policy_allow_is_inert_and_fingerprinted(self) -> None:
        evaluation = policy_evaluation()
        self.assertEqual(evaluation.decision, "allow")
        self.assertEqual(evaluation.reason_code, "policy_allowed")
        self.assertRegex(evaluation.effective_decision_fingerprint, r"^oae-fp-v1:[0-9a-f]{64}$")
        self.assertEqual(evaluation.authority_state, "inert")
        self.assertFalse(evaluation.authorizes_execution)

    def test_missing_wrong_and_duplicate_bound_rules_deny(self) -> None:
        missing = snapshot(rules=(rule(managed_rule_id="different.rule"),))
        duplicate = snapshot(rules=(rule(), rule()))
        self.assertEqual(policy_evaluation(policy=missing).reason_code, "bound_rule_missing")
        self.assertEqual(policy_evaluation(policy=duplicate).reason_code, "bound_rule_ambiguous")
        wrong_target = snapshot(rules=(rule(rule_target=target(repository_id=202)),))
        self.assertEqual(policy_evaluation(policy=wrong_target).reason_code, "rule_target_mismatch")

    def test_read_only_can_only_receive_closed_read_or_preflight_actions(self) -> None:
        actor = principal(profile="read_only")
        merge = policy_evaluation(actor=actor)
        self.assertEqual(merge.reason_code, "principal_read_only")
        read_policy = snapshot(rules=(rule(actions=("self_read", "preflight")),))
        read = policy_evaluation(policy=read_policy, actor=actor, action="self_read")
        self.assertEqual(read.decision, "allow")

    def test_unrelated_revision_and_rule_preserve_effective_fingerprint(self) -> None:
        first = policy_evaluation()
        changed = snapshot(
            revision=2,
            rules=(rule(), rule(managed_rule_id="unrelated.read", actions=("self_read",))),
        )
        second = policy_evaluation(policy=changed)
        self.assertEqual(
            first.effective_decision_fingerprint, second.effective_decision_fingerprint
        )
        bound_changed = snapshot(revision=3, rules=(rule(actions=("guarded_merge", "preflight")),))
        self.assertNotEqual(
            first.effective_decision_fingerprint,
            policy_evaluation(policy=bound_changed).effective_decision_fingerprint,
        )


class OrdinaryAgentEligibilityTests(unittest.TestCase):
    def test_exact_chain_is_eligible_but_never_authorizes_execution(self) -> None:
        result = evaluate()
        self.assertEqual(result.decision, "eligible")
        self.assertEqual(result.reason_code, "eligible")
        self.assertFalse(result.authorizes_execution)
        self.assertEqual(
            result.request_digest,
            canonical_json_sha256(request().model_dump(mode="json")),
        )

    def test_stable_refusal_precedence_starts_with_principal_then_target(self) -> None:
        result = evaluate(
            principal=principal(status="revoked"),
            lease=lease(target=target(repository_id=202)),
        )
        self.assertEqual(result.reason_code, "principal_revoked")
        self.assertEqual(
            evaluate(lease=lease(target=target(repository_id=202))).reason_code,
            "lease_target_mismatch",
        )

    def test_chain_mismatch_rotation_and_containment_deny(self) -> None:
        cases = (
            ("credential_principal_mismatch", {"credential": credential(principal_id="agent_two")}),
            ("session_principal_mismatch", {"session": session(principal_id="agent_two")}),
            ("credential_version_rotated", {"credential": credential(credential_version=2)}),
            ("credential_digest_rotated", {"credential": credential(credential_digest="d" * 64)}),
            ("lease_session_mismatch", {"lease": lease(session_id="session_two")}),
            ("session_outside_credential_lifetime", {"session": session(valid_from=5)}),
            ("lease_outside_session_lifetime", {"lease": lease(expires_at=190)}),
        )
        for reason, arguments in cases:
            with self.subTest(reason=reason):
                self.assertEqual(evaluate(**arguments).reason_code, reason)

    def test_half_open_expiry_and_scheduled_revocation(self) -> None:
        self.assertEqual(evaluate(now=200).reason_code, "credential_expired")
        self.assertEqual(
            evaluate(credential=credential(revoked_at=NOW)).reason_code, "credential_revoked"
        )
        self.assertEqual(evaluate(credential=credential(revoked_at=NOW + 1)).decision, "eligible")

    def test_policy_is_always_reevaluated_before_fingerprint_comparison(self) -> None:
        denied_policy = snapshot(rules=())
        result = evaluate(snapshot=denied_policy, lease=lease(fingerprint=f"oae-fp-v1:{'f' * 64}"))
        self.assertEqual(result.reason_code, "bound_rule_missing")

    def test_lease_action_request_action_and_fingerprint_are_exact(self) -> None:
        self.assertEqual(
            evaluate(request=request(action="preflight", pull_request_count=0)).reason_code,
            "action_not_allowed",
        )
        changed_head = request(
            pull_requests=(OrdinaryAgentPullRequest(number=1, head_sha="d" * 40),),
            permitted_stack_edit_pull_requests=(1,),
        )
        self.assertNotEqual(
            evaluate(request=changed_head).request_digest, evaluate().request_digest
        )

    def test_budget_window_and_requested_cost_deny_without_decrementing(self) -> None:
        original = lease()
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
            evaluate(lease=over_pr_budget, request=request(pull_request_count=2)).reason_code,
            "budget_exhausted",
        )
        self.assertEqual(evaluate(lease=lease(), now=29).reason_code, "lease_not_yet_valid")
        self.assertEqual(original.budget.actions_used, 0)
