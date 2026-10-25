from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.guest_capture_boundary import GUEST_CAPTURE_INTEGRATION
from control_plane.product_config import _plan_product_config_secret_write
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.guest_capture_credentials import (
    plan_guest_credential_adoption,
    resolve_guest_capture_values,
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

    def adopt(self) -> None:
        records, bindings = plan_guest_credential_adoption(
            binding=self.binding,
            records=self.store.list_secret_records(),
            bindings=self.store.list_secret_bindings(),
        )
        for record in records:
            self.store.write_secret_record(record)
        for binding in bindings:
            self.store.write_secret_binding(binding)

    def test_adoption_never_reads_versions_and_second_product_uses_same_records(self) -> None:
        versions = {
            r.secret_id: self.store.list_secret_versions(secret_id=r.secret_id)
            for r in self.store.list_secret_records()
        }
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("No values")
        ):
            self.adopt()
        self.assertEqual(set(versions), {r.secret_id for r in self.store.list_secret_records()})
        for secret_id, original in versions.items():
            self.assertEqual(original, self.store.list_secret_versions(secret_id=secret_id))
        request = self.binding.request.model_copy(update={"product": "other", "context": "other"})
        other = self.binding.model_copy(update={"request": request})
        self.assertEqual(
            resolve_guest_capture_values(self.store, self.binding),
            resolve_guest_capture_values(self.store, other),
        )
        self.assertEqual(
            secrets.resolve_secret_values_for_integration_from_store(
                record_store=self.store,
                integration="launchplane_worker",
                context_name="example-product",
                instance_name="prod",
            ),
            {},
        )

    def test_different_guest_storage_or_prefix_refuses_before_decrypt(self) -> None:
        self.adopt()
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

    def test_shared_prefix_uses_highest_active_retention_even_when_policy_review_expired(
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

    def test_startup_adoption_is_atomic_idempotent_and_preserves_custody(self) -> None:
        with patch.object(
            self.store, "_after_product_authority_bundle_step", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                self.store.adopt_guest_capture_credentials()
        self.assertTrue(
            all(
                r.integration == secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
                for r in self.store.list_secret_records()
            )
        )
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("No version reads")
        ):
            self.assertTrue(self.store.adopt_guest_capture_credentials())
            self.assertFalse(self.store.adopt_guest_capture_credentials())
        for record in self.store.list_secret_records():
            events = self.store.list_secret_audit_events(secret_id=record.secret_id)
            adoption = next(event for event in events if event.event_type == "relabelled")
            self.assertEqual(adoption.metadata["source_context"], self.binding.request.context)

    def test_duplicate_guest_credentials_are_never_arbitrarily_adopted(self) -> None:
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
        self.assertFalse(self.store.adopt_guest_capture_credentials())
        with self.assertRaises(ValueError):
            resolve_guest_capture_values(self.store, self.binding)

    def test_both_rotation_writers_preserve_guest_and_binding_identity(self) -> None:
        self.adopt()
        original = next(r for r in self.store.list_secret_records() if "PRIVATE_KEY" in r.name)
        bindings = self.store.list_secret_bindings(integration=GUEST_CAPTURE_INTEGRATION)
        key = "PRODUCTION_BACKUP_SSH_PRIVATE_KEY"
        plan = _plan_product_config_secret_write(
            record_store=self.store,
            scope=original.scope,
            integration=original.integration,
            name=original.name,
            binding_key=key,
            plaintext_value="rotated-fixture",
            context_name=original.context,
            instance_name=original.instance,
        )
        self.assertEqual(plan["secret_records"][0].capture_boundary, original.capture_boundary)
        expected = next(b for b in bindings if b.secret_id == original.secret_id)
        self.assertEqual(plan["configured_binding"].binding_id, expected.binding_id)
        secrets.write_secret_value(
            record_store=self.store,
            scope=original.scope,
            integration=original.integration,
            name=original.name,
            binding_key=key,
            plaintext_value="rotated-fixture",
            context_name=original.context,
            instance_name=original.instance,
        )
        self.assertEqual(
            self.store.read_secret_record(original.secret_id).capture_boundary,
            original.capture_boundary,
        )
        self.assertEqual(
            {b.binding_id for b in self.store.list_secret_bindings()},
            {b.binding_id for b in bindings},
        )
        self.assertEqual(
            resolve_guest_capture_values(self.store, self.binding)[key], "rotated-fixture"
        )
