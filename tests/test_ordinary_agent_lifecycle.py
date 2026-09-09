from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentEnrollmentReceipt,
)
from control_plane.ordinary_agent_lifecycle import lifecycle_record_sha256_from_payload
from control_plane.storage.postgres import (
    LaunchplaneIdempotencyRow,
    LaunchplaneOrdinaryAgentAuthenticationCredentialRow,
    LaunchplaneOrdinaryAgentCredentialCustodyRow,
    LaunchplaneOrdinaryAgentLifecycleAuditRow,
    LaunchplaneOrdinaryAgentPrincipalRow,
    PostgresRecordStore,
)
from tests.support.ordinary_agent_lifecycle import (
    enrollment_envelope,
    enrollment_mutation,
    revocation_envelope,
    rotation_envelope,
    replace_policy_without_ordinary_agent_rule,
    setup_ordinary_agent_authority,
)


class FailingOrdinaryAgentStore(PostgresRecordStore):
    def __init__(self, *, database_url: str, fail_step: str) -> None:
        self.fail_step = fail_step
        super().__init__(database_url=database_url)

    def _after_ordinary_agent_enrollment_write_step(self, step_name: str) -> None:
        if step_name == self.fail_step:
            raise RuntimeError(f"injected ordinary-agent failure after {step_name}")


