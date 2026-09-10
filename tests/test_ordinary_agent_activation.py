from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationRequest,
    OrdinaryAgentDeliveryActivationScope,
    OrdinaryAgentDeliveryActivationSetupRequest,
    OrdinaryAgentDeliveryInventoryReference,
    OrdinaryAgentDeliveryPolicyPackageReference,
    OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
    build_ordinary_agent_delivery_activation_event_id,
    build_ordinary_agent_delivery_activation_id,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationActor,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationPlanningError,
    resolve_ordinary_agent_delivery_activation_setup_source,
)
from control_plane.privileged_operation_service import (
    cancel_privileged_operation,
    create_typed_privileged_operation_plan,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url


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
        read_attempt_versions=(1,),
        custody_reservation_versions=(1,),
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
    def test_request_union_rejects_mixed_or_caller_asserted_setup_fields(self) -> None:
        payload = {
            "action": "setup",
            "policy_operation_id": "privileged-operation-policy-package",
            "repository_inventory_record_id": "repository-inventory-1001-r1",
            "activation_expires_at": "2026-09-11T21:00:00Z",
            "reason": "Prepare one qualification-only activation.",
        }
        request: OrdinaryAgentDeliveryActivationRequest = TypeAdapter(
            OrdinaryAgentDeliveryActivationRequest
        ).validate_python(payload)

        self.assertIsInstance(request, OrdinaryAgentDeliveryActivationSetupRequest)
        for forbidden_field, value in (
            ("approver_github_id", 101),
            ("provider_installation_id", 5001),
            ("database_revision", "caller-asserted"),
            ("readiness", True),
            ("expected_revision", 1),
        ):
            with self.subTest(field=forbidden_field), self.assertRaises(ValidationError):
                TypeAdapter(OrdinaryAgentDeliveryActivationRequest).validate_python(
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


class OrdinaryAgentDeliveryActivationSourceTests(unittest.TestCase):
    def test_resolver_recomputes_one_rule_policy_package_and_rejects_cancellation(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            try:
                store.seed_authz_policy_if_absent(
                    LaunchplaneAuthzPolicyRecord(
                        record_id="launchplane-authz-policy-r1",
                        revision=1,
                        source="test",
                        updated_at="2026-09-10T20:00:00Z",
                        policy=LaunchplaneAuthzPolicy(schema_version=2),
                    )
                )
                inventory = RepositoryInventoryRecord(
                    repository_id="1001",
                    repository_owner_id="9001",
                    repository="example/launchplane",
                    inventory_state="tracked",
                    inventory_revision=1,
                    recorded_at="2026-09-10T20:00:00Z",
                    source="test",
                    reason="Bind the exact test repository.",
                )
                store.write_repository_inventory_record(inventory)
                planned = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="managed-authz-policy-set",
                    actor=PrivilegedOperationActor(
                        identity_type="github_human",
                        github_id=101,
                        login="admin",
                    ),
                    source_kind="browser_api",
                    source_event_id="activation-policy-package",
                    request=ManagedAuthzPolicySetProposalInput(
                        managed_set_id="ordinary-agent.pilot",
                        schema_migration="migrate_v2_to_v3",
                        reason="Propose exactly one ordinary-agent pilot rule.",
                        desired_policy=LaunchplaneAuthzPolicy.model_validate(
                            {
                                "schema_version": 3,
                                "ordinary_agents": [
                                    {
                                        "managed_set_id": "ordinary-agent.pilot",
                                        "managed_rule_id": "agent-one",
                                        "principal_id": "agent_one",
                                        "target": _scope().target.model_dump(mode="json"),
                                        "actions": ["self_read", "preflight"],
                                    }
                                ],
                            }
                        ),
                    ),
                    now=lambda: datetime(2026, 9, 10, 21, tzinfo=timezone.utc),
                ).record

                resolved = resolve_ordinary_agent_delivery_activation_setup_source(
                    store,
                    policy_operation_id=planned.operation_id,
                    repository_inventory_record_id=inventory.record_id,
                    observed_at=datetime(2026, 9, 10, 21, 5, tzinfo=timezone.utc),
                )
                cancel_privileged_operation(
                    record_store=store,
                    operation_id=planned.operation_id,
                    actor_github_id=101,
                    actor_login="admin",
                    source_event_id="cancel-activation-policy-package",
                    reason="Withdraw the proposed policy package.",
                    now=lambda: datetime(2026, 9, 10, 21, 10, tzinfo=timezone.utc),
                )
                with self.assertRaisesRegex(
                    OrdinaryAgentDeliveryActivationPlanningError,
                    "status is not admissible",
                ):
                    resolve_ordinary_agent_delivery_activation_setup_source(
                        store,
                        policy_operation_id=planned.operation_id,
                        repository_inventory_record_id=inventory.record_id,
                        observed_at=datetime(2026, 9, 10, 21, 15, tzinfo=timezone.utc),
                    )
            finally:
                store.close()

        self.assertEqual(resolved.scope, _scope())
        self.assertEqual(resolved.policy_package.policy_operation_id, planned.operation_id)
        self.assertEqual(resolved.policy_package.request_sha256, planned.request_digest)
        self.assertEqual(resolved.policy_package.evidence_sha256, planned.evidence_digest)
        self.assertEqual(resolved.inventory.record_id, inventory.record_id)
        self.assertIsInstance(planned.evidence, ManagedAuthzPolicySetHumanEvidence)
        assert isinstance(planned.evidence, ManagedAuthzPolicySetHumanEvidence)
        self.assertEqual(
            resolved.policy_package.candidate_policy_sha256,
            planned.evidence.diff.desired_policy_sha256,
        )

    def test_resolver_accepts_schema_v3_reconcile_without_repeated_migration(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            try:
                human_rule = {
                    "managed_set_id": "ordinary-agent.pilot",
                    "managed_rule_id": "reviewer",
                    "github_ids": [101],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["ordinary_agent_delivery_activation.approve"],
                }
                active_v3 = LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-r3",
                    revision=3,
                    source="test",
                    updated_at="2026-09-10T20:00:00Z",
                    policy=LaunchplaneAuthzPolicy.model_validate(
                        {"schema_version": 3, "github_humans": [human_rule]}
                    ),
                )
                inventory = RepositoryInventoryRecord(
                    repository_id="1001",
                    repository_owner_id="9001",
                    repository="example/launchplane",
                    inventory_state="tracked",
                    inventory_revision=1,
                    recorded_at="2026-09-10T20:00:00Z",
                    source="test",
                    reason="Bind the exact test repository.",
                )
                store.write_repository_inventory_record(inventory)
                with patch.object(
                    store,
                    "list_authz_policy_records",
                    return_value=(active_v3,),
                ):
                    planned = create_typed_privileged_operation_plan(
                        record_store=store,
                        descriptor_id="managed-authz-policy-set",
                        actor=PrivilegedOperationActor(
                            identity_type="github_human",
                            github_id=101,
                            login="admin",
                        ),
                        source_kind="browser_api",
                        source_event_id="activation-policy-package-v3",
                        request=ManagedAuthzPolicySetProposalInput(
                            managed_set_id="ordinary-agent.pilot",
                            reason="Reconcile one ordinary-agent rule under schema v3.",
                            desired_policy=LaunchplaneAuthzPolicy.model_validate(
                                {
                                    "schema_version": 3,
                                    "github_humans": [human_rule],
                                    "ordinary_agents": [
                                        {
                                            "managed_set_id": "ordinary-agent.pilot",
                                            "managed_rule_id": "agent-one",
                                            "principal_id": "agent_one",
                                            "target": _scope().target.model_dump(mode="json"),
                                            "actions": ["self_read", "preflight"],
                                        }
                                    ],
                                }
                            ),
                        ),
                        now=lambda: datetime(2026, 9, 10, 21, tzinfo=timezone.utc),
                    ).record

                    resolved = resolve_ordinary_agent_delivery_activation_setup_source(
                        store,
                        policy_operation_id=planned.operation_id,
                        repository_inventory_record_id=inventory.record_id,
                        observed_at=datetime(2026, 9, 10, 21, 5, tzinfo=timezone.utc),
                    )
            finally:
                store.close()

        self.assertEqual(resolved.scope, _scope())
        self.assertEqual(resolved.policy_package.policy_operation_id, planned.operation_id)
        self.assertIsInstance(planned.request, ManagedAuthzPolicySetProposalInput)
        assert isinstance(planned.request, ManagedAuthzPolicySetProposalInput)
        self.assertEqual(planned.request.schema_migration, "reject")


if __name__ == "__main__":
    unittest.main()
