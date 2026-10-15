from __future__ import annotations

from pathlib import Path
from typing import cast
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentAuthenticationCredentialRecord,
)
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentDeliveryRow,
    LaunchplaneOrdinaryAgentAuthenticationCredentialRow,
    PostgresRecordStore,
)
from tests.support.ordinary_agent_lifecycle import (
    TEST_ISSUER_KEY,
    enrollment_envelope,
    prepare_test_issuance,
    setup_ordinary_agent_authority,
)


class OrdinaryAgentDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'db.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.policy, inventory = setup_ordinary_agent_authority(self.store)
        self.envelope = enrollment_envelope(policy_record=self.policy, inventory=inventory)
        self.prepared, self.bundle = prepare_test_issuance(self.envelope)
        self.decrypt = self.enterContext(
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=lambda ciphertext, key_id: (
                    Fernet(TEST_ISSUER_KEY).decrypt(ciphertext.encode()).decode()
                ),
            )
        )

    def test_privileged_worker_cleanup_preserves_key_retirement_of_legacy_ciphertext(self) -> None:
        from control_plane.secrets import KeyRing, reencrypt_secrets
        from control_plane.cli import main
        from click.testing import CliRunner

        self.seed_legacy_delivery()
        key_ring = KeyRing(
            active_key_id="new-key",
            keys={"new-key": Fernet(Fernet.generate_key()), "test-issuer": Fernet(TEST_ISSUER_KEY)},
            active_hmac_key=b"h" * 32,
        )
        with (
            patch("control_plane.secrets._get_key_ring", return_value=key_ring),
            patch.object(self.store, "list_secret_records", return_value=()),
        ):
            before = reencrypt_secrets(record_store=self.store)
            self.assertIn("test-issuer", cast(list[str], before["retirement_blocked_key_ids"]))
            self.assertNotIn("test-issuer", cast(list[str], before["retirement_ready_key_ids"]))
            with patch.object(
                self.store,
                "_ordinary_agent_database_epoch",
                return_value=self.envelope.delivery.expires_at + 1,
            ):
                with patch("control_plane.cli_service._store", return_value=self.store):
                    result = CliRunner().invoke(
                        main,
                        [
                            "service",
                            "privileged-operation-workers",
                            "run-once",
                            "--database-url",
                            "sqlite+pysqlite:///:memory:",
                        ],
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.store.ordinary_agent_delivery_key_usage(), {})
            after = reencrypt_secrets(record_store=self.store)
            self.assertIn("test-issuer", cast(list[str], after["retirement_ready_key_ids"]))
            self.assertNotEqual(before["plan_digest"], after["plan_digest"])

    def test_completed_key_rotation_replay_rechecks_new_capsule_retirement_usage(self) -> None:
        from control_plane.contracts.secret_record import SecretRecord, SecretVersion
        from control_plane.secrets import KeyRing, reencrypt_secrets

        new_cipher = Fernet(Fernet.generate_key())
        legacy_cipher = Fernet(Fernet.generate_key())
        ring = KeyRing(
            active_key_id="new-key",
            keys={
                "new-key": new_cipher,
                "legacy-key": legacy_cipher,
                "test-issuer": Fernet(TEST_ISSUER_KEY),
            },
            active_hmac_key=b"h" * 32,
        )
        # Custody's existing version already uses the new root, so rotating an
        # unrelated secret does not invalidate the pending enrollment CAS.
        current = self.store.read_secret_version("ordinary-agent-app-key-v1")
        self.store.write_secret_version(
            current.model_copy(
                update={
                    "key_id": "new-key",
                    "ciphertext": new_cipher.encrypt(b"test app key").decode(),
                }
            )
        )
        self.store.write_secret_version(
            SecretVersion(
                version_id="other-v1",
                secret_id="other",
                created_at="2026-09-09T00:00:00Z",
                created_by="test",
                ciphertext=legacy_cipher.encrypt(b"other value").decode(),
                key_id="legacy-key",
            )
        )
        self.store.write_secret_record(
            SecretRecord(
                secret_id="other",
                scope="global",
                integration="test",
                name="other",
                current_version_id="other-v1",
                created_at="2026-09-09T00:00:00Z",
                updated_at="2026-09-09T00:00:00Z",
                updated_by="test",
            )
        )
        with (
            patch("control_plane.secrets._get_key_ring", return_value=ring),
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=lambda ciphertext, key_id: (
                    ring.keys[key_id].decrypt(ciphertext.encode()).decode()
                ),
            ),
        ):
            plan = reencrypt_secrets(record_store=self.store)
            self.assertIn("test-issuer", cast(list[str], plan["retirement_ready_key_ids"]))

            def replay_rotation() -> dict[str, object]:
                return reencrypt_secrets(
                    record_store=self.store,
                    apply=True,
                    expected_plan_digest=cast(str, plan["plan_digest"]),
                    operation_token="test-completed-rotation",
                    actor="test",
                    reason="Rotate test secret root",
                )

            applied = replay_rotation()
            self.assertEqual(applied["status"], "ok")
            self.seed_legacy_delivery()  # Capsule was prepared under the still-loaded old key.
            replay = replay_rotation()
            self.assertTrue(replay["recovered"])
            self.assertEqual(replay["rotated_count"], applied["rotated_count"])
            self.assertEqual(replay["plan_digest"], applied["plan_digest"])
            self.assertIn("test-issuer", cast(list[str], replay["retirement_blocked_key_ids"]))
            self.assertNotIn("test-issuer", cast(list[str], replay["retirement_ready_key_ids"]))

    def seed_legacy_delivery(self) -> None:
        # Seed historical rows directly: retired enrollment is no longer executable.
        candidate = self.bundle.candidate
        payload = {
            "schema_version": 1,
            "revoked_at": None,
            "record_id": "legacy-authentication-record",
            "principal_id": candidate.principal_id,
            "credential_id": candidate.credential_id,
            "credential_version": self.bundle.credential_version,
            "status": "active",
            "credential_digest": candidate.credential_digest,
            "valid_from": candidate.valid_from,
            "expires_at": candidate.expires_at,
            "issuance_evidence_sha256": candidate.issuance_evidence_sha256,
            "policy": self.envelope.policy.model_dump(mode="json"),
            "recorded_at": self.policy.updated_at,
        }
        credential = OrdinaryAgentAuthenticationCredentialRecord.model_validate(
            {
                **payload,
                "record_sha256": canonical_json_sha256(payload),
            }
        )
        self.store._write_row(
            LaunchplaneOrdinaryAgentAuthenticationCredentialRow(
                record_id=credential.record_id,
                principal_id=credential.principal_id,
                credential_id=credential.credential_id,
                credential_version=credential.credential_version,
                lifecycle_status=credential.status,
                is_current=True,
                credential_digest=credential.credential_digest,
                recorded_at=credential.recorded_at,
                record_sha256=credential.record_sha256,
                payload=credential.model_dump(mode="json"),
            )
        )
        self.store._write_row(
            LaunchplaneOrdinaryAgentDeliveryRow(
                operation_id=self.bundle.operation_id,
                principal_id=candidate.principal_id,
                credential_id=candidate.credential_id,
                credential_version=self.bundle.credential_version,
                credential_digest=candidate.credential_digest,
                issuance_evidence_sha256=candidate.issuance_evidence_sha256,
                intent_sha256=self.bundle.intent_sha256,
                receiver_claim_sha256=self.bundle.receiver_claim_sha256,
                ciphertext=self.bundle.ciphertext,
                ciphertext_sha256=self.bundle.ciphertext_sha256,
                key_id=self.bundle.key_id,
                delivery_expires_at=self.bundle.delivery_expires_at,
                delivery_status="never_attempted",
            )
        )
