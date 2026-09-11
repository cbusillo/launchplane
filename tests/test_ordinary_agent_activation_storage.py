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
from control_plane.storage.postgres import (
    OrdinaryAgentDeliveryActivationConflictError,
    PostgresRecordStore,
)
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION


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


def _reference(
    record: OrdinaryAgentDeliveryActivationRecord,
) -> OrdinaryAgentDeliveryActivationReference:
    return OrdinaryAgentDeliveryActivationReference(
        activation_id=record.activation_id,
        revision=record.revision,
        activation_sha256=record.activation_sha256,
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


class _FailingActivationStore(PostgresRecordStore):
    def _after_ordinary_agent_delivery_activation_write_step(self, step_name: str) -> None:
        if step_name == "installed_event_inserted":
            raise RuntimeError("injected event failure")


class _FailingDerivedActivationStore(PostgresRecordStore):
    def _after_ordinary_agent_delivery_activation_write_step(self, step_name: str) -> None:
        if step_name == "derived_event_inserted":
            raise RuntimeError("injected derived event failure")


class OrdinaryAgentDeliveryActivationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "launchplane.sqlite3"
        self.database_url = f"sqlite+pysqlite:///{database_path}"
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_schema_capability_and_exact_install_replay(self) -> None:
        revision, digest, valid = self.store.ordinary_agent_delivery_activation_schema_capability()
        self.assertEqual(revision, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue(valid)

        record = _record(
            operation_id="activation-setup-one",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        event = _event(
            record, action="installed", source_operation_id=record.source_setup_operation_id
        )
        written = self.store.install_ordinary_agent_delivery_activation(record, event)
        replayed = self.store.install_ordinary_agent_delivery_activation(record, event)

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "replayed")
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

    def test_revoke_is_exact_and_original_setup_recovery_is_historical(self) -> None:
        installed = _record(
            operation_id="activation-setup-revoke",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-12T20:00:00Z",
        )
        installed_event = _event(
            installed, action="installed", source_operation_id=installed.source_setup_operation_id
        )
        self.store.install_ordinary_agent_delivery_activation(installed, installed_event)
        revoked = _revoked(installed, occurred_at="2026-09-10T21:00:00Z")
        revoked_event = _event(
            revoked,
            action="revoked",
            source_operation_id="activation-revoke-one",
            previous=installed,
        )

        written = self.store.revoke_ordinary_agent_delivery_activation(revoked, revoked_event)
        replayed = self.store.revoke_ordinary_agent_delivery_activation(revoked, revoked_event)
        setup_result = self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
            installed.source_setup_operation_id
        )
        revoke_result = self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
            revoked_event.source_operation_id
        )

        self.assertEqual(written.status, "written")
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(
            self.store.install_ordinary_agent_delivery_activation(
                installed, installed_event
            ).status,
            "replayed",
        )
        self.assertEqual(setup_result, (installed, installed_event))
        self.assertEqual(revoke_result, (revoked, revoked_event))

    def test_derived_readiness_transitions_are_exact_atomic_and_replayable(self) -> None:
        installed = _record(
            operation_id="activation-setup-readiness",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-12T20:00:00Z",
        )
        installed_event = _event(
            installed, action="installed", source_operation_id=installed.source_setup_operation_id
        )
        self.store.install_ordinary_agent_delivery_activation(installed, installed_event)

        derived = self.store.transition_ordinary_agent_delivery_activation_readiness(
            expected_record=installed,
            action="guarded_derived",
            occurred_at="2026-09-10T21:00:00Z",
            evidence_ids=(
                "qualification-attestation",
                "provider-protection",
                "qualification-attestation",
            ),
        )
        replayed = self.store.transition_ordinary_agent_delivery_activation_readiness(
            expected_record=installed,
            action="guarded_derived",
            occurred_at="2026-09-10T21:00:00Z",
            evidence_ids=("provider-protection", "qualification-attestation"),
        )

        self.assertEqual(derived.status, "written")
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(derived.record.effective_state, "guarded")
        self.assertEqual(derived.record.updated_at, "2026-09-10T21:00:00+00:00")
        self.assertEqual(
            derived.event.evidence_ids,
            ("provider-protection", "qualification-attestation"),
        )
        self.assertEqual(derived.event.source_operation_id, "")

        lost = self.store.transition_ordinary_agent_delivery_activation_readiness(
            expected_record=derived.record,
            action="readiness_lost",
            occurred_at="2026-09-10T22:00:00Z",
            evidence_ids=("credential-revoked",),
            invalidation_reason="credential_revoked",
        )
        self.assertEqual(lost.record.effective_state, "qualification_only")
        self.assertEqual(lost.event.invalidation_reason, "credential_revoked")
        with self.assertRaises(OrdinaryAgentDeliveryActivationConflictError):
            self.store.transition_ordinary_agent_delivery_activation_readiness(
                expected_record=installed,
                action="guarded_derived",
                occurred_at="2026-09-10T23:00:00Z",
                evidence_ids=("stale-evidence",),
            )

    def test_derived_readiness_rolls_back_projection_when_event_insert_fails(self) -> None:
        installed = _record(
            operation_id="activation-setup-readiness-rollback",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-12T20:00:00Z",
        )
        installed_event = _event(
            installed, action="installed", source_operation_id=installed.source_setup_operation_id
        )
        self.store.install_ordinary_agent_delivery_activation(installed, installed_event)
        failing = _FailingDerivedActivationStore(database_url=self.database_url)
        self.addCleanup(failing.close)

        with self.assertRaisesRegex(RuntimeError, "injected derived event failure"):
            failing.transition_ordinary_agent_delivery_activation_readiness(
                expected_record=installed,
                action="guarded_derived",
                occurred_at="2026-09-10T21:00:00Z",
                evidence_ids=("qualification-attestation",),
            )

        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(installed.activation_id),
            installed,
        )
        self.assertEqual(
            self.store.list_ordinary_agent_delivery_activation_event_records(
                activation_id=installed.activation_id
            ),
            (installed_event,),
        )

    def test_expired_guarded_replacement_is_atomic_and_old_never_revives(self) -> None:
        first = _record(
            operation_id="activation-setup-expired-one",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-10T21:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        predecessor = _reference(first)
        second = _record(
            operation_id="activation-setup-expired-two",
            installed_at="2026-09-10T22:00:00Z",
            expires_at="2026-09-11T22:00:00Z",
            predecessor=predecessor,
        )
        superseded = OrdinaryAgentDeliveryActivationRecord.model_validate(
            {
                **first.model_dump(mode="json"),
                "revision": 2,
                "updated_at": second.installed_at,
                "superseded_by_activation_id": second.activation_id,
                "superseded_at": second.installed_at,
                "activation_sha256": "",
            }
        )
        superseded_event = _event(
            superseded,
            action="superseded",
            source_operation_id=second.source_setup_operation_id,
            previous=first,
        )
        installed_event = _event(
            second, action="installed", source_operation_id=second.source_setup_operation_id
        )

        replacement = self.store.install_ordinary_agent_delivery_activation(
            second,
            installed_event,
            predecessor=superseded,
            predecessor_event=superseded_event,
        )
        replacement_replay = self.store.install_ordinary_agent_delivery_activation(
            second,
            installed_event,
            predecessor=superseded,
            predecessor_event=superseded_event,
        )

        self.assertEqual(replacement.status, "written")
        self.assertEqual(replacement_replay.status, "replayed")
        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(first.activation_id),
            superseded,
        )
        self.assertEqual(
            self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
                first.source_setup_operation_id
            )[0],
            first,
        )
        self.assertEqual(
            self.store.recover_ordinary_agent_delivery_activation_by_source_operation(
                second.source_setup_operation_id
            ),
            (second, installed_event),
        )

    def test_revoked_predecessor_allows_new_identity_without_rewrite(self) -> None:
        first = _record(
            operation_id="activation-setup-revoked-predecessor",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-12T20:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        revoked = _revoked(first, occurred_at="2026-09-10T21:00:00Z")
        self.store.revoke_ordinary_agent_delivery_activation(
            revoked,
            _event(
                revoked,
                action="revoked",
                source_operation_id="activation-revoke-predecessor",
                previous=first,
            ),
        )
        predecessor = _reference(revoked)
        second = _record(
            operation_id="activation-setup-after-revoke",
            installed_at="2026-09-10T22:00:00Z",
            expires_at="2026-09-11T22:00:00Z",
            predecessor=predecessor,
        )
        self.store.install_ordinary_agent_delivery_activation(
            second,
            _event(
                second, action="installed", source_operation_id=second.source_setup_operation_id
            ),
            predecessor=revoked,
        )
        self.assertEqual(
            self.store.install_ordinary_agent_delivery_activation(
                second,
                _event(
                    second,
                    action="installed",
                    source_operation_id=second.source_setup_operation_id,
                ),
                predecessor=revoked,
            ).status,
            "replayed",
        )

        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(first.activation_id),
            revoked,
        )
        self.assertEqual(len(self.store.list_ordinary_agent_delivery_activation_records()), 2)

    def test_repository_rename_does_not_create_a_second_current_scope(self) -> None:
        first = _record(
            operation_id="activation-before-repository-rename",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        renamed_scope = first.scope.model_copy(
            update={
                "target": first.scope.target.model_copy(
                    update={"repository": "example/renamed-repository"}
                )
            }
        )
        renamed = _record(
            operation_id="activation-after-repository-rename",
            installed_at="2026-09-10T21:00:00Z",
            expires_at="2026-09-11T21:00:00Z",
            scope=renamed_scope,
        )

        with self.assertRaisesRegex(
            OrdinaryAgentDeliveryActivationConflictError,
            "exact predecessor",
        ):
            self.store.install_ordinary_agent_delivery_activation(
                renamed,
                _event(
                    renamed,
                    action="installed",
                    source_operation_id=renamed.source_setup_operation_id,
                ),
            )

        self.assertEqual(
            self.store.list_ordinary_agent_delivery_activation_records(),
            (first,),
        )

    def test_revoked_predecessor_rejects_setup_after_newer_history(self) -> None:
        first = _record(
            operation_id="activation-stale-predecessor-first",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        revoked_first = _revoked(first, occurred_at="2026-09-10T21:00:00Z")
        self.store.revoke_ordinary_agent_delivery_activation(
            revoked_first,
            _event(
                revoked_first,
                action="revoked",
                source_operation_id="activation-stale-predecessor-revoke-first",
                previous=first,
            ),
        )
        stale_reference = _reference(revoked_first)
        stale = _record(
            operation_id="activation-stale-predecessor-planned",
            installed_at="2026-09-10T22:00:00Z",
            expires_at="2026-09-11T22:00:00Z",
            predecessor=stale_reference,
        )
        newer = _record(
            operation_id="activation-stale-predecessor-newer",
            installed_at="2026-09-10T22:01:00Z",
            expires_at="2026-09-11T22:01:00Z",
            predecessor=stale_reference,
        )
        self.store.install_ordinary_agent_delivery_activation(
            newer,
            _event(
                newer,
                action="installed",
                source_operation_id=newer.source_setup_operation_id,
            ),
            predecessor=revoked_first,
        )
        revoked_newer = _revoked(newer, occurred_at="2026-09-10T22:02:00Z")
        self.store.revoke_ordinary_agent_delivery_activation(
            revoked_newer,
            _event(
                revoked_newer,
                action="revoked",
                source_operation_id="activation-stale-predecessor-revoke-newer",
                previous=newer,
            ),
        )

        with self.assertRaisesRegex(
            OrdinaryAgentDeliveryActivationConflictError,
            "exact latest predecessor",
        ):
            self.store.install_ordinary_agent_delivery_activation(
                stale,
                _event(
                    stale,
                    action="installed",
                    source_operation_id=stale.source_setup_operation_id,
                ),
                predecessor=revoked_first,
            )

        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(newer.activation_id),
            revoked_newer,
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_ordinary_agent_delivery_activation_record(stale.activation_id)

    def test_replacement_installed_at_must_be_newer_than_predecessor(self) -> None:
        first = _record(
            operation_id="activation-tied-predecessor-first",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        revoked = _revoked(first, occurred_at="2026-09-10T21:00:00Z")
        self.store.revoke_ordinary_agent_delivery_activation(
            revoked,
            _event(
                revoked,
                action="revoked",
                source_operation_id="activation-tied-predecessor-revoke",
                previous=first,
            ),
        )
        tied = _record(
            operation_id="activation-tied-predecessor-replacement",
            installed_at=first.installed_at,
            expires_at="2026-09-12T20:00:00Z",
            predecessor=_reference(revoked),
        )

        with self.assertRaisesRegex(
            OrdinaryAgentDeliveryActivationConflictError,
            "installed after its latest predecessor",
        ):
            self.store.install_ordinary_agent_delivery_activation(
                tied,
                _event(
                    tied,
                    action="installed",
                    source_operation_id=tied.source_setup_operation_id,
                ),
                predecessor=revoked,
            )

        self.assertEqual(
            self.store.list_ordinary_agent_delivery_activation_records(),
            (revoked,),
        )

    def test_event_failure_rolls_back_activation_projection(self) -> None:
        failing_store = _FailingActivationStore(database_url=self.database_url)
        self.addCleanup(failing_store.close)
        record = _record(
            operation_id="activation-setup-rollback",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-11T20:00:00Z",
        )
        event = _event(
            record, action="installed", source_operation_id=record.source_setup_operation_id
        )

        with self.assertRaisesRegex(RuntimeError, "injected event failure"):
            failing_store.install_ordinary_agent_delivery_activation(record, event)
        with self.assertRaises(FileNotFoundError):
            self.store.read_ordinary_agent_delivery_activation_record(record.activation_id)
        with self.assertRaises(FileNotFoundError):
            self.store.read_ordinary_agent_delivery_activation_event(event.event_id)

    def test_replacement_requires_exact_predecessor_and_event(self) -> None:
        first = _record(
            operation_id="activation-setup-cas-one",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-10T21:00:00Z",
        )
        self.store.install_ordinary_agent_delivery_activation(
            first,
            _event(first, action="installed", source_operation_id=first.source_setup_operation_id),
        )
        predecessor = _reference(first)
        second = _record(
            operation_id="activation-setup-cas-two",
            installed_at="2026-09-10T22:00:00Z",
            expires_at="2026-09-11T22:00:00Z",
            predecessor=predecessor,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentDeliveryActivationConflictError,
            "superseded event",
        ):
            self.store.install_ordinary_agent_delivery_activation(
                second,
                _event(
                    second,
                    action="installed",
                    source_operation_id=second.source_setup_operation_id,
                ),
                predecessor=predecessor,
            )
        self.assertEqual(
            self.store.read_ordinary_agent_delivery_activation_record(first.activation_id), first
        )
        self.assertEqual(len(self.store.list_ordinary_agent_delivery_activation_records()), 1)


if __name__ == "__main__":
    unittest.main()
