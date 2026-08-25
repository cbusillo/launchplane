from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from control_plane.authorization_recovery import (
    AuthorizationRecoveryAudit,
    AuthorizationRecoveryChallenge,
    AuthorizationRecoveryKey,
    AuthorizationRecoveryService,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


_HARDWARE_KEY = "sk-ssh-ed25519@openssh.com AAAAC3NzaC1lZDI1NTE5AAAAIEhBUkRXQVJFX0tFWV9URVNUX01BVEVSSUFM recovery@example"


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
        self.state = None
        self.records = (_record(),)

    def read_authorization_bootstrap_state(self):
        return self.state

    def write_authorization_bootstrap_state(self, state):
        if self.state is not None and self.state.status == "complete" and state != self.state:
            raise ValueError("monotonic")
        self.state = state

    def list_authorization_recovery_keys(self):
        return tuple(self.keys.values())

    def read_authorization_recovery_key(self, key_id):
        return self.keys.get(key_id)

    def write_authorization_recovery_key(self, key):
        self.keys[key.key_id] = key

    def read_authorization_recovery_challenge(self, challenge_id):
        return self.challenges.get(challenge_id)

    def list_authorization_recovery_challenges(self):
        return tuple(self.challenges.values())

    def write_authorization_recovery_challenge(self, challenge):
        self.challenges[challenge.challenge_id] = challenge

    def consume_authorization_recovery_challenge(self, *, challenge_id, used_at):
        challenge = self.challenges.get(challenge_id)
        if challenge is None or challenge.used_at:
            return False
        self.challenges[challenge_id] = challenge.model_copy(update={"used_at": used_at})
        return True

    def write_authorization_recovery_audit(self, audit):
        self.audits.append(audit)

    def list_authz_policy_records(self, *, status="", limit=None):
        records = tuple(record for record in self.records if not status or record.status == status)
        return records if limit is None else records[:limit]

    def compare_and_write_authz_policy_record(self, *, expected_record, replacement_record, mutation):
        if self.records != (expected_record,):
            return AuthzPolicyCompareWriteResult(status="stale")
        self.records = (replacement_record,) if replacement_record is not None else self.records
        return AuthzPolicyCompareWriteResult(status="written", current_record=replacement_record)


class AuthorizationRecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _Store()
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
        )

    def _activate(self, key_id: str, custody_slot: str) -> None:
        self.service.enroll_key(key_id=key_id, custody_slot=custody_slot, public_key=_HARDWARE_KEY)
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            self.service.verify_key_proof(key_id=key_id, signature=b"-----BEGIN SSH SIGNATURE-----\nfixture")

    def test_recovery_requires_two_independent_active_hardware_keys(self) -> None:
        self._activate("key-one", "custody-a")
        with self.assertRaisesRegex(ValueError, "two active independently"):
            self.service.prepare(operation="initial_bootstrap", intended_github_id=101)
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(operation="initial_bootstrap", intended_github_id=101)
        self.assertIn(b'"intended_github_id":101', prepared.canonical_request)
        self.assertNotIn(b"recovery@example", prepared.canonical_request)

    def test_apply_consumes_challenge_and_completes_bootstrap_monotonically(self) -> None:
        self._activate("key-one", "custody-a")
        self._activate("key-two", "custody-b")
        prepared = self.service.prepare(operation="initial_bootstrap", intended_github_id=101)
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            result = self.service.apply(
                challenge_id=prepared.challenge.challenge_id,
                key_id="key-one",
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
                trace_id="trace-1",
            )
        self.assertEqual(result.policy.github_humans[0].github_ids, (101,))
        self.assertEqual(self.service.bootstrap_state().status, "complete")
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
        prepared = self.service.prepare(operation="initial_bootstrap", intended_github_id=101)
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

    def test_normal_ed25519_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "hardware-backed"):
            self.service.enroll_key(
                key_id="not-hardware",
                custody_slot="custody-a",
                public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEhBUkRXQVJFX0tFWQ== test",
            )
