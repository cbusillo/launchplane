from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import ValidationError

from control_plane.contracts.canonical_json import canonical_json_bytes
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentBudget,
    OrdinaryAgentEffectRecord,
    OrdinaryAgentEligibilityResult,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
    OrdinaryAgentPullRequest,
    OrdinaryAgentRequest,
    OrdinaryAgentReasonCode,
    OrdinaryAgentTarget,
)
from tests.support.ordinary_agent import (
    OrdinaryAgentResultConflictError,
    TestOrdinaryAgentEvidenceStore,
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
SHA = "b" * 40


def target() -> OrdinaryAgentTarget:
    return OrdinaryAgentTarget(repository_id=101, repository="example/project", base_branch="main")


def snapshot() -> OrdinaryAgentPolicySnapshot:
    return OrdinaryAgentPolicySnapshot(
        **INERT,
        record_id="policy-1",
        revision=1,
        policy_digest=DIGEST,
        input_domain_id="ordinary-agent-effective-inputs-v1",
        evaluator_semantics_version="ordinary-agent-eligibility-v1",
        rules=(
            OrdinaryAgentPolicyRule(
                managed_set_id="ordinary.agents",
                managed_rule_id="guarded.merge",
                principal_id="agent_one",
                target=target(),
                actions=("guarded_merge",),
            ),
        ),
    )


def eligibility_result(
    *, reason: OrdinaryAgentReasonCode = "eligible"
) -> OrdinaryAgentEligibilityResult:
    return OrdinaryAgentEligibilityResult(
        **INERT,
        record_id="result-1",
        request_id="request_one",
        evaluated_at=100,
        request_digest="c" * 64,
        principal_id="agent_one",
        session_id="session_one",
        lease_id="lease_one",
        decision="eligible" if reason == "eligible" else "denied",
        reason_code=reason,
        policy_record_id="policy-1",
        policy_revision=1,
        policy_digest=DIGEST,
        effective_decision_fingerprint=f"oae-fp-v1:{DIGEST}",
    )


class OrdinaryAgentContractTests(unittest.TestCase):
    def test_eligibility_rejects_contradictory_decision_and_reason(self) -> None:
        payload = eligibility_result().model_dump()
        for changes in (
            {"reason_code": "budget_exhausted"},
            {"decision": "denied"},
            {"decision": "denied", "reason_code": "policy_allowed"},
            {"evaluated_at": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                OrdinaryAgentEligibilityResult.model_validate({**payload, **changes})

    def test_models_are_strict_frozen_and_forbid_extra_fields(self) -> None:
        principal = OrdinaryAgentPrincipal(
            **INERT,
            record_id="principal-1",
            principal_id="agent_one",
            execution_profile="guarded_executor",
            status="active",
        )
        with self.assertRaises(ValidationError):
            OrdinaryAgentPrincipal.model_validate({**principal.model_dump(), "unexpected": "value"})
        with self.assertRaises(ValidationError):
            principal.status = "revoked"
        with self.assertRaises(ValidationError):
            OrdinaryAgentTarget.model_validate(
                {"repository_id": "101", "repository": "example/project", "base_branch": "main"}
            )

    def test_required_inert_markers_reject_missing_or_positive_authority(self) -> None:
        payload = snapshot().model_dump(mode="json")
        for marker in INERT:
            malformed = dict(payload)
            malformed.pop(marker)
            with self.subTest(marker=marker), self.assertRaises(ValidationError):
                OrdinaryAgentPolicySnapshot.model_validate(malformed)
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicySnapshot.model_validate({**payload, "authorizes_execution": True})
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicySnapshot.model_validate({**payload, "authority_state": "live"})

    def test_exact_targets_hashes_and_managed_ids_reject_wildcards_or_whitespace(self) -> None:
        for values in (
            {"repository_id": 1, "repository": "example/*", "base_branch": "main"},
            {"repository_id": 1, "repository": "example/project", "base_branch": "release *"},
            {"repository_id": 1, "repository": " example/project", "base_branch": "main"},
        ):
            with self.assertRaises(ValidationError):
                OrdinaryAgentTarget.model_validate(values)
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicyRule(
                managed_set_id="Bad ID",
                managed_rule_id="rule",
                principal_id="agent_one",
                target=target(),
                actions=("guarded_merge",),
            )
        with self.assertRaises(ValidationError):
            OrdinaryAgentPullRequest(number=1, head_sha="A" * 40)

    def test_request_requires_unique_finite_pull_requests_and_stack_subset(self) -> None:
        common = dict(
            **INERT,
            record_id="request-record",
            request_id="request_one",
            idempotency_key="request-key",
            lease_id="lease_one",
            session_id="session_one",
            principal_id="agent_one",
            target=target(),
            base_sha=SHA,
            action="guarded_merge",
        )
        with self.assertRaises(ValidationError):
            OrdinaryAgentRequest.model_validate(
                {
                    **common,
                    "pull_requests": (
                        OrdinaryAgentPullRequest(number=7, head_sha=SHA),
                        OrdinaryAgentPullRequest(number=7, head_sha="c" * 40),
                    ),
                    "permitted_stack_edit_pull_requests": (7,),
                }
            )
        with self.assertRaises(ValidationError):
            OrdinaryAgentRequest.model_validate(
                {
                    **common,
                    "pull_requests": (OrdinaryAgentPullRequest(number=7, head_sha=SHA),),
                    "permitted_stack_edit_pull_requests": (8,),
                }
            )

    def test_negative_budget_and_incoherent_effect_states_reject(self) -> None:
        with self.assertRaises(ValidationError):
            OrdinaryAgentBudget(
                window_start=1,
                window_end=2,
                action_limit=-1,
                actions_used=0,
                pull_request_limit=1,
                pull_requests_used=0,
            )
        common = dict(
            **INERT,
            record_id="effect-1",
            request_id="request_one",
            completed_effects=(),
            active_reservations=(),
            active_fences=(),
            provider_ttl_residual_seconds=None,
        )
        with self.assertRaises(ValidationError):
            OrdinaryAgentEffectRecord.model_validate(
                {**common, "state": "unknown_reconciliation_required", "success": True}
            )
        with self.assertRaises(ValidationError):
            OrdinaryAgentEffectRecord.model_validate(
                {**common, "state": "unknown_reconciliation_required", "success": False}
            )
        partial_payload = {
            **common,
            "completed_effects": ("effect-one",),
            "active_fences": ("fence-one",),
        }
        partial = OrdinaryAgentEffectRecord.model_validate(
            {**partial_payload, "state": "partially_completed", "success": False}
        )
        self.assertEqual(partial.state, "partially_completed")

    def test_serialization_round_trip_rejects_future_schema_and_preserves_false(self) -> None:
        payload = json.loads(snapshot().model_dump_json())
        self.assertIs(payload["authorizes_execution"], False)
        self.assertEqual(
            OrdinaryAgentPolicySnapshot.model_validate_json(json.dumps(payload)), snapshot()
        )
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicySnapshot.model_validate({**payload, "record_kind": "future_v2"})
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicySnapshot.model_validate(
                {**payload, "input_domain_id": "unknown-domain"}
            )

    def test_test_store_round_trip_replay_conflict_and_missing_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TestOrdinaryAgentEvidenceStore(snapshots=(snapshot(),))
            result = eligibility_result()
            self.assertEqual(store.read_snapshot("policy-1"), snapshot())
            self.assertIsNone(store.read_snapshot("missing"))
            self.assertEqual(store.put_result(result), result)
            self.assertEqual(store.put_result(result), result)
            self.assertEqual(store.get_result("result-1"), result)
            self.assertIsNone(store.get_result("missing"))
            round_trip_path = Path(directory) / "result.json"
            round_trip_path.write_bytes(canonical_json_bytes(result.model_dump(mode="json")))
            self.assertEqual(
                OrdinaryAgentEligibilityResult.model_validate_json(round_trip_path.read_bytes()),
                result,
            )
            with self.assertRaises(OrdinaryAgentResultConflictError):
                store.put_result(eligibility_result(reason="budget_exhausted"))
