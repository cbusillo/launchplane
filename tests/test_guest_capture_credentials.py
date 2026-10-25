from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.guest_capture_credentials import (
    resolve_guest_capture_values,
    capture_boundary,
    guest_capture_metadata,
    shared_snapshot_retention,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import resolve_production_backup_binding
from tests.test_production_backup_authority import _policy, _source_target, _destination_target


class GuestCaptureCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(database_url=f"sqlite:///{Path(directory.name) / 'db'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_production_backup_target_record(_source_target())
        self.store.write_production_backup_target_record(_destination_target())
        self.store.write_production_backup_policy_record(_policy())
        self.binding = resolve_production_backup_binding(
            self.store,
            ProductionBackupGateRequest(
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=_policy().promotion_action,
                backup_record_id="capture",
            ),
        )
        self.environment = patch.dict(
            "os.environ", {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "fixture"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"):
            secrets.write_secret_value(
                record_store=self.store,
                scope="context_instance",
                integration="launchplane_worker",
                name=key,
                binding_key=key,
                plaintext_value=f"fixture-{key}",
                context_name="example-product",
                instance_name="prod",
                actor="fixture",
            )

        self.store.seed_guest_capture_bindings()

    def test_another_active_product_uses_the_same_unchanged_credential_records(self) -> None:
        records = self.store.list_secret_records()
        versions = {
            record.secret_id: self.store.list_secret_versions(secret_id=record.secret_id)
            for record in records
        }
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other", context="other", record_id="", policy_id="", policy_digest=""
        )
        policy = type(_policy()).model_validate(payload)
        self.store.write_production_backup_policy_record(policy)
        other = resolve_production_backup_binding(
            self.store,
            self.binding.request.model_copy(update={"product": "other", "context": "other"}),
        )
        self.assertEqual(
            resolve_guest_capture_values(self.store, self.binding),
            resolve_guest_capture_values(self.store, other),
        )
        self.assertEqual(self.store.list_secret_records(), records)
        for secret_id, original in versions.items():
            self.assertEqual(self.store.list_secret_versions(secret_id=secret_id), original)

    def test_guest_pin_replay_cannot_replace_or_reinterpret_the_original_pair(self) -> None:
        boundary = capture_boundary(
            self.binding.policy, self.binding.source_target, self.binding.destination_target
        )
        pin = self.store.read_guest_capture_binding(boundary)
        assert pin is not None
        events = self.store.list_secret_audit_events(secret_id=pin.private_key_secret_id)
        event = next(event for event in events if event.event_id == boundary.event_id)
        self.store.write_secret_audit_event(pin.audit_event("2026-10-25T00:00:00Z"))
        self.assertEqual(
            next(
                current
                for current in self.store.list_secret_audit_events(
                    secret_id=pin.private_key_secret_id
                )
                if current.event_id == boundary.event_id
            ),
            event,
        )
        for changed in (
            pin.model_copy(update={"known_hosts_secret_id": "replacement"}),
            pin.model_copy(update={"boundary": boundary.model_copy(update={"guest_id": "999"})}),
        ):
            with self.subTest(pin=changed), self.assertRaises(ValueError):
                self.store.write_secret_audit_event(changed.audit_event(event.recorded_at))
        self.assertEqual(self.store.read_guest_capture_binding(boundary), pin)

    def test_different_guest_storage_or_prefix_refuses_before_decrypt(self) -> None:
        for component, change in (
            ("source_target", {"guest_id": "999"}),
            ("source_target", {"host": "other.invalid"}),
            ("source_target", {"guest_kind": "qemu"}),
            ("destination_target", {"storage_id": "other"}),
        ):
            target = getattr(self.binding, component)
            changed = self.binding.model_copy(
                update={
                    component: target.model_copy(
                        update={"destination": target.destination.model_copy(update=change)}
                    )
                }
            )
            with (
                self.subTest(change=change),
                patch(
                    "control_plane.secrets._decrypt_secret_value",
                    side_effect=AssertionError("No decrypt"),
                ),
                self.assertRaises(ValueError),
            ):
                resolve_guest_capture_values(self.store, changed)
        policy = self.binding.policy.model_copy(
            update={
                "fast_snapshot": self.binding.policy.fast_snapshot.model_copy(
                    update={"snapshot_prefix": "other"}
                )
            }
        )
        with self.assertRaises(ValueError):
            resolve_guest_capture_values(
                self.store, self.binding.model_copy(update={"policy": policy})
            )

    def test_shared_prefix_preserves_the_highest_active_retention_even_after_review_expires(
        self,
    ) -> None:
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other",
            context="other",
            policy_id="",
            record_id="",
            policy_digest="",
            review_after="2026-09-02T00:00:00Z",
        )
        payload["fast_snapshot"]["retention_count"] = 20
        self.store.write_production_backup_policy_record(type(_policy()).model_validate(payload))
        self.assertEqual(shared_snapshot_retention(self.store, self.binding), 20)

    def test_unbound_extra_pair_cannot_displace_existing_guest_credential(self) -> None:
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other", context="other", policy_id="", record_id="", policy_digest=""
        )
        self.store.write_production_backup_policy_record(type(_policy()).model_validate(payload))
        for key in ("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"):
            secrets.write_secret_value(
                record_store=self.store,
                scope="context_instance",
                integration="launchplane_worker",
                name=key,
                binding_key=key,
                plaintext_value="different-fixture",
                context_name="other",
                instance_name="prod",
            )
        before = self.store.read_guest_capture_binding(
            capture_boundary(
                self.binding.policy, self.binding.source_target, self.binding.destination_target
            )
        )
        assert before is not None
        self.store.seed_guest_capture_bindings()
        self.assertEqual(self.store.read_guest_capture_binding(before.boundary), before)
        self.assertEqual(
            {record.context for _, record in guest_capture_metadata(self.store, self.binding)},
            {self.binding.request.context},
        )

    def test_unrelated_invalid_policy_does_not_break_credential_selection(self) -> None:
        payload = _source_target().model_dump(mode="json")
        payload.update(target_id="unrelated-guest", record_id="", target_digest="")
        source = type(_source_target()).model_validate(payload)
        self.store.write_production_backup_target_record(source)
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other", context="other", record_id="", policy_id="", policy_digest=""
        )
        payload["fast_snapshot"]["source_target_id"] = source.target_id
        payload["independent_backup"]["source_target_id"] = source.target_id
        self.store.write_production_backup_policy_record(type(_policy()).model_validate(payload))
        payload = source.model_dump(mode="json")
        payload.update(
            record_id="", target_digest="", target_revision=2, supersedes_record_id=source.record_id
        )
        payload["destination"]["host"] = "unrelated.invalid"
        self.store.write_production_backup_target_record(type(source).model_validate(payload))
        self.assertTrue(resolve_guest_capture_values(self.store, self.binding))

    def test_policy_move_keeps_old_key_on_old_guest_and_uses_existing_destination_pair(
        self,
    ) -> None:
        payload = _source_target().model_dump(mode="json")
        payload.update(target_id="old-guest", record_id="", target_digest="")
        payload["destination"]["guest_id"] = "202"
        old_source = type(_source_target()).model_validate(payload)
        self.store.write_production_backup_target_record(old_source)
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other", context="other", record_id="", policy_id="", policy_digest=""
        )
        payload["fast_snapshot"].update(
            source_target_id=old_source.target_id, snapshot_prefix="old-predeploy"
        )
        payload["independent_backup"]["source_target_id"] = old_source.target_id
        old_policy = type(_policy()).model_validate(payload)
        self.store.write_production_backup_policy_record(old_policy)
        for key in ("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"):
            secrets.write_secret_value(
                record_store=self.store,
                scope="context_instance",
                integration=secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
                name=key,
                binding_key=key,
                plaintext_value="old-guest-fixture",
                context_name="other",
                instance_name="prod",
            )
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("No version reads")
        ):
            self.store.seed_guest_capture_bindings()
        old_boundary = capture_boundary(old_policy, old_source, self.binding.destination_target)
        old_pin = self.store.read_guest_capture_binding(old_boundary)
        records = self.store.list_secret_records()
        versions = {
            record.secret_id: self.store.list_secret_versions(secret_id=record.secret_id)
            for record in records
        }
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other",
            context="other",
            record_id="",
            policy_id="",
            policy_digest="",
            policy_revision=2,
            supersedes_record_id=old_policy.record_id,
        )
        proposed_policy = type(_policy()).model_validate(payload)
        request = self.binding.request.model_copy(update={"product": "other", "context": "other"})
        proposed = ProductionBackupGateWorkerRequest(
            request=request,
            policy=proposed_policy,
            source_target=self.binding.source_target,
            destination_target=self.binding.destination_target,
        )
        expected = guest_capture_metadata(self.store, self.binding)
        self.assertEqual(guest_capture_metadata(self.store, proposed), expected)
        self.store.write_production_backup_policy_record(proposed_policy)
        current = resolve_production_backup_binding(self.store, request)
        self.assertEqual(guest_capture_metadata(self.store, current), expected)
        self.assertEqual(self.store.read_guest_capture_binding(old_boundary), old_pin)
        self.assertEqual(self.store.list_secret_records(), records)
        for secret_id, original in versions.items():
            self.assertEqual(self.store.list_secret_versions(secret_id=secret_id), original)
