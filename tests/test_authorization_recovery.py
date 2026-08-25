from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.authorization_recovery import (
    AUTHORIZATION_RECOVERY_NAMESPACE,
    AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES,
    AuthorizationRecoveryAudit,
    AuthorizationRecoveryApplyRequest,
    AuthorizationRecoveryApplyResult,
    AuthorizationBootstrapState,
    AuthorizationRecoveryChallenge,
    AuthorizationRecoveryKey,
    AuthorizationRecoveryService,
    _verify_sshsig,
    canonicalize_recovery_public_key,
    recovery_key_fingerprint,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy


_HARDWARE_KEY_TYPE = "sk-ssh-ed25519@openssh.com"


def _hardware_key(material: str) -> str:
    key_type = _HARDWARE_KEY_TYPE.encode("ascii")
    key_blob = len(key_type).to_bytes(4, "big") + key_type + material.encode("utf-8")
    return f"{_HARDWARE_KEY_TYPE} {base64.b64encode(key_blob).decode('ascii')} recovery@example"


_HARDWARE_KEY = _hardware_key("fixture-material-for-parser-only")


def _record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(schema_version=2)
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test",
        updated_at="2026-08-25T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


class _Store:
    def __init__(self) -> None:
        self.keys: dict[str, AuthorizationRecoveryKey] = {}
        self.challenges: dict[str, AuthorizationRecoveryChallenge] = {}
        self.audits: list[AuthorizationRecoveryAudit] = []
        self.state: AuthorizationBootstrapState | None = None
        self.records = (_record(),)
        self.outbox_deliveries: list[OutboxDeliveryRecord] = []

    def read_authorization_bootstrap_state(self) -> AuthorizationBootstrapState | None:
        return self.state

    def complete_authorization_bootstrap_once(self, state: AuthorizationBootstrapState) -> bool:
        if self.state is not None and self.state.status == "complete" and state != self.state:
            return False
        self.state = state
        return True

    def list_authorization_recovery_keys(self) -> tuple[AuthorizationRecoveryKey, ...]:
        return tuple(self.keys.values())

    def read_authorization_recovery_key(self, key_id: str) -> AuthorizationRecoveryKey | None:
        return self.keys.get(key_id)

    def write_authorization_recovery_key(self, key: AuthorizationRecoveryKey) -> None:
        self.keys[key.key_id] = key

    def read_authorization_recovery_challenge(
        self, challenge_id: str
    ) -> AuthorizationRecoveryChallenge | None:
        return self.challenges.get(challenge_id)

    def list_authorization_recovery_challenges(
        self,
    ) -> tuple[AuthorizationRecoveryChallenge, ...]:
        return tuple(self.challenges.values())

    def write_authorization_recovery_challenge(
        self, challenge: AuthorizationRecoveryChallenge
    ) -> None:
        self.challenges[challenge.challenge_id] = challenge

    def consume_authorization_recovery_challenge(self, *, challenge_id: str, used_at: str) -> bool:
        challenge = self.challenges.get(challenge_id)
        if challenge is None or challenge.used_at:
            return False
        self.challenges[challenge_id] = challenge.model_copy(update={"used_at": used_at})
        return True

    def write_authorization_recovery_audit(self, audit: AuthorizationRecoveryAudit) -> None:
        if all(existing.audit_id != audit.audit_id for existing in self.audits):
            self.audits.append(audit)

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = tuple(record for record in self.records if not status or record.status == status)
        return records if limit is None else records[:limit]

    def apply_authorization_recovery(
        self, request: AuthorizationRecoveryApplyRequest
    ) -> AuthorizationRecoveryApplyResult:
        challenge = self.challenges.get(request.challenge.challenge_id)
        if challenge is None:
            return AuthorizationRecoveryApplyResult(status="challenge_unavailable")
        if challenge.used_at:
            return AuthorizationRecoveryApplyResult(status="replayed", challenge=challenge)
        if challenge != request.challenge:
            return AuthorizationRecoveryApplyResult(status="conflict", challenge=challenge)
        signing_key = self.keys.get(request.signing_key_id)
        if signing_key is None or signing_key.status != "active":
            return AuthorizationRecoveryApplyResult(
                status="signing_key_unavailable", challenge=challenge
            )
        active = tuple(key for key in self.keys.values() if key.status == "active")
        if len({key.custody_slot for key in active}) < 2:
            return AuthorizationRecoveryApplyResult(
                status="custody_not_independent", challenge=challenge
            )
        active_record = self.records[0]
        if active_record.policy_sha256 != challenge.active_policy_sha256:
            return AuthorizationRecoveryApplyResult(
                status="active_policy_stale", challenge=challenge
            )
        if request.candidate_record.policy_sha256 != challenge.candidate_policy_sha256:
            return AuthorizationRecoveryApplyResult(
                status="candidate_digest_mismatch", challenge=challenge
            )
        used = challenge.model_copy(update={"used_at": request.completed_at})
        self.challenges[challenge.challenge_id] = used
        self.records = (request.candidate_record,)
        bootstrap_state = None
        if challenge.operation == "initial_bootstrap":
            if self.state is not None:
                return AuthorizationRecoveryApplyResult(
                    status="bootstrap_already_complete", challenge=challenge
                )
            bootstrap_state = AuthorizationBootstrapState(
                completed_at=request.completed_at,
                completed_by_github_id=challenge.intended_github_id,
                completion_challenge_id=challenge.challenge_id,
            )
            self.state = bootstrap_state
        if challenge.operation == "replace_recovery_key":
            replacement = self.keys[challenge.replacement_key_id]
            compromised = self.keys[challenge.compromised_key_id]
            self.keys[replacement.key_id] = replacement.model_copy(
                update={
                    "status": "active",
                    "activated_at": request.completed_at,
                    "proof_challenge": "",
                    "proof_expires_at": "",
                }
            )
            self.keys[compromised.key_id] = compromised.model_copy(
                update={
                    "status": "revoked",
                    "revoked_at": request.completed_at,
                    "proof_challenge": "",
                    "proof_expires_at": "",
                }
            )
        self.audits.append(request.audit)
        self.outbox_deliveries.append(request.outbox_delivery)
        return AuthorizationRecoveryApplyResult(
            status="applied",
            authz_policy_record=request.candidate_record,
            bootstrap_state=bootstrap_state,
            challenge=used,
            audit=request.audit,
            outbox_delivery=request.outbox_delivery,
        )


class AuthorizationRecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _Store()
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
            random_token=lambda byte_count: (
                f"token-{byte_count}-{len(self.store.challenges)}-abcdefghijklmnopqrstuvwx"
            ),
        )

    def _activate(self, key_id: str, custody_slot: str) -> None:
        self.service.enroll_key(
            key_id=key_id,
            custody_slot=custody_slot,
            public_key=_hardware_key(key_id),
        )
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            self.service.verify_key_proof(
                key_id=key_id, signature=b"-----BEGIN SSH SIGNATURE-----\nfixture"
            )

    def test_recovery_requires_two_independent_active_hardware_keys(self) -> None:
        self._activate("key-one", "custody-a")
        with self.assertRaisesRegex(ValueError, "two active independently"):
            self.service.prepare(
                operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
            )
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        self.assertIn(b'"intended_github_id":101', prepared.canonical_request)
        self.assertNotIn(b"recovery@example", prepared.canonical_request)
        self.assertIn(b'"signing_key_id":"key-one"', prepared.canonical_request)

    def test_apply_consumes_challenge_and_completes_bootstrap_monotonically(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            result = self.service.apply(
                challenge_id=prepared.challenge.challenge_id,
                key_id="key-one",
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                trace_id="trace-1",
            )
        self.assertIsNotNone(result.authz_policy_record)
        assert result.authz_policy_record is not None
        self.assertEqual(result.authz_policy_record.policy.github_humans[0].github_ids, (101,))
        self.assertEqual(self.service.bootstrap_state(), "complete")
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            with self.assertRaisesRegex(ValueError, "already been consumed"):
                self.service.apply(
                    challenge_id=prepared.challenge.challenge_id,
                    key_id="key-one",
                    signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                    trace_id="trace-2",
                )

    def test_apply_verifies_the_exact_prepared_canonical_bytes(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        signature = b"-----BEGIN SSH SIGNATURE-----\nfixture"

        with patch("control_plane.authorization_recovery._verify_sshsig") as verifier:
            self.service.apply(
                challenge_id=prepared.challenge.challenge_id,
                key_id="key-one",
                signature=signature,
                trace_id="trace-exact-payload",
            )

        verifier.assert_called_once_with(
            public_key=self.store.keys["key-one"].public_key,
            payload=prepared.canonical_request,
            signature=signature,
        )

    def test_unknown_challenge_does_not_create_unbounded_audit_rows(self) -> None:
        initial_audit_count = len(self.store.audits)

        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.service.apply(
                challenge_id="unknown-challenge",
                key_id="unknown-key",
                signature=b"invalid",
                trace_id="trace-unknown",
            )

        self.assertEqual(len(self.store.audits), initial_audit_count)

    def test_invalid_apply_signature_is_audited_once_per_challenge(self) -> None:
        current_time = [datetime(2026, 8, 25, tzinfo=timezone.utc)]
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: current_time[0],
            random_token=lambda byte_count: (
                f"token-{byte_count}-{len(self.store.challenges)}-abcdefghijklmnopqrstuvwx"
            ),
        )
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )

        for _ in range(2):
            current_time[0] += timedelta(seconds=2)
            with (
                patch(
                    "control_plane.authorization_recovery._verify_sshsig",
                    side_effect=ValueError("invalid signature"),
                ),
                self.assertRaisesRegex(ValueError, "invalid signature"),
            ):
                self.service.apply(
                    challenge_id=prepared.challenge.challenge_id,
                    key_id="key-one",
                    signature=b"invalid",
                    trace_id="trace-invalid-signature",
                )

        signature_audits = [
            audit for audit in self.store.audits if audit.reason_code == "signature_invalid"
        ]
        self.assertEqual(len(signature_audits), 1)

    def test_invalid_key_proof_signature_is_audited_once(self) -> None:
        current_time = [datetime(2026, 8, 25, tzinfo=timezone.utc)]
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: current_time[0],
            random_token=lambda byte_count: (
                f"token-{byte_count}-{len(self.store.challenges)}-abcdefghijklmnopqrstuvwx"
            ),
        )
        self.service.enroll_key(
            key_id="key-one",
            custody_slot="custody-a",
            public_key=_hardware_key("key-one"),
        )

        for _ in range(2):
            current_time[0] += timedelta(seconds=2)
            with (
                patch(
                    "control_plane.authorization_recovery._verify_sshsig",
                    side_effect=ValueError("invalid signature"),
                ),
                self.assertRaisesRegex(ValueError, "invalid signature"),
            ):
                self.service.verify_key_proof(key_id="key-one", signature=b"invalid")

        signature_audits = [
            audit for audit in self.store.audits if audit.reason_code == "signature_invalid"
        ]
        self.assertEqual(len(signature_audits), 1)

    def test_expired_key_proof_rejection_is_audited_once(self) -> None:
        current_time = [datetime(2026, 8, 25, tzinfo=timezone.utc)]
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: current_time[0],
            random_token=lambda byte_count: (
                f"token-{byte_count}-{len(self.store.challenges)}-abcdefghijklmnopqrstuvwx"
            ),
        )
        self.service.enroll_key(
            key_id="key-one",
            custody_slot="custody-a",
            public_key=_hardware_key("key-one"),
        )
        current_time[0] += timedelta(minutes=11)

        for _ in range(2):
            current_time[0] += timedelta(seconds=2)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.service.verify_key_proof(key_id="key-one", signature=b"invalid")

        expired_audits = [
            audit for audit in self.store.audits if audit.reason_code == "proof_expired"
        ]
        self.assertEqual(len(expired_audits), 1)

    def test_prepare_capacity_is_scoped_per_key_and_does_not_block_enrollment(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        self.service.prepare(
            operation="initial_bootstrap", intended_github_id=102, signing_key_id="key-one"
        )

        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "rate limit"):
                self.service.prepare(
                    operation="initial_bootstrap",
                    intended_github_id=103,
                    signing_key_id="key-one",
                )

        prepared_with_other_key = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=103, signing_key_id="key-two"
        )
        enrolled = self.service.enroll_key(
            key_id="key-three",
            custody_slot="custody-c",
            public_key=_hardware_key("key-three"),
        )
        rate_limit_audits = [
            audit for audit in self.store.audits if audit.reason_code == "challenge_rate_limited"
        ]

        self.assertEqual(prepared_with_other_key.challenge.signing_key_id, "key-two")
        self.assertEqual(enrolled.status, "pending")
        self.assertEqual(len(rate_limit_audits), 1)

    def test_restore_known_administrator_can_replace_the_fixed_recovery_identity(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        initial = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            self.service.apply(
                challenge_id=initial.challenge.challenge_id,
                key_id="key-one",
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                trace_id="trace-bootstrap",
            )
        restore = self.service.prepare(
            operation="restore_known_administrator",
            intended_github_id=202,
            signing_key_id="key-two",
        )

        with patch("control_plane.authorization_recovery._verify_sshsig"):
            restored = self.service.apply(
                challenge_id=restore.challenge.challenge_id,
                key_id="key-two",
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                trace_id="trace-restore",
            )

        assert restored.authz_policy_record is not None
        self.assertEqual(restored.authz_policy_record.policy.github_humans[0].github_ids, (202,))

    def test_revocation_fails_closed_before_signature_verification(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        self._activate("key-three", "custody-c")
        prepared = self.service.prepare(
            operation="initial_bootstrap", intended_github_id=101, signing_key_id="key-one"
        )
        self.service.revoke_key(key_id="key-one")
        with patch("control_plane.authorization_recovery._verify_sshsig") as verifier:
            with self.assertRaisesRegex(ValueError, "not active"):
                self.service.apply(
                    challenge_id=prepared.challenge.challenge_id,
                    key_id="key-one",
                    signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                    trace_id="trace-1",
                )
        verifier.assert_not_called()

    def test_public_key_comment_is_not_part_of_canonical_fingerprint(self) -> None:
        key_with_first_comment = f"{_HARDWARE_KEY} first-comment"
        key_with_second_comment = _HARDWARE_KEY.replace("recovery@example", "second-comment")

        canonical_one, _, _ = canonicalize_recovery_public_key(key_with_first_comment)
        canonical_two, _, _ = canonicalize_recovery_public_key(key_with_second_comment)

        self.assertEqual(canonical_one, canonical_two)
        self.assertEqual(
            recovery_key_fingerprint(key_with_first_comment),
            recovery_key_fingerprint(key_with_second_comment),
        )

    def test_duplicate_live_key_fingerprint_cannot_claim_independent_custody(self) -> None:
        self.service.enroll_key(
            key_id="key-one",
            custody_slot="custody-a",
            public_key=_HARDWARE_KEY,
        )

        with self.assertRaisesRegex(ValueError, "already has a non-revoked enrollment"):
            self.service.enroll_key(
                key_id="key-two",
                custody_slot="custody-b",
                public_key=_HARDWARE_KEY,
            )

    def test_rotation_binds_replacement_and_revokes_compromised_key(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        self._activate("key-three", "custody-c")
        self.service.enroll_key(
            key_id="key-four", custody_slot="custody-d", public_key=_hardware_key("key-four")
        )
        self.store.state = AuthorizationBootstrapState(
            completed_at="2026-08-25T00:00:00Z",
            completed_by_github_id=101,
            completion_challenge_id="authz-recovery-complete",
        )
        prepared = self.service.prepare(
            operation="replace_recovery_key",
            intended_github_id=101,
            signing_key_id="key-two",
            compromised_key_id="key-one",
            replacement_key_id="key-four",
        )

        with patch("control_plane.authorization_recovery._verify_sshsig"):
            result = self.service.apply(
                challenge_id=prepared.challenge.challenge_id,
                key_id="key-two",
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                trace_id="trace-rotate",
            )

        self.assertEqual(result.status, "applied")
        self.assertEqual(self.store.keys["key-four"].status, "active")
        self.assertEqual(self.store.keys["key-one"].status, "revoked")

    def test_normal_ed25519_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "hardware-backed"):
            self.service.enroll_key(
                key_id="not-hardware",
                custody_slot="custody-a",
                public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEhBUkRXQVJFX0tFWQ== test",
            )

    def test_public_key_parser_rejects_multiline_and_mismatched_blob_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "single OpenSSH"):
            canonicalize_recovery_public_key(f"{_HARDWARE_KEY}\n{_HARDWARE_KEY}")
        with self.assertRaisesRegex(ValueError, "does not match"):
            canonicalize_recovery_public_key(
                "sk-ssh-ed25519@openssh.com "
                "AAAAC3NzaC1lZDI1NTE5Zml4dHVyZS1tYXRlcmlhbC1mb3ItcGFyc2VyLW9ubHk= "
                "mismatched@example"
            )

    def test_sshsig_verifier_accepts_known_answer_signature(self) -> None:
        public_key, payload, signature = self._software_sshsig_fixture(
            namespace=AUTHORIZATION_RECOVERY_NAMESPACE
        )

        with patch(
            "control_plane.authorization_recovery.AUTHORIZATION_RECOVERY_KEY_TYPES",
            frozenset({"ssh-ed25519"}),
        ):
            _verify_sshsig(public_key=public_key, payload=payload, signature=signature)

    def test_sshsig_verifier_rejects_wrong_namespace(self) -> None:
        public_key, payload, signature = self._software_sshsig_fixture(
            namespace="launchplane.authorization-recovery.wrong"
        )

        with self.assertRaisesRegex(ValueError, "verification failed"):
            with patch(
                "control_plane.authorization_recovery.AUTHORIZATION_RECOVERY_KEY_TYPES",
                frozenset({"ssh-ed25519"}),
            ):
                _verify_sshsig(public_key=public_key, payload=payload, signature=signature)

    def test_sshsig_verifier_rejects_software_keys_by_default(self) -> None:
        public_key, payload, signature = self._software_sshsig_fixture(
            namespace=AUTHORIZATION_RECOVERY_NAMESPACE
        )

        with self.assertRaisesRegex(ValueError, "hardware-backed"):
            _verify_sshsig(public_key=public_key, payload=payload, signature=signature)

    def test_sshsig_verifier_rejects_malformed_and_oversized_envelopes(self) -> None:
        public_key, payload, _ = self._software_sshsig_fixture(
            namespace=AUTHORIZATION_RECOVERY_NAMESPACE
        )

        with self.assertRaisesRegex(ValueError, "verification failed"):
            with patch(
                "control_plane.authorization_recovery.AUTHORIZATION_RECOVERY_KEY_TYPES",
                frozenset({"ssh-ed25519"}),
            ):
                _verify_sshsig(
                    public_key=public_key,
                    payload=payload,
                    signature=b"-----BEGIN SSH SIGNATURE-----\nmalformed",
                )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            with patch(
                "control_plane.authorization_recovery.AUTHORIZATION_RECOVERY_KEY_TYPES",
                frozenset({"ssh-ed25519"}),
            ):
                _verify_sshsig(
                    public_key=public_key,
                    payload=payload,
                    signature=(
                        b"-----BEGIN SSH SIGNATURE-----\n"
                        + (b"A" * AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES)
                    ),
                )

    def _software_sshsig_fixture(self, *, namespace: str) -> tuple[str, bytes, bytes]:
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is required for SSHSIG verifier coverage")
        payload = b"launchplane recovery verifier known answer"
        with TemporaryDirectory() as temporary_directory_name:
            base = Path(temporary_directory_name)
            key_path = base / "fixture"
            payload_path = base / "payload"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            payload_path.write_bytes(payload)
            subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "sign",
                    "-f",
                    str(key_path),
                    "-n",
                    namespace,
                    str(payload_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return (
                key_path.with_suffix(".pub").read_text(encoding="utf-8"),
                payload,
                payload_path.with_suffix(".sig").read_bytes(),
            )
