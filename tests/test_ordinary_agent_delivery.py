from __future__ import annotations

from pathlib import Path
from typing import cast
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentEnrollmentCompareWriteResult,
)
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentIdentity,
    OrdinaryAgentToken,
    generate_receiver_claim_secret,
    parse_ordinary_agent_token,
)
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentDeliveryRow,
    LaunchplaneOrdinaryAgentDeliveryAuditRow,
    PostgresRecordStore,
)
from tests.support.ordinary_agent_lifecycle import (
    TEST_CLAIM_SECRET,
    TEST_ISSUER_KEY,
    apply_test_enrollment,
    enrollment_envelope,
    enrollment_mutation,
    prepare_test_issuance,
    replace_policy_without_ordinary_agent_rule,
    revocation_envelope,
    rotation_envelope,
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

    def enroll(self) -> OrdinaryAgentEnrollmentCompareWriteResult:
        return self.store.compare_and_apply_ordinary_agent_enrollment(
            envelope=self.prepared,
            mutation=enrollment_mutation(self.prepared),
            issuance=self.bundle,
        )

    def claim(self) -> OrdinaryAgentToken | None:
        return self.store.claim_ordinary_agent_credential(
            operation_id=self.envelope.operation_id, claim_secret=TEST_CLAIM_SECRET
        )

    def verify(self, token: OrdinaryAgentToken | None) -> OrdinaryAgentIdentity | None:
        assert token is not None
        return self.store.verify_ordinary_agent_token(parse_ordinary_agent_token(token.value))

    def test_randomized_retry_recovers_committed_token_and_bad_proof_cannot_consume(self) -> None:
        written = self.enroll()
        alternate, alternate_bundle = prepare_test_issuance(self.envelope)
        self.assertNotEqual(self.bundle.token.value, alternate_bundle.token.value)
        replay = self.store.compare_and_apply_ordinary_agent_enrollment(
            envelope=alternate, mutation=enrollment_mutation(alternate), issuance=alternate_bundle
        )
        self.assertEqual(replay.receipt, written.receipt)
        denied = self.store.claim_ordinary_agent_credential(
            operation_id=self.envelope.operation_id, claim_secret=generate_receiver_claim_secret()
        )
        self.assertIsNone(denied)
        self.decrypt.assert_not_called()
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentDeliveryRow, self.envelope.operation_id)
            assert row is not None
            self.assertEqual(row.delivery_status, "never_attempted")
            self.assertIsNotNone(row.ciphertext)
            event = session.scalar(select(LaunchplaneOrdinaryAgentDeliveryAuditRow))
            assert event is not None
            self.assertEqual(event.event, "invalid_receiver_proof")
        token = self.claim()
        self.assertEqual(token, self.bundle.token)
        self.assertEqual(self.claim(), token)
        self.assertIsNotNone(self.verify(token))
        self.assertIsNone(self.verify(alternate_bundle.token))

    def test_enrollment_rejects_missing_or_inconsistent_issuer_bundle(self) -> None:
        with self.assertRaisesRegex(ValueError, "issuance bundle"):
            self.store.compare_and_apply_ordinary_agent_enrollment(
                envelope=self.prepared, mutation=enrollment_mutation(self.prepared)
            )
        other, other_bundle = prepare_test_issuance(self.envelope)
        with self.assertRaisesRegex(ValueError, "reviewed envelope"):
            self.store.compare_and_apply_ordinary_agent_enrollment(
                envelope=self.prepared,
                mutation=enrollment_mutation(self.prepared),
                issuance=other_bundle,
            )
        self.assertIsNone(
            self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        )

    def test_expiry_distinguishes_unclaimed_from_attempted_and_replay_is_terminal(self) -> None:
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                # Independent fixture per branch so both have the same authoritative prestate.
                if attempted:
                    self.setUp()
                written = self.enroll()
                if attempted:
                    self.assertIsNotNone(self.claim())
                with patch.object(
                    self.store,
                    "_ordinary_agent_database_epoch",
                    return_value=self.envelope.delivery.expires_at + 1,
                ):
                    self.assertEqual(self.store.expire_ordinary_agent_deliveries(), 1)
                    replay = self.store.compare_and_apply_ordinary_agent_enrollment(
                        envelope=self.prepared, mutation=enrollment_mutation(self.prepared)
                    )
                    self.assertEqual(replay.receipt, written.receipt)
                    self.assertEqual(
                        replay.delivery_status,
                        "expired_after_attempt" if attempted else "delivery_expired_unclaimed",
                    )
                    self.assertIsNone(self.claim())
                    self.assertEqual(self.verify(self.bundle.token) is not None, attempted)
                principal = self.store.read_current_ordinary_agent_principal(
                    principal_id="agent_one"
                )
                assert principal is not None
                self.assertEqual(principal.status, "active")
                self.assertEqual(self.store.ordinary_agent_delivery_key_usage(), {})
                credential = self.store.read_ordinary_agent_authentication_credential(
                    credential_id="agent_one_auth", credential_version=1
                )
                assert credential is not None
                self.assertEqual(credential.status, "active" if attempted else "revoked")

    def test_rotation_after_unclaimed_expiry_and_revocation_cancel_delivery_without_agent_rule(
        self,
    ) -> None:
        written = self.enroll()
        with patch.object(
            self.store,
            "_ordinary_agent_database_epoch",
            return_value=self.envelope.delivery.expires_at + 1,
        ):
            self.store.expire_ordinary_agent_deliveries()
        principal = written.current_principal
        assert principal is not None
        rotated = rotation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
            custody_record_id=principal.custody_record_id,
            custody_sha256=principal.custody_sha256,
        )
        result = apply_test_enrollment(
            self.store, envelope=rotated, mutation=enrollment_mutation(rotated)
        )
        self.assertEqual(result.status, "written")
        self.assertIsNone(self.verify(self.bundle.token))
        replacement = replace_policy_without_ordinary_agent_rule(self.store, current=self.policy)
        principal = result.current_principal
        assert principal is not None
        revoke = revocation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
        )
        revoke = revoke.model_copy(
            update={
                "administrator": revoke.administrator.model_copy(
                    update={
                        "policy_record_id": replacement.record_id,
                        "policy_revision": replacement.revision,
                        "policy_sha256": replacement.policy_sha256,
                        "policy_source": replacement.source,
                    }
                )
            }
        )
        result = apply_test_enrollment(
            self.store, envelope=revoke, mutation=enrollment_mutation(revoke)
        )
        self.assertEqual(result.status, "written")
        self.assertEqual(self.store.ordinary_agent_delivery_key_usage(), {})
        self.assertIsNone(
            self.store.claim_ordinary_agent_credential(
                operation_id=rotated.operation_id, claim_secret=TEST_CLAIM_SECRET
            )
        )

    def test_legacy_marker_matching_known_token_cannot_verify_but_can_rotate(self) -> None:
        written = self.enroll()
        with self.store._session_factory() as session:
            session.execute(delete(LaunchplaneOrdinaryAgentDeliveryRow))
            session.commit()
        self.assertIsNone(self.verify(self.bundle.token))
        principal = written.current_principal
        assert principal is not None
        rotated = rotation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
            custody_record_id=principal.custody_record_id,
            custody_sha256=principal.custody_sha256,
        )
        result = apply_test_enrollment(
            self.store, envelope=rotated, mutation=enrollment_mutation(rotated)
        )
        self.assertEqual(result.status, "written")
        token = self.store.claim_ordinary_agent_credential(
            operation_id=rotated.operation_id, claim_secret=TEST_CLAIM_SECRET
        )
        self.assertIsNotNone(self.verify(token))

    def test_revoke_between_decryption_and_final_recheck_never_emits(self) -> None:
        written = self.enroll()
        principal = written.current_principal
        assert principal is not None
        revoke = revocation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
        )

        def decrypt_then_revoke(ciphertext: str, key_id: str) -> str:
            plaintext = Fernet(TEST_ISSUER_KEY).decrypt(ciphertext.encode()).decode()
            result = apply_test_enrollment(
                self.store, envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(result.status, "written")
            return plaintext

        self.decrypt.side_effect = decrypt_then_revoke
        self.assertIsNone(self.claim())
        self.assertIsNone(self.verify(self.bundle.token))

    def test_retained_delivery_blocks_key_retirement_until_ciphertext_cleanup(self) -> None:
        from control_plane.secrets import KeyRing, reencrypt_secrets

        self.enroll()
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
                self.store.expire_ordinary_agent_deliveries()
            after = reencrypt_secrets(record_store=self.store)
            self.assertIn("test-issuer", cast(list[str], after["retirement_ready_key_ids"]))
            self.assertNotEqual(before["plan_digest"], after["plan_digest"])

    def test_unrelated_policy_revision_preserves_delivery_but_removed_rule_denies_claim(
        self,
    ) -> None:
        from control_plane.contracts.authz_policy_record import build_authz_policy_record_id

        self.enroll()
        replacement = self.policy.model_copy(
            update={
                "revision": self.policy.revision + 1,
                "record_id": build_authz_policy_record_id(
                    revision=self.policy.revision + 1, policy_sha256=self.policy.policy_sha256
                ),
            }
        )
        self.store._write_row(
            self.store._authz_policy_row(self.policy.model_copy(update={"status": "superseded"}))
        )
        self.store._write_row(self.store._authz_policy_row(replacement))
        token = self.claim()
        self.assertIsNotNone(token)
        replace_policy_without_ordinary_agent_rule(self.store, current=replacement)
        self.assertIsNone(self.claim())
        # Authentication does not silently acquire or cache policy permission.
        self.assertIsNotNone(self.verify(token))
        with patch.object(
            self.store,
            "_ordinary_agent_database_epoch",
            return_value=self.bundle.candidate.expires_at,
        ):
            self.assertIsNone(self.verify(token))

    def test_read_only_principal_uses_same_identity_only_authentication(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'readonly.db'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store, actions=("self_read",))
            envelope = enrollment_envelope(policy_record=policy, inventory=inventory)
            result = apply_test_enrollment(
                store, envelope=envelope, mutation=enrollment_mutation(envelope)
            )
            assert result.current_principal is not None
            self.assertEqual(result.current_principal.execution_profile, "read_only")
            token = store.claim_ordinary_agent_credential(
                operation_id=envelope.operation_id, claim_secret=TEST_CLAIM_SECRET
            )
            assert token is not None
            identity = store.verify_ordinary_agent_token(parse_ordinary_agent_token(token.value))
            self.assertEqual(
                identity,
                OrdinaryAgentIdentity(
                    principal_id=envelope.principal_id,
                    credential_id="agent_one_auth",
                    credential_version=1,
                ),
            )

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
            self.enroll()  # Capsule was prepared under the still-loaded old key.
            replay = replay_rotation()
            self.assertTrue(replay["recovered"])
            self.assertEqual(replay["rotated_count"], applied["rotated_count"])
            self.assertEqual(replay["plan_digest"], applied["plan_digest"])
            self.assertIn("test-issuer", cast(list[str], replay["retirement_blocked_key_ids"]))
            self.assertNotIn("test-issuer", cast(list[str], replay["retirement_ready_key_ids"]))
