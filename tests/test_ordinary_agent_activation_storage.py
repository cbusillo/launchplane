from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationEventAction,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationReference,
    OrdinaryAgentDeliveryActivationScope,
    OrdinaryAgentDeliveryInventoryReference,
    OrdinaryAgentDeliveryPolicyPackageReference,
    OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
    build_ordinary_agent_delivery_activation_event_id,
    build_ordinary_agent_delivery_activation_id,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION
from tests.support.activation_history import seed_activation_history


def _scope(*, repository_id: int = 1001) -> OrdinaryAgentDeliveryActivationScope:
    return OrdinaryAgentDeliveryActivationScope(
        target=OrdinaryAgentTarget(
            repository_id=repository_id,
            repository=f"example/repository-{repository_id}",
            base_branch="main",
        ),
        managed_set_id="ordinary-agent-delivery",
        managed_rule_id="guarded-main",
    )


def _capability() -> OrdinaryAgentDeliveryRuntimeCapabilityEvidence:
    return OrdinaryAgentDeliveryRuntimeCapabilityEvidence(
        observed_database_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
        database_revision_compatible=True,
        activation_schema_invariants_sha256="a" * 64,
        activation_schema_invariants_valid=True,
        finite_request_versions=(1,),
        read_attempt_versions=(1,),
        custody_issue_attempt_versions=(1,),
        qualification_attestation_versions=(),
        activation_record_versions=(1,),
        activation_event_versions=(1,),
        recovery_versions=(1,),
        authz_policy_read_versions=(2,),
        variant_parsers_registered=True,
        activation_storage_registered=True,
        activation_cas_registered=True,
        activation_recovery_registered=True,
        bounded_cleanup_registered=True,
        rollback_reader_registered=True,
        qualification_advancer_registered=False,
        guarded_worker_registered=False,
        policy_v3_write_supported=False,
        observed_at="2026-09-10T20:00:00Z",
    )


def _record(
    *,
    operation_id: str,
    installed_at: str,
    expires_at: str,
    scope: OrdinaryAgentDeliveryActivationScope | None = None,
    predecessor: OrdinaryAgentDeliveryActivationReference | None = None,
) -> OrdinaryAgentDeliveryActivationRecord:
    resolved_scope = scope or _scope()
    return OrdinaryAgentDeliveryActivationRecord(
        activation_id=build_ordinary_agent_delivery_activation_id(
            scope=resolved_scope,
            source_setup_operation_id=operation_id,
        ),
        scope=resolved_scope,
        source_setup_operation_id=operation_id,
        source_setup_approval_sha256="b" * 64,
        policy_package=OrdinaryAgentDeliveryPolicyPackageReference(
            policy_operation_id=f"{operation_id}-policy",
            request_sha256="1" * 64,
            evidence_sha256="2" * 64,
            plan_sha256="3" * 64,
            desired_set_sha256="4" * 64,
            candidate_policy_sha256="5" * 64,
        ),
        inventory=OrdinaryAgentDeliveryInventoryReference(
            record_id=f"{operation_id}-inventory",
            revision=1,
            inventory_sha256="6" * 64,
        ),
        desired_state="guarded",
        effective_state="qualification_only",
        activation_expires_at=expires_at,
        runtime_capability_at_setup=_capability(),
        revision=1,
        predecessor=predecessor,
        installed_at=installed_at,
        updated_at=installed_at,
    )


def _event(
    record: OrdinaryAgentDeliveryActivationRecord,
    *,
    action: OrdinaryAgentDeliveryActivationEventAction,
    source_operation_id: str,
    previous: OrdinaryAgentDeliveryActivationRecord | None = None,
) -> OrdinaryAgentDeliveryActivationEvent:
    return OrdinaryAgentDeliveryActivationEvent.model_validate(
        {
            "event_id": build_ordinary_agent_delivery_activation_event_id(
                activation_id=record.activation_id,
                sequence=record.revision,
                action=action,
                source_operation_id=source_operation_id,
            ),
            "activation_id": record.activation_id,
            "sequence": record.revision,
            "action": action,
            "previous_revision": previous.revision if previous is not None else 0,
            "previous_activation_sha256": (
                previous.activation_sha256 if previous is not None else ""
            ),
            "resulting_revision": record.revision,
            "resulting_activation_sha256": record.activation_sha256,
            "resulting_desired_state": record.desired_state,
            "resulting_effective_state": record.effective_state,
            "occurred_at": record.updated_at,
            "source_operation_id": source_operation_id,
        }
    )


def _revoked(
    record: OrdinaryAgentDeliveryActivationRecord, *, occurred_at: str
) -> OrdinaryAgentDeliveryActivationRecord:
    return OrdinaryAgentDeliveryActivationRecord.model_validate(
        {
            **record.model_dump(mode="json"),
            "desired_state": "revoked",
            "effective_state": "revoked",
            "revision": record.revision + 1,
            "updated_at": occurred_at,
            "revoked_at": occurred_at,
            "activation_sha256": "",
        }
    )


class OrdinaryAgentDeliveryActivationHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "launchplane.sqlite3"
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{database_path}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_historical_records_and_events_read_back_exactly(self) -> None:
        record = _record(
            operation_id="activation-setup-one",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        event = _event(
            record, action="installed", source_operation_id=record.source_setup_operation_id
        )
        seed_activation_history(self.store, record, event)

        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(record.activation_id), record
        )
        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_event(event.event_id), event
        )
        self.assertEqual(self.store.list_ordinary_agent_delivery_activation_records(), (record,))
        self.assertEqual(
            self.store.list_ordinary_agent_delivery_activation_event_records(
                activation_id=record.activation_id
            ),
            (event,),
        )

    def test_recovery_projects_each_terminal_outcome_by_source_operation(self) -> None:
        installed = _record(
            operation_id="activation-setup-revoke",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-12T20:00:00Z",
        )
        installed_event = _event(
            installed, action="installed", source_operation_id=installed.source_setup_operation_id
        )
        revoked = _revoked(installed, occurred_at="2026-09-10T21:00:00Z")
        revoked_event = _event(
            revoked,
            action="revoked",
            source_operation_id="activation-revoke-one",
            previous=installed,
        )
        seed_activation_history(self.store, installed, installed_event)
        seed_activation_history(self.store, revoked, revoked_event)

        self.assertEqual(
            self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
                installed.source_setup_operation_id
            ),
            (installed, installed_event),
        )
        self.assertEqual(
            self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
                revoked_event.source_operation_id
            ),
            (revoked, revoked_event),
        )
        with self.assertRaises(FileNotFoundError):
            self.store.recover_ordinary_agent_delivery_activation_by_source_operation("unknown")


if __name__ == "__main__":
    unittest.main()
