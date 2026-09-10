from __future__ import annotations

import json
from itertools import combinations
from typing import cast
import unittest

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentAction,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
    OrdinaryAgentTarget,
    PrincipalProfile,
)
from control_plane.contracts.ordinary_agent_enrollment import (
    OrdinaryAgentEnrollRequest,
    OrdinaryAgentRevokePrincipalRequest,
    OrdinaryAgentRotateCredentialRequest,
)
from control_plane.ordinary_agent_enrollment import (
    build_ordinary_agent_enrollment_review,
    derive_ordinary_agent_execution_profile,
    dormant_ordinary_agent_enrollment,
    parse_ordinary_agent_enrollment_request,
)
from control_plane.ordinary_agent_eligibility import evaluate_ordinary_agent_policy


_DIGEST = "a" * 64
_TARGET = OrdinaryAgentTarget(
    repository_id=1001,
    repository="example/launchplane",
    base_branch="main",
)
_RULE = OrdinaryAgentPolicyRule(
    managed_set_id="ordinary-agent.pilot",
    managed_rule_id="agent_one.launchplane.main",
    principal_id="agent_one",
    target=_TARGET,
    actions=("self_read", "preflight"),
)


def _policy_payload() -> dict[str, object]:
    return {
        "record_id": "authz-policy-r3",
        "revision": 3,
        "policy_sha256": _DIGEST,
        "managed_set_id": _RULE.managed_set_id,
        "managed_rule_id": _RULE.managed_rule_id,
        "target": _TARGET.model_dump(mode="json"),
    }


def _principal_payload() -> dict[str, object]:
    return {
        "record_id": "ordinary-agent-principal-agent-one",
        "revision": 2,
        "pre_state_sha256": "b" * 64,
    }


