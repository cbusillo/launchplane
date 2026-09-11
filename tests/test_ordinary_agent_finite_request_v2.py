from __future__ import annotations

import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest, OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_client import (
    OrdinaryAgentFiniteRequestServerFields,
    build_ordinary_agent_finite_request_from_client,
    ordinary_agent_finite_client_intent_matches,
    ordinary_agent_finite_client_intent_sha256,
    ordinary_agent_finite_request_deadlines,
    ordinary_agent_finite_request_id,
    parse_ordinary_agent_finite_client_request,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobClaimFence,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentQualificationFiniteRequestV2,
    parse_ordinary_agent_finite_request,
)
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
    cancel_ordinary_agent_finite_request,
    ordinary_agent_finite_request_intent_sha256,
    rebind_ordinary_agent_finite_request,
)
from control_plane.ordinary_agent_merge_train_job import advance_ordinary_agent_merge_train_job


_V1_GOLDEN = (
    '{"schema_version":1,"request_id":"request-one","idempotency_key":"idempotency-one",'
    '"principal_id":"agent_one","session_id":"session-one","lease_id":"lease-one",'
    '"target":{"repository_id":42,"repository":"example/repo","base_branch":"main"},'
    '"base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","pull_requests":[{"number":12,'
    '"head_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],'
    '"permitted_stack_edit_pull_requests":[],"binding_revision":1,'
    '"refresh_allowance_total":1,"refresh_used":0,"admitted_at":1800000000,'
    '"expires_at":1800000100,"continuation_expires_at":1800000200,"status":"waiting",'
    '"cancellation_requested_at":null,"execution_record_ids":[]}'
)


