from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationScope,
    OrdinaryAgentDeliveryActivationSetupRequest,
    OrdinaryAgentDeliveryInventoryReference,
    OrdinaryAgentDeliveryPolicyPackageReference,
    OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
    build_ordinary_agent_delivery_activation_event_id,
    build_ordinary_agent_delivery_activation_id,
)


def _scope() -> OrdinaryAgentDeliveryActivationScope:
    return OrdinaryAgentDeliveryActivationScope(
        target=OrdinaryAgentTarget(
            repository_id=1001,
            repository="example/launchplane",
            base_branch="main",
        ),
        managed_set_id="ordinary-agent.pilot",
        managed_rule_id="agent-one",
    )


def _capability() -> OrdinaryAgentDeliveryRuntimeCapabilityEvidence:
    return OrdinaryAgentDeliveryRuntimeCapabilityEvidence(
        observed_database_revision="test-head",
        database_revision_compatible=True,
        activation_schema_invariants_sha256="a" * 64,
        activation_schema_invariants_valid=True,
        finite_request_versions=(1, 2),
        read_attempt_versions=(1, 2),
        custody_issue_attempt_versions=(1,),
        qualification_attestation_versions=(1,),
        activation_record_versions=(1,),
        activation_event_versions=(1,),
        recovery_versions=(1,),
        authz_policy_read_versions=(2, 3),
        variant_parsers_registered=True,
        activation_storage_registered=True,
        activation_cas_registered=True,
        activation_recovery_registered=True,
        bounded_cleanup_registered=True,
        rollback_reader_registered=True,
        qualification_advancer_registered=False,
        guarded_worker_registered=False,
        policy_v3_write_supported=False,
        observed_at="2026-09-10T21:00:00Z",
    )


def _policy_package() -> OrdinaryAgentDeliveryPolicyPackageReference:
    return OrdinaryAgentDeliveryPolicyPackageReference(
        policy_operation_id="privileged-operation-policy-package",
        request_sha256="b" * 64,
        evidence_sha256="c" * 64,
        plan_sha256="d" * 64,
        desired_set_sha256="e" * 64,
        candidate_policy_sha256="f" * 64,
    )


def _inventory() -> OrdinaryAgentDeliveryInventoryReference:
    return OrdinaryAgentDeliveryInventoryReference(
        record_id="repository-inventory-1001-r1",
        revision=1,
        inventory_sha256="1" * 64,
    )


class OrdinaryAgentDeliveryActivationContractTests(unittest.TestCase):
    def test_historical_setup_rejects_mixed_or_caller_asserted_fields(self) -> None:
        payload = {
            "action": "setup",
            "policy_operation_id": "privileged-operation-policy-package",
            "repository_inventory_record_id": "repository-inventory-1001-r1",
            "activation_expires_at": "2026-09-11T21:00:00Z",
            "reason": "Prepare one qualification-only activation.",
        }
        request = OrdinaryAgentDeliveryActivationSetupRequest.model_validate(payload)

        self.assertIsInstance(request, OrdinaryAgentDeliveryActivationSetupRequest)
        for forbidden_field, value in (
            ("approver_github_id", 101),
            ("provider_installation_id", 5001),
            ("database_revision", "caller-asserted"),
            ("readiness", True),
            ("expected_revision", 1),
        ):
            with self.subTest(field=forbidden_field), self.assertRaises(ValidationError):
                OrdinaryAgentDeliveryActivationSetupRequest.model_validate(
                    {**payload, forbidden_field: value}
                )

    def test_record_digest_and_expired_predecessor_supersession_are_explicit(self) -> None:
        scope = _scope()
        setup_operation_id = "privileged-operation-setup-one"
        activation_id = build_ordinary_agent_delivery_activation_id(
            scope=scope,
            source_setup_operation_id=setup_operation_id,
        )
        record = OrdinaryAgentDeliveryActivationRecord(
            activation_id=activation_id,
            scope=scope,
            source_setup_operation_id=setup_operation_id,
            source_setup_approval_sha256="2" * 64,
            policy_package=_policy_package(),
            inventory=_inventory(),
            desired_state="guarded",
            effective_state="qualification_only",
            activation_expires_at="2026-09-11T21:00:00Z",
            runtime_capability_at_setup=_capability(),
            revision=1,
            installed_at="2026-09-10T21:00:00Z",
            updated_at="2026-09-10T21:00:00Z",
        )

        self.assertFalse(record.authorizes_execution)
        self.assertEqual(
            OrdinaryAgentDeliveryActivationRecord.model_validate(record.model_dump()),
            record,
        )
        successor_id = build_ordinary_agent_delivery_activation_id(
            scope=scope,
            source_setup_operation_id="privileged-operation-setup-two",
        )
        superseded = OrdinaryAgentDeliveryActivationRecord.model_validate(
            {
                **record.model_dump(),
                "revision": 2,
                "updated_at": "2026-09-12T21:00:00Z",
                "superseded_by_activation_id": successor_id,
                "superseded_at": "2026-09-12T21:00:00Z",
                "activation_sha256": "",
            }
        )
        event = OrdinaryAgentDeliveryActivationEvent(
            event_id=build_ordinary_agent_delivery_activation_event_id(
                activation_id=record.activation_id,
                sequence=2,
                action="superseded",
                source_operation_id="privileged-operation-setup-two",
            ),
            activation_id=record.activation_id,
            sequence=2,
            action="superseded",
            previous_revision=record.revision,
            previous_activation_sha256=record.activation_sha256,
            resulting_revision=superseded.revision,
            resulting_activation_sha256=superseded.activation_sha256,
            resulting_desired_state=superseded.desired_state,
            resulting_effective_state=superseded.effective_state,
            occurred_at="2026-09-12T21:00:00Z",
            source_operation_id="privileged-operation-setup-two",
        )

        self.assertEqual(event.previous_activation_sha256, record.activation_sha256)
        self.assertEqual(superseded.desired_state, "guarded")
        self.assertEqual(superseded.effective_state, "qualification_only")
        self.assertEqual(superseded.superseded_by_activation_id, successor_id)
        with self.assertRaisesRegex(ValidationError, "only an expired guarded activation"):
            OrdinaryAgentDeliveryActivationRecord.model_validate(
                {
                    **record.model_dump(),
                    "revision": 2,
                    "updated_at": "2026-09-10T22:00:00Z",
                    "superseded_by_activation_id": successor_id,
                    "superseded_at": "2026-09-10T22:00:00Z",
                    "activation_sha256": "",
                }
            )


if __name__ == "__main__":
    unittest.main()
