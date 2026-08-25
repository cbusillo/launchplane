from __future__ import annotations

from datetime import datetime, timezone
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


_HARDWARE_KEY = "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tZml4dHVyZS1tYXRlcmlhbC1mb3ItcGFyc2VyLW9ubHk= recovery@example"


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
        self.service.enroll_key(key_id=key_id, custody_slot=custody_slot, public_key=_HARDWARE_KEY)
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

    def test_rotation_binds_replacement_and_revokes_compromised_key(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        self._activate("key-three", "custody-c")
        self.service.enroll_key(
            key_id="key-four", custody_slot="custody-d", public_key=_HARDWARE_KEY
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