class OrdinaryAgentLifecycleStorageTests(unittest.TestCase):
    def _database_url(self, directory: str) -> str:
        return f"sqlite+pysqlite:///{Path(directory) / 'launchplane.sqlite3'}"

    def test_enroll_replays_exact_receipt_and_changed_payload_conflicts(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            envelope = enrollment_envelope(policy_record=policy, inventory=inventory)
            mutation = enrollment_mutation(envelope)

            denied_envelope = envelope.model_copy(
                update={
                    "administrator": envelope.administrator.model_copy(
                        update={"administrator_github_id": 987654321}
                    )
                }
            )
            denied = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=denied_envelope, mutation=enrollment_mutation(denied_envelope)
            )
            self.assertEqual(denied.status, "administrator_denied")
            self.assertIsNone(store.read_current_ordinary_agent_principal(principal_id="agent_one"))

            written = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=envelope, mutation=mutation
            )
            replayed = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=envelope, mutation=mutation
            )
            changed_envelope = envelope.model_copy(update={"plan_sha256": "9" * 64})
            conflict = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=changed_envelope, mutation=enrollment_mutation(changed_envelope)
            )

            self.assertEqual(written.status, "written")
            self.assertEqual(replayed.status, "replayed")
            self.assertEqual(replayed.receipt, written.receipt)
            self.assertEqual(conflict.status, "idempotency_conflict")
            self.assertIsInstance(written.receipt, OrdinaryAgentEnrollmentReceipt)
            assert written.receipt is not None
            self.assertEqual(
                store.read_current_ordinary_agent_principal(principal_id="agent_one"),
                written.current_principal,
            )
            self.assertIsNotNone(
                store.read_ordinary_agent_credential_custody(
                    record_id=written.receipt.custody_record_id or ""
                )
            )
            self.assertIsNotNone(
                store.read_ordinary_agent_lifecycle_audit(operation_id=envelope.operation_id)
            )
            with store._engine.connect() as connection:
                persisted_payloads = tuple(
                    str(payload)
                    for table in (
                        LaunchplaneOrdinaryAgentPrincipalRow,
                        LaunchplaneOrdinaryAgentAuthenticationCredentialRow,
                        LaunchplaneOrdinaryAgentCredentialCustodyRow,
                        LaunchplaneOrdinaryAgentLifecycleAuditRow,
                    )
                    for payload in connection.execute(select(table.payload)).scalars()
                )
            self.assertFalse(
                any("encrypted-test-placeholder" in payload for payload in persisted_payloads)
            )
            store.close()

    def test_rotate_advances_both_current_records_and_revoke_needs_no_custody_read(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            enroll = enrollment_envelope(policy_record=policy, inventory=inventory)
            enrolled = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=enroll, mutation=enrollment_mutation(enroll)
            )
            assert enrolled.receipt is not None
            rotation = rotation_envelope(
                enrolled=enroll,
                principal_record_id=enrolled.receipt.principal_record_id,
                principal_revision=enrolled.receipt.principal_revision,
                principal_sha256=enrolled.receipt.principal_sha256,
                custody_record_id=enrolled.receipt.custody_record_id or "",
                custody_sha256=enrolled.receipt.custody_sha256 or "",
            )
            stale_custody = rotation.model_copy(
                update={
                    "custody": rotation.custody.model_copy(update={"predecessor_sha256": "9" * 64})
                }
            )
            rejected = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=stale_custody, mutation=enrollment_mutation(stale_custody)
            )
            self.assertEqual(rejected.status, "custody_drift")
            self.assertEqual(
                store.read_current_ordinary_agent_principal(principal_id="agent_one"),
                enrolled.current_principal,
            )
            rotated = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=rotation, mutation=enrollment_mutation(rotation)
            )
            assert rotated.receipt is not None
            self.assertEqual(rotated.receipt.principal_revision, 2)
            self.assertEqual(rotated.receipt.credential_version, 2)
            previous_credential = store.read_ordinary_agent_authentication_credential(
                credential_id=rotation.credential_id,
                credential_version=1,
            )
            assert previous_credential is not None
            self.assertEqual(previous_credential.status, "superseded")
            rotation_audit = store.read_ordinary_agent_lifecycle_audit(
                operation_id=rotation.operation_id
            )
            assert rotation_audit is not None
            self.assertEqual(
                rotation_audit.previous_credential_record_id,
                previous_credential.record_id,
            )
            self.assertEqual(
                rotation_audit.previous_credential_sha256,
                enrolled.receipt.credential_sha256,
            )

            with store._session_factory() as session:
                session.query(LaunchplaneOrdinaryAgentCredentialCustodyRow).delete()
                session.commit()
            replacement_policy = replace_policy_without_ordinary_agent_rule(
                store,
                current=policy,
            )
            revoke = revocation_envelope(
                enrolled=enroll,
                principal_record_id=rotated.receipt.principal_record_id,
                principal_revision=rotated.receipt.principal_revision,
                principal_sha256=rotated.receipt.principal_sha256,
            )
            revoke = revoke.model_copy(
                update={
                    "administrator": revoke.administrator.model_copy(
                        update={
                            "policy_record_id": replacement_policy.record_id,
                            "policy_revision": replacement_policy.revision,
                            "policy_schema_version": replacement_policy.policy.schema_version,
                            "policy_sha256": replacement_policy.policy_sha256,
                            "policy_source": replacement_policy.source,
                        }
                    )
                }
            )
            revoked = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(revoked.status, "written")
            assert revoked.receipt is not None
            assert revoked.current_principal is not None
            self.assertIsNone(revoked.receipt.custody_record_id)
            self.assertEqual(revoked.current_principal.status, "revoked")
            current_credential = store.read_ordinary_agent_authentication_credential(
                credential_id=rotation.credential_id,
                credential_version=2,
            )
            assert current_credential is not None
            self.assertEqual(current_credential.status, "revoked")
            store.close()

    def test_revoke_missing_authentication_credential_records_absence_and_replays(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            enroll = enrollment_envelope(policy_record=policy, inventory=inventory)
            enrolled = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=enroll, mutation=enrollment_mutation(enroll)
            )
            assert enrolled.receipt is not None
            with store._session_factory() as session:
                session.query(LaunchplaneOrdinaryAgentAuthenticationCredentialRow).delete()
                session.commit()
            revoke = revocation_envelope(
                enrolled=enroll,
                principal_record_id=enrolled.receipt.principal_record_id,
                principal_revision=enrolled.receipt.principal_revision,
                principal_sha256=enrolled.receipt.principal_sha256,
            )
            revoked = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(revoked.status, "written")
            assert revoked.current_principal is not None
            assert revoked.receipt is not None
            self.assertEqual(revoked.current_principal.status, "revoked")
            self.assertIsNone(revoked.receipt.credential_sha256)
            audit = store.read_ordinary_agent_lifecycle_audit(operation_id=revoke.operation_id)
            assert audit is not None
            self.assertIsNone(audit.previous_credential_record_id)
            self.assertIsNone(audit.resulting_credential_record_id)
            replayed = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(replayed.status, "replayed")
            self.assertEqual(replayed.receipt, revoked.receipt)
            store.close()

    def test_revoke_skips_misbound_credential_without_mutating_it_and_replays(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            enroll = enrollment_envelope(policy_record=policy, inventory=inventory)
            enrolled = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=enroll, mutation=enrollment_mutation(enroll)
            )
            assert enrolled.receipt is not None
            with store._session_factory() as session:
                row = session.query(LaunchplaneOrdinaryAgentAuthenticationCredentialRow).one()
                # A valid but misbound record must not be treated as this principal's credential.
                drifted_payload = dict(row.payload)
                drifted_payload["principal_id"] = "another_agent"
                drifted_payload["credential_digest"] = "9" * 64
                drifted_payload["record_sha256"] = lifecycle_record_sha256_from_payload(
                    {key: value for key, value in drifted_payload.items() if key != "record_sha256"}
                )
                row.principal_id = "another_agent"
                row.credential_digest = "9" * 64
                row.record_sha256 = drifted_payload["record_sha256"]
                row.payload = drifted_payload
                session.commit()
            revoke = revocation_envelope(
                enrolled=enroll,
                principal_record_id=enrolled.receipt.principal_record_id,
                principal_revision=enrolled.receipt.principal_revision,
                principal_sha256=enrolled.receipt.principal_sha256,
            )
            revoked = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(revoked.status, "written")
            assert revoked.current_principal is not None
            assert revoked.receipt is not None
            self.assertEqual(revoked.current_principal.status, "revoked")
            self.assertIsNone(revoked.receipt.credential_sha256)
            with store._session_factory() as session:
                retained = session.query(LaunchplaneOrdinaryAgentAuthenticationCredentialRow).one()
                self.assertEqual(retained.payload, drifted_payload)
                self.assertEqual(retained.lifecycle_status, "active")
            audit = store.read_ordinary_agent_lifecycle_audit(operation_id=revoke.operation_id)
            assert audit is not None
            self.assertIsNone(audit.previous_credential_record_id)
            self.assertIsNone(audit.resulting_credential_record_id)
            replayed = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=revoke, mutation=enrollment_mutation(revoke)
            )
            self.assertEqual(replayed.status, "replayed")
            self.assertEqual(replayed.receipt, revoked.receipt)
            store.close()

    def test_inconsistent_provider_candidate_cannot_write_authoritative_records(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            enroll = enrollment_envelope(policy_record=policy, inventory=inventory)
            custody = enroll.custody
            candidates = (
                custody.model_copy(
                    update={
                        "managed_secret": custody.managed_secret.model_copy(
                            update={"integration": "different_app"}
                        )
                    }
                ),
                custody.model_copy(
                    update={
                        "managed_secret": custody.managed_secret.model_copy(
                            update={"binding_key": "different_key"}
                        )
                    }
                ),
                custody.model_copy(update={"effect_profiles": ("guarded_merge",)}),
                custody.model_copy(
                    update={
                        "permissions": tuple(
                            p for p in custody.permissions if p.name != "pull_requests"
                        )
                    }
                ),
            )
            for candidate in candidates:
                with self.subTest(candidate=candidate):
                    envelope = enroll.model_copy(update={"custody": candidate})
                    result = store.compare_and_apply_ordinary_agent_enrollment(
                        envelope=envelope, mutation=enrollment_mutation(envelope)
                    )
                    self.assertEqual(result.status, "custody_drift")
                    self.assertIsNone(result.idempotency_record)
                    self.assertIsNone(
                        store.read_current_ordinary_agent_principal(
                            principal_id=envelope.principal_id
                        )
                    )
                    self.assertIsNone(
                        store.read_ordinary_agent_lifecycle_audit(
                            operation_id=envelope.operation_id
                        )
                    )
            # Rejections release their reservation; the real candidate can then commit once.
            written = store.compare_and_apply_ordinary_agent_enrollment(
                envelope=enroll, mutation=enrollment_mutation(enroll)
            )
            self.assertEqual(written.status, "written")
            store.close()

    def test_exact_route_scope_and_store_derived_response_are_required(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            envelope = enrollment_envelope(policy_record=policy, inventory=inventory)
            mutation = enrollment_mutation(envelope)

            invalid_mutations = (
                replace(mutation, scope="other"),
                replace(mutation, route_path="/other"),
                replace(mutation, response_payload={"caller": "controlled"}),
            )
            for invalid in invalid_mutations:
                with self.subTest(mutation=invalid), self.assertRaises(ValueError):
                    store.compare_and_apply_ordinary_agent_enrollment(
                        envelope=envelope, mutation=invalid
                    )
            self.assertIsNone(store.read_current_ordinary_agent_principal(principal_id="agent_one"))
            store.close()

    def test_failure_after_each_domain_write_rolls_back_every_record(self) -> None:
        for step in (
            "insert_principal",
            "insert_credential",
            "insert_custody",
            "insert_audit",
            "complete_idempotency",
        ):
            with self.subTest(step=step), TemporaryDirectory() as directory:
                store = FailingOrdinaryAgentStore(
                    database_url=self._database_url(directory), fail_step=step
                )
                store.ensure_schema()
                policy, inventory = setup_ordinary_agent_authority(store)
                envelope = enrollment_envelope(policy_record=policy, inventory=inventory)
                with self.assertRaisesRegex(RuntimeError, step):
                    store.compare_and_apply_ordinary_agent_enrollment(
                        envelope=envelope, mutation=enrollment_mutation(envelope)
                    )
                with store._engine.connect() as connection:
                    counts = tuple(
                        len(tuple(connection.execute(select(table)).all()))
                        for table in (
                            LaunchplaneOrdinaryAgentPrincipalRow,
                            LaunchplaneOrdinaryAgentAuthenticationCredentialRow,
                            LaunchplaneOrdinaryAgentCredentialCustodyRow,
                            LaunchplaneOrdinaryAgentLifecycleAuditRow,
                            LaunchplaneIdempotencyRow,
                        )
                    )
                self.assertEqual(counts, (0, 0, 0, 0, 0))
                store.close()

    def test_policy_inventory_and_secret_drift_leave_no_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=self._database_url(directory))
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            base = enrollment_envelope(policy_record=policy, inventory=inventory)
            cases = (
                (
                    base.model_copy(
                        update={
                            "policy": base.policy.model_copy(update={"policy_sha256": "9" * 64}),
                            "custody": base.custody.model_copy(
                                update={
                                    "policy": base.policy.model_copy(
                                        update={"policy_sha256": "9" * 64}
                                    )
                                }
                            ),
                        }
                    ),
                    "policy_drift",
                ),
                (
                    base.model_copy(
                        update={
                            "custody": base.custody.model_copy(
                                update={
                                    "repository_inventory": base.custody.repository_inventory.model_copy(
                                        update={"inventory_digest": "9" * 64}
                                    )
                                }
                            )
                        }
                    ),
                    "inventory_drift",
                ),
                (
                    base.model_copy(
                        update={
                            "custody": base.custody.model_copy(
                                update={
                                    "managed_secret": base.custody.managed_secret.model_copy(
                                        update={"secret_version_id": "missing-version"}
                                    )
                                }
                            )
                        }
                    ),
                    "secret_drift",
                ),
            )
            for position, (candidate, expected_status) in enumerate(cases, start=1):
                candidate = candidate.model_copy(
                    update={"operation_id": f"ordinary-agent-drift-{position}"}
                )
                result = store.compare_and_apply_ordinary_agent_enrollment(
                    envelope=candidate, mutation=enrollment_mutation(candidate)
                )
                self.assertEqual(result.status, expected_status)
                self.assertIsNone(result.idempotency_record)
            self.assertIsNone(store.read_current_ordinary_agent_principal(principal_id="agent_one"))
            store.close()
