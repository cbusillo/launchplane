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


class OrdinaryAgentDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        import time

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'db.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.issuer_key = Fernet.generate_key()
        now = int(time.time())
        payload = {
            "schema_version": 1,
            "record_id": "legacy-authentication-record",
            "principal_id": "agent_one",
            "credential_id": "legacy-credential",
            "credential_version": 1,
            "credential_digest": "a" * 64,
            "status": "active",
            "valid_from": now - 100,
            "expires_at": now + 3600,
            "revoked_at": None,
            "issuance_evidence_sha256": "b" * 64,
            "recorded_at": "2026-09-08T00:00:00Z",
            "policy": {
                "record_id": "legacy-policy",
                "revision": 1,
                "policy_sha256": "c" * 64,
                "managed_set_id": "legacy-set",
                "managed_rule_id": "legacy-rule",
                "target": {
                    "repository_id": 123,
                    "repository": "example/repo",
                    "base_branch": "main",
                },
            },
        }
        self.credential = OrdinaryAgentAuthenticationCredentialRecord.model_validate(
            {**payload, "record_sha256": canonical_json_sha256(payload)}
        )
        self.delivery = LaunchplaneOrdinaryAgentDeliveryRow(
            operation_id="legacy-enrollment",
            principal_id=self.credential.principal_id,
            credential_id=self.credential.credential_id,
            credential_version=self.credential.credential_version,
            credential_digest=self.credential.credential_digest,
            issuance_evidence_sha256=self.credential.issuance_evidence_sha256,
            intent_sha256="d" * 64,
            receiver_claim_sha256="e" * 64,
            ciphertext=Fernet(self.issuer_key).encrypt(b"historical test capsule").decode(),
            ciphertext_sha256="f" * 64,
            key_id="test-issuer",
            delivery_expires_at=now + 600,
            delivery_status="never_attempted",
        )

    def seed_legacy_delivery(self) -> None:
        credential = self.credential
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
        self.store._write_row(self.delivery)

    def test_privileged_worker_cleanup_preserves_key_retirement_of_legacy_ciphertext(self) -> None:
        from control_plane.secrets import KeyRing, reencrypt_secrets
        from control_plane.cli import main
        from click.testing import CliRunner

        self.seed_legacy_delivery()
        key_ring = KeyRing(
            active_key_id="new-key",
            keys={"new-key": Fernet(Fernet.generate_key()), "test-issuer": Fernet(self.issuer_key)},
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
                return_value=self.delivery.delivery_expires_at + 1,
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
                "test-issuer": Fernet(self.issuer_key),
            },
            active_hmac_key=b"h" * 32,
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

    def test_cleanup_failure_redacts_database_detail_and_chained_parameters(self) -> None:
        import traceback
        from sqlalchemy.exc import DBAPIError
        from control_plane.storage.postgres import OrdinaryAgentPersistenceError

        private_detail = "synthetic-private-capsule-value"
        failure = DBAPIError(
            "SELECT private_payload",
            {"capsule": private_detail},
            RuntimeError(private_detail),
        )
        with patch.object(self.store, "_session_factory", side_effect=failure):
            try:
                self.store.expire_ordinary_agent_deliveries()
            except OrdinaryAgentPersistenceError as error:
                rendered = "".join(traceback.format_exception(error))
                self.assertNotIn(private_detail, rendered)
                self.assertTrue(error.__suppress_context__)
            else:
                self.fail("Database cleanup failure was not reported")

    def test_expiry_preserves_credential_revocation_session_cancellation_and_unknown_custody(
        self,
    ) -> None:
        from sqlalchemy import select
        from control_plane.contracts.ordinary_agent_session_lifecycle import (
            OrdinaryAgentSessionRecord,
            OrdinaryAgentLeaseRecord,
            OrdinaryAgentFiniteRequestRecord,
        )
        from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentEffectRecord
        from control_plane.storage.postgres import (
            LaunchplaneOrdinaryAgentSessionRow,
            LaunchplaneOrdinaryAgentLeaseRow,
            LaunchplaneOrdinaryAgentFiniteRequestRow,
            LaunchplaneOrdinaryAgentEffectRow,
            LaunchplaneOrdinaryAgentCustodyIssueAttemptRow,
            LaunchplaneOrdinaryAgentEffectCustodyRow,
        )

        for attempted, unknown_custody, request_state in (
            (False, False, "waiting"),
            (False, True, "waiting"),
            (True, True, "waiting"),
            (False, True, "reconciliation_required"),
            (False, False, "completed"),
        ):
            with self.subTest(
                attempted=attempted, unknown_custody=unknown_custody, request_state=request_state
            ):
                if attempted or unknown_custody or request_state != "waiting":
                    self.setUp()
                self.seed_legacy_delivery()
                candidate = self.credential
                start, expiry = candidate.valid_from, candidate.expires_at
                target = self.credential.policy.target
                session_record = OrdinaryAgentSessionRecord.model_validate(
                    {
                        "session_id": "legacy-session",
                        "principal_id": candidate.principal_id,
                        "credential_id": candidate.credential_id,
                        "credential_version": 1,
                        "credential_digest": candidate.credential_digest,
                        "valid_from": start,
                        "expires_at": expiry,
                        "delegation": {
                            "operation_id": "legacy-approval",
                            "approval_sha256": "a" * 64,
                            "receiver_sha256": "b" * 64,
                            "actions": ["guarded_merge"],
                            "session_expires_at": expiry,
                            "lease_expires_at": expiry,
                            "action_limit": 1,
                            "pull_request_limit": 1,
                            "refresh_allowance": 0,
                        },
                    }
                )
                lease = OrdinaryAgentLeaseRecord.model_validate(
                    {
                        "lease_id": "legacy-lease",
                        "session_id": session_record.session_id,
                        "principal_id": candidate.principal_id,
                        "target": target.model_dump(),
                        "action": "guarded_merge",
                        "managed_set_id": "legacy-set",
                        "managed_rule_id": "legacy-rule",
                        "effective_decision_fingerprint": "oae-fp-v1:" + "c" * 64,
                        "valid_from": start,
                        "expires_at": expiry,
                        "budget": {
                            "window_start": start,
                            "window_end": expiry,
                            "action_limit": 1,
                            "actions_used": 0,
                            "pull_request_limit": 1,
                            "pull_requests_used": 0,
                        },
                    }
                )
                request = OrdinaryAgentFiniteRequestRecord.model_validate(
                    {
                        "status": request_state,
                        "request_id": "legacy-request",
                        "idempotency_key": "legacy-request",
                        "principal_id": candidate.principal_id,
                        "session_id": session_record.session_id,
                        "lease_id": lease.lease_id,
                        "target": target.model_dump(),
                        "base_sha": "a" * 40,
                        "pull_requests": [{"number": 7, "head_sha": "b" * 40}],
                        "permitted_stack_edit_pull_requests": [],
                        "refresh_allowance_total": 0,
                        "admitted_at": start,
                        "expires_at": expiry,
                    }
                )
                effect = OrdinaryAgentEffectRecord.model_validate(
                    {
                        "effect_id": "legacy-effect",
                        "request_id": request.request_id,
                        "session_id": session_record.session_id,
                        "lease_id": lease.lease_id,
                        "principal_id": candidate.principal_id,
                        "scope_sha256": request.scope_sha256,
                        "binding_revision": request.binding_revision,
                        "semantic_ordinal": 1,
                        "action_ordinal": 1,
                        "command_sha256": "d" * 64,
                        "command": {
                            "kind": "candidate_ref_delete",
                            "effect": {
                                "lineage": {
                                    "repository": target.repository,
                                    "base_branch": target.base_branch,
                                    "batch_id": "legacy-batch",
                                },
                                "candidate_ref": "refs/heads/legacy-candidate",
                            },
                        },
                        "target": target.model_dump(),
                        "controller_fence": {
                            "controller_key": "legacy-controller",
                            "lease_owner": "legacy-worker",
                            "lease_acquired_at": "legacy-acquisition",
                        },
                        "policy_record_id": self.credential.policy.record_id,
                        "policy_revision": self.credential.policy.revision,
                        "policy_sha256": self.credential.policy.policy_sha256,
                        "credential_id": candidate.credential_id,
                        "credential_version": 1,
                        "credential_digest": candidate.credential_digest,
                        "reserved_at": start,
                        "updated_at": start,
                    }
                )
                with self.store._session_factory() as db:
                    delivery = db.get(
                        LaunchplaneOrdinaryAgentDeliveryRow, self.delivery.operation_id
                    )
                    assert delivery is not None
                    delivery.delivery_status = (
                        "delivery_attempted" if attempted else "never_attempted"
                    )
                    db.add(
                        LaunchplaneOrdinaryAgentSessionRow(
                            session_id=session_record.session_id,
                            operation_id=session_record.delegation.operation_id,
                            principal_id=candidate.principal_id,
                            credential_id=candidate.credential_id,
                            credential_version=1,
                            payload=session_record.model_dump(mode="json"),
                        )
                    )
                    db.add(
                        LaunchplaneOrdinaryAgentLeaseRow(
                            lease_id=lease.lease_id,
                            session_id=lease.session_id,
                            revision=lease.revision,
                            payload=lease.model_dump(mode="json"),
                        )
                    )
                    db.add(
                        LaunchplaneOrdinaryAgentFiniteRequestRow(
                            request_id=request.request_id,
                            principal_id=request.principal_id,
                            session_id=request.session_id,
                            lease_id=request.lease_id,
                            idempotency_key=request.idempotency_key,
                            intent_sha256="e" * 64,
                            payload=request.model_dump(mode="json"),
                        )
                    )
                    db.add(
                        LaunchplaneOrdinaryAgentEffectRow(
                            effect_id=effect.effect_id,
                            request_id=effect.request_id,
                            lease_id=effect.lease_id,
                            scope_sha256=effect.scope_sha256,
                            binding_revision=effect.binding_revision,
                            action_ordinal=effect.action_ordinal,
                            semantic_key="legacy",
                            command_sha256=effect.command_sha256,
                            revision=effect.revision,
                            payload=effect.model_dump(mode="json"),
                        )
                    )
                    if unknown_custody:
                        db.add(
                            LaunchplaneOrdinaryAgentCustodyIssueAttemptRow(
                                attempt_id="legacy-custody",
                                idempotency_key_sha256="f" * 64,
                                request_sha256="e" * 64,
                                principal_id=candidate.principal_id,
                                repository_id=target.repository_id,
                                state="issue_unknown",
                                mint_started_at=self.credential.recorded_at,
                                dispatch_deadline=self.credential.recorded_at,
                                updated_at=self.credential.recorded_at,
                                payload={},
                            )
                        )
                        db.add(
                            LaunchplaneOrdinaryAgentEffectCustodyRow(
                                attempt_id="legacy-custody",
                                effect_id=effect.effect_id,
                                purpose="dispatch",
                                payload={},
                            )
                        )
                    db.commit()
                now = self.delivery.delivery_expires_at + 1
                with patch.object(self.store, "_ordinary_agent_database_epoch", return_value=now):
                    self.assertEqual(self.store.expire_ordinary_agent_deliveries(), 1)
                    self.assertEqual(self.store.expire_ordinary_agent_deliveries(), 0)
                with self.store._session_factory() as db:
                    credential = db.scalar(
                        select(LaunchplaneOrdinaryAgentAuthenticationCredentialRow)
                    )
                    persisted_session = db.get(
                        LaunchplaneOrdinaryAgentSessionRow, session_record.session_id
                    )
                    persisted_lease = db.get(LaunchplaneOrdinaryAgentLeaseRow, lease.lease_id)
                    persisted_request = db.get(
                        LaunchplaneOrdinaryAgentFiniteRequestRow, request.request_id
                    )
                    persisted_effect = db.get(LaunchplaneOrdinaryAgentEffectRow, effect.effect_id)
                    assert (
                        credential
                        and persisted_session
                        and persisted_lease
                        and persisted_request
                        and persisted_effect
                    )
                    self.assertEqual(
                        credential.lifecycle_status, "active" if attempted else "revoked"
                    )
                    self.assertEqual(
                        persisted_session.payload["revoked_at"], None if attempted else now
                    )
                    self.assertEqual(
                        persisted_lease.payload["revoked_at"], None if attempted else now
                    )
                    self.assertEqual(
                        persisted_request.payload["status"],
                        request_state
                        if attempted or request_state in {"reconciliation_required", "completed"}
                        else "cancelled",
                    )
                    self.assertEqual(
                        persisted_effect.payload["state"],
                        "reserved"
                        if attempted
                        else "reconciliation_required"
                        if unknown_custody
                        else "not_dispatched",
                    )
                    persisted_delivery = db.get(
                        LaunchplaneOrdinaryAgentDeliveryRow, self.delivery.operation_id
                    )
                    assert persisted_delivery is not None
                    self.assertIsNone(persisted_delivery.ciphertext)
