from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.guest_capture_credentials import (
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

    def test_duplicate_guest_credentials_refuse_without_decrypting_any_pair(self) -> None:
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
        with (
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=AssertionError("No decrypt"),
            ),
            self.assertRaises(ValueError),
        ):
            resolve_guest_capture_values(self.store, self.binding)

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