class OrdinaryAgentEnrollmentContractTests(unittest.TestCase):
    def test_action_specific_requests_parse_without_credential_material(self) -> None:
        requests = (
            parse_ordinary_agent_enrollment_request(
                json.dumps(
                    {
                        "action": "enroll",
                        "principal_id": "agent_one",
                        "policy": _policy_payload(),
                        "expected_principal_absent": True,
                    }
                )
            ),
            parse_ordinary_agent_enrollment_request(
                json.dumps(
                    {
                        "action": "rotate_credential",
                        "principal_id": "agent_one",
                        "policy": _policy_payload(),
                        "principal": _principal_payload(),
                        "credential_id": "credential-agent-one",
                        "credential_version": 4,
                    }
                )
            ),
            parse_ordinary_agent_enrollment_request(
                json.dumps(
                    {
                        "action": "revoke_principal",
                        "principal_id": "agent_one",
                        "principal": _principal_payload(),
                    }
                )
            ),
        )

        self.assertIsInstance(requests[0], OrdinaryAgentEnrollRequest)
        self.assertIsInstance(requests[1], OrdinaryAgentRotateCredentialRequest)
        self.assertIsInstance(requests[2], OrdinaryAgentRevokePrincipalRequest)

    def test_unknown_or_secret_material_fields_fail_closed(self) -> None:
        for field_name in (
            "credential",
            "token",
            "private_key",
            "replacement_digest",
            "custody_reference",
        ):
            payload = {
                "action": "enroll",
                "principal_id": "agent_one",
                "policy": _policy_payload(),
                "expected_principal_absent": True,
                field_name: "caller-material",
            }
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                parse_ordinary_agent_enrollment_request(json.dumps(payload))

    def test_review_is_exact_deterministic_and_redacted(self) -> None:
        request = cast(
            OrdinaryAgentEnrollRequest,
            parse_ordinary_agent_enrollment_request(
                json.dumps(
                    {
                        "action": "enroll",
                        "principal_id": "agent_one",
                        "policy": _policy_payload(),
                        "expected_principal_absent": True,
                    }
                )
            ),
        )

        first = build_ordinary_agent_enrollment_review(request=request, bound_rule=_RULE)
        second = build_ordinary_agent_enrollment_review(request=request, bound_rule=_RULE)
        payload = first.model_dump_json()

        self.assertEqual(first, second)
        self.assertEqual(first.derived_execution_profile, "read_only")
        self.assertEqual(first.credential_custody, "unavailable")
        for forbidden_field in (
            '"credential"',
            '"credential_digest"',
            '"token"',
            '"private_key"',
            '"replacement_digest"',
            '"custody_reference"',
        ):
            self.assertNotIn(forbidden_field, payload)
        tampered = first.model_dump(mode="json")
        tampered["principal_id"] = "agent_two"
        with self.assertRaisesRegex(ValidationError, "review digest does not match"):
            type(first).model_validate(tampered)
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_ordinary_agent_enrollment_review(
                request=request,
                bound_rule=_RULE.model_copy(update={"principal_id": "agent_two"}),
            )

    def test_revoke_review_needs_no_active_policy_rule(self) -> None:
        request = cast(
            OrdinaryAgentRevokePrincipalRequest,
            parse_ordinary_agent_enrollment_request(
                json.dumps(
                    {
                        "action": "revoke_principal",
                        "principal_id": "agent_one",
                        "principal": _principal_payload(),
                    }
                )
            ),
        )

        review = build_ordinary_agent_enrollment_review(request=request)

        self.assertIsNone(review.policy)
        self.assertEqual(review.credential_custody, "not_applicable")
        self.assertIsNone(review.derived_execution_profile)

    def test_profile_derivation_is_exhaustive_order_independent_and_rule_bounded(self) -> None:
        actions: tuple[OrdinaryAgentAction, ...] = (
            "self_read",
            "preflight",
            "guarded_merge",
        )
        for size in range(1, len(actions) + 1):
            for subset in combinations(actions, size):
                with self.subTest(actions=subset):
                    expected: PrincipalProfile = (
                        "guarded_executor" if "guarded_merge" in subset else "read_only"
                    )
                    self.assertEqual(derive_ordinary_agent_execution_profile(subset), expected)
                    self.assertEqual(
                        derive_ordinary_agent_execution_profile(tuple(reversed(subset))), expected
                    )
                    self.assertEqual(
                        derive_ordinary_agent_execution_profile((*subset, subset[0])), expected
                    )
                    rule = _RULE.model_copy(update={"actions": subset})
                    snapshot = OrdinaryAgentPolicySnapshot(
                        record_kind="proposed_ordinary_agent_v1",
                        authority_state="inert",
                        authorizes_execution=False,
                        record_id="ordinary-agent-policy-r3",
                        revision=3,
                        policy_digest=_DIGEST,
                        input_domain_id="ordinary-agent-effective-inputs-v1",
                        evaluator_semantics_version="ordinary-agent-eligibility-v1",
                        rules=(rule,),
                    )
                    principal = OrdinaryAgentPrincipal(
                        record_kind="proposed_ordinary_agent_v1",
                        authority_state="inert",
                        authorizes_execution=False,
                        record_id="ordinary-agent-principal-agent-one",
                        principal_id="agent_one",
                        execution_profile=expected,
                        status="active",
                    )
                    for absent_action in set(actions) - set(subset):
                        evaluation = evaluate_ordinary_agent_policy(
                            snapshot=snapshot,
                            principal=principal,
                            target=_TARGET,
                            action=absent_action,
                            managed_set_id=rule.managed_set_id,
                            managed_rule_id=rule.managed_rule_id,
                        )
                        self.assertEqual(evaluation.decision, "deny")
        with self.assertRaisesRegex(ValueError, "no execution-profile classification"):
            derive_ordinary_agent_execution_profile(cast(tuple[OrdinaryAgentAction, ...], ("x",)))

    def test_all_dormant_operations_have_no_store_or_provider_interface_or_effect(self) -> None:
        payloads = (
            {
                "action": "enroll",
                "principal_id": "agent_one",
                "policy": _policy_payload(),
                "expected_principal_absent": True,
            },
            {
                "action": "rotate_credential",
                "principal_id": "agent_one",
                "policy": _policy_payload(),
                "principal": _principal_payload(),
                "credential_id": "credential-agent-one",
                "credential_version": 4,
            },
            {
                "action": "revoke_principal",
                "principal_id": "agent_one",
                "principal": _principal_payload(),
            },
        )
        for payload in payloads:
            with self.subTest(action=payload["action"]):
                request = parse_ordinary_agent_enrollment_request(json.dumps(payload))
                result = dormant_ordinary_agent_enrollment(request)

                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.reason_code, "ordinary_agent_enrollment_not_activated")
                self.assertEqual(
                    result.diagnostic_codes,
                    ()
                    if payload["action"] == "revoke_principal"
                    else ("credential_custody_unavailable",),
                )
                self.assertEqual(result.effect_count, 0)
                self.assertFalse(result.authorizes_execution)