class OrdinaryAgentFiniteRequestV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = OrdinaryAgentTarget(
            repository_id=42, repository="example/repo", base_branch="main"
        )
        self.common = {
            "request_id": "request-two",
            "idempotency_key": "idempotency-two",
            "principal_id": "agent_one",
            "session_id": "session-one",
            "lease_id": "lease-one",
            "target": self.target,
            "binding_revision": 1,
            "admitted_at": 1_800_000_000,
            "expires_at": 1_800_000_100,
            "continuation_expires_at": 1_800_000_200,
        }

    def test_v1_golden_bytes_and_identities_remain_unchanged(self) -> None:
        request = OrdinaryAgentFiniteRequestRecord.model_validate_json(_V1_GOLDEN)

        self.assertEqual(request.model_dump_json(), _V1_GOLDEN)
        self.assertEqual(
            request.scope_sha256,
            "ec9b496643b629305d348b85b026da7ed3fc4ddab569e4b2016852f3ea043a25",
        )
        self.assertEqual(
            ordinary_agent_finite_request_intent_sha256(request),
            "5d51dea8e3755e45373de97bc17a40882d660c78fce44c093b2f4300b8194c54",
        )
        self.assertEqual(parse_ordinary_agent_finite_request(request.model_dump()), request)
        legacy_without_explicit_version = request.model_dump(exclude={"schema_version"})
        self.assertEqual(
            parse_ordinary_agent_finite_request(legacy_without_explicit_version), request
        )

    def test_v2_variants_are_closed_and_domain_separated(self) -> None:
        qualification = OrdinaryAgentQualificationFiniteRequestV2.model_validate(self.common)
        guarded = OrdinaryAgentGuardedDeliveryFiniteRequestV2.model_validate(
            {
                **self.common,
                "base_sha": "a" * 40,
                "pull_requests": (OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
                "permitted_stack_edit_pull_requests": (),
                "refresh_allowance_total": 1,
            }
        )

        self.assertIsInstance(
            parse_ordinary_agent_finite_request(qualification.model_dump()),
            OrdinaryAgentQualificationFiniteRequestV2,
        )
        self.assertIsInstance(
            parse_ordinary_agent_finite_request(guarded.model_dump()),
            OrdinaryAgentGuardedDeliveryFiniteRequestV2,
        )
        self.assertNotEqual(qualification.scope_sha256, guarded.scope_sha256)
        self.assertNotEqual(
            ordinary_agent_finite_request_intent_sha256(qualification),
            ordinary_agent_finite_request_intent_sha256(guarded),
        )
        with self.assertRaises(ValidationError):
            OrdinaryAgentQualificationFiniteRequestV2.model_validate(
                {**qualification.model_dump(), "base_sha": "a" * 40}
            )
        with self.assertRaises(ValidationError):
            parse_ordinary_agent_finite_request({**qualification.model_dump(), "schema_version": 3})

    def test_client_v2_shapes_contain_only_caller_intent(self) -> None:
        qualification = parse_ordinary_agent_finite_client_request(
            {
                "schema_version": 2,
                "purpose": "qualification",
                "idempotency_key": "idempotency-client",
                "session_id": "session-one",
                "lease_id": "lease-one",
            }
        )
        self.assertEqual(
            set(qualification.model_dump()),
            {"schema_version", "purpose", "idempotency_key", "session_id", "lease_id"},
        )
        guarded = parse_ordinary_agent_finite_client_request(
            {
                "schema_version": 2,
                "purpose": "guarded_delivery",
                "idempotency_key": "idempotency-client-guarded",
                "session_id": "session-one",
                "lease_id": "lease-one",
                "base_sha": "a" * 40,
                "pull_requests": [{"number": 12, "head_sha": "b" * 40}],
                "permitted_stack_edit_pull_requests": [],
                "refresh_allowance": 2,
            }
        )
        self.assertEqual(
            set(guarded.model_dump()),
            {
                "schema_version",
                "purpose",
                "idempotency_key",
                "session_id",
                "lease_id",
                "base_sha",
                "pull_requests",
                "permitted_stack_edit_pull_requests",
                "refresh_allowance",
            },
        )
        for value in (
            {**qualification.model_dump(), "target": self.target.model_dump()},
            {**qualification.model_dump(), "base_sha": "a" * 40},
            {**guarded.model_dump(), "principal_id": "agent_one"},
            {**guarded.model_dump(), "schema_version": 1},
            {**guarded.model_dump(), "purpose": "unknown"},
        ):
            with self.assertRaises(ValidationError):
                parse_ordinary_agent_finite_client_request(value)

    def test_client_identity_and_request_id_are_stable_and_server_derived(self) -> None:
        request = parse_ordinary_agent_finite_client_request(
            {
                "schema_version": 2,
                "purpose": "qualification",
                "idempotency_key": "idempotency-client",
                "session_id": "session-one",
                "lease_id": "lease-one",
            }
        )
        self.assertEqual(
            ordinary_agent_finite_request_id(
                principal_id="agent_one", idempotency_key="idempotency-client"
            ),
            ordinary_agent_finite_request_id(
                principal_id="agent_one", idempotency_key="idempotency-client"
            ),
        )
        self.assertNotEqual(
            ordinary_agent_finite_request_id(
                principal_id="agent_one", idempotency_key="idempotency-client"
            ),
            ordinary_agent_finite_request_id(
                principal_id="agent_two", idempotency_key="idempotency-client"
            ),
        )
        self.assertRegex(
            ordinary_agent_finite_request_id(
                principal_id="agent_one", idempotency_key="idempotency-client"
            ),
            r"^ordinary-request-[0-9a-f]{32}$",
        )
        before = ordinary_agent_finite_client_intent_sha256(request)
        rebuilt = request.model_copy(update={"idempotency_key": "different"})
        self.assertNotEqual(before, ordinary_agent_finite_client_intent_sha256(rebuilt))

    def test_server_builder_derives_authority_and_fresh_timing_fields(self) -> None:
        request = parse_ordinary_agent_finite_client_request(
            {
                "schema_version": 2,
                "purpose": "guarded_delivery",
                "idempotency_key": "idempotency-client",
                "session_id": "session-one",
                "lease_id": "lease-one",
                "base_sha": "a" * 40,
                "pull_requests": [{"number": 12, "head_sha": "b" * 40}],
                "permitted_stack_edit_pull_requests": [],
                "refresh_allowance": 2,
            }
        )
        persisted = build_ordinary_agent_finite_request_from_client(
            request,
            server=OrdinaryAgentFiniteRequestServerFields(
                principal_id="agent_one",
                target=self.target,
                admitted_at=1_800_000_000,
                lease_expires_at=1_800_000_500,
                continuation_expires_at=1_800_000_200,
                binding_revision=1,
                request_lifetime_seconds=100,
                refresh_allowance_ceiling=2,
            ),
        )
        self.assertIsInstance(persisted, OrdinaryAgentGuardedDeliveryFiniteRequestV2)
        assert isinstance(persisted, OrdinaryAgentGuardedDeliveryFiniteRequestV2)
        self.assertEqual(
            persisted.request_id,
            ordinary_agent_finite_request_id(
                principal_id="agent_one", idempotency_key="idempotency-client"
            ),
        )
        self.assertEqual(persisted.principal_id, "agent_one")
        self.assertEqual(persisted.target, self.target)
        self.assertEqual(persisted.expires_at, 1_800_000_100)
        self.assertEqual(persisted.refresh_allowance_total, 2)
        self.assertEqual(persisted.status, "waiting")
        self.assertEqual(persisted.refresh_used, 0)
        self.assertTrue(ordinary_agent_finite_client_intent_matches(request, persisted))
        self.assertFalse(
            ordinary_agent_finite_client_intent_matches(
                request.model_copy(update={"refresh_allowance": 1}), persisted
            )
        )
        with self.assertRaisesRegex(ValueError, "refresh allowance exceeds server ceiling"):
            build_ordinary_agent_finite_request_from_client(
                request.model_copy(update={"refresh_allowance": 3}),
                server=OrdinaryAgentFiniteRequestServerFields(
                    principal_id="agent_one",
                    target=self.target,
                    admitted_at=1_800_000_000,
                    lease_expires_at=1_800_000_500,
                    continuation_expires_at=1_800_000_200,
                    refresh_allowance_ceiling=2,
                ),
            )

    def test_deadline_builder_fails_when_locked_lease_is_expired(self) -> None:
        with self.assertRaisesRegex(ValueError, "lease must outlive admission"):
            ordinary_agent_finite_request_deadlines(
                admitted_at=100,
                lease_expires_at=100,
                continuation_expires_at=None,
            )

    def test_variant_preserving_lifecycle_updates_do_not_adapt_shape(self) -> None:
        qualification = OrdinaryAgentQualificationFiniteRequestV2.model_validate(self.common)
        cancelled = cancel_ordinary_agent_finite_request(request=qualification, now=1_800_000_001)
        self.assertIsInstance(cancelled, OrdinaryAgentQualificationFiniteRequestV2)
        self.assertEqual(cancelled.status, "cancelled")

        guarded = OrdinaryAgentGuardedDeliveryFiniteRequestV2.model_validate(
            {
                **self.common,
                "base_sha": "a" * 40,
                "pull_requests": (OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
                "permitted_stack_edit_pull_requests": (),
                "refresh_allowance_total": 1,
            }
        )
        rebound = rebind_ordinary_agent_finite_request(
            request=guarded,
            base_sha="c" * 40,
            pull_requests=guarded.pull_requests,
            expected_binding_revision=1,
        )
        self.assertIsInstance(rebound, OrdinaryAgentGuardedDeliveryFiniteRequestV2)
        self.assertEqual(rebound.scope_sha256, guarded.scope_sha256)

    def test_existing_merge_advancer_refuses_qualification_before_store_access(self) -> None:
        qualification = OrdinaryAgentQualificationFiniteRequestV2.model_validate(self.common)
        store = Mock()
        claimed = OrdinaryAgentClaimedJob(
            request=qualification,
            claim_fence=OrdinaryAgentJobClaimFence(
                request_id=qualification.request_id,
                worker_id="worker-one",
                generation=1,
            ),
            claim_expires_at=1_800_000_010,
        )

        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "request_purpose_unsupported"
        ):
            advance_ordinary_agent_merge_train_job(claimed=claimed, store=store)
        self.assertEqual(store.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
