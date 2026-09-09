from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.contracts.ordinary_agent_custody import (
    OrdinaryAgentCustodyCandidate,
    OrdinaryAgentCustodyConflictError,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyCleanupUnknown,
    OrdinaryAgentCustodyError,
    OrdinaryAgentCustodyUnavailable,
    ordinary_agent_provider_token_lease,
    resolve_ordinary_agent_github_app_identity,
)
from control_plane.storage.postgres import PostgresRecordStore


def _candidate(
    *,
    credential_version: int = 1,
    repository: str = "example/repo",
    base_branch: str = "main",
) -> OrdinaryAgentCustodyCandidate:
    return OrdinaryAgentCustodyCandidate(
        principal_id="agent_one",
        repository_id=123,
        repository=repository,
        base_branch=base_branch,
        credential_id="credential_one",
        credential_version=credential_version,
        secret_id="secret-app",
        secret_binding_id="binding-app",
        secret_version_id="version-1",
        expected_app_id=42,
        effect_profile="guarded_merge",
    )


class OrdinaryAgentCustodyTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url="sqlite+pysqlite:///:memory:")
        self.store.ensure_schema()
        self.store.write_secret_record(
            SecretRecord(
                secret_id="secret-app",
                scope="global",
                integration="ordinary_agent_github_app",
                name="ordinary agent GitHub App private key",
                current_version_id="version-1",
                created_at="2026-09-08T12:00:00Z",
                updated_at="2026-09-08T12:00:00Z",
            )
        )
        self.store.write_secret_version(
            SecretVersion(
                version_id="version-1",
                secret_id="secret-app",
                created_at="2026-09-08T12:00:00Z",
                ciphertext="encrypted-private-key",
            )
        )
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="binding-app",
                secret_id="secret-app",
                integration="ordinary_agent_github_app",
                binding_key="private_key",
                created_at="2026-09-08T12:00:00Z",
                updated_at="2026-09-08T12:00:00Z",
            )
        )
        self.decrypt = patch(
            "control_plane.ordinary_agent_custody.control_plane_secrets._decrypt_secret_value",
            return_value=self.private_key,
        )
        self.decrypt.start()

    def tearDown(self) -> None:
        self.decrypt.stop()
        self.store.close()

    def test_resolver_requires_exact_current_binding_and_version(self) -> None:
        resolved = resolve_ordinary_agent_github_app_identity(
            record_store=self.store,
            candidate=_candidate(),
        )
        self.assertEqual(resolved.identity.app_id, 42)
        self.assertNotIn(self.private_key, repr(resolved))

        stale = _candidate().model_copy(update={"secret_version_id": "version-old"})
        with self.assertRaisesRegex(OrdinaryAgentCustodyError, "unavailable"):
            resolve_ordinary_agent_github_app_identity(
                record_store=self.store,
                candidate=stale,
            )

    def test_token_stays_in_memory_and_confirmed_revoke_releases_fence(self) -> None:
        calls: list[dict[str, object]] = []
        now = datetime.now(timezone.utc)

        def api_request(**kwargs: object) -> object:
            calls.append(kwargs)
            path = kwargs["path"]
            if path == "/app":
                return {"id": 42}
            if path == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            if path == "/app/installations/77/access_tokens":
                self.assertEqual(
                    kwargs["body"],
                    {
                        "repository_ids": [123],
                        "permissions": {
                            "contents": "write",
                            "pull_requests": "read",
                        },
                    },
                )
                return {
                    "token": "provider-token-secret",
                    "expires_at": (now + timedelta(minutes=50)).isoformat(),
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "read",
                    },
                    "repositories": [{"id": 123, "full_name": "example/repo"}],
                }
            if path == "/installation/token":
                return None
            raise AssertionError(path)

        with ordinary_agent_provider_token_lease(
            record_store=self.store,
            secret_store=self.store,
            candidate=_candidate(),
            idempotency_key="request-one",
            request_payload={"action": "guarded_merge", "sha": "a" * 40},
            api_request=api_request,
            utc_now=lambda: now,
        ) as lease:
            self.assertEqual(lease.installation_token.token, "provider-token-secret")
            self.assertNotIn("provider-token-secret", repr(lease))
            issued = self.store.read_ordinary_agent_custody_issue_attempt(lease.attempt_id)
            self.assertEqual(issued.state, "issued")
            self.assertNotIn("provider-token-secret", str(issued.model_dump()))

        closed = self.store.read_ordinary_agent_custody_issue_attempt(lease.attempt_id)
        self.assertEqual((closed.state, closed.close_reason), ("closed", "confirmed_revoked"))
        self.assertEqual(calls[-1]["path"], "/installation/token")

    def test_lost_mint_response_stays_fenced_across_rotation_and_retry(self) -> None:
        mint_calls = 0

        def api_request(**kwargs: object) -> object:
            nonlocal mint_calls
            path = kwargs["path"]
            if path == "/app":
                return {"id": 42}
            if path == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            mint_calls += 1
            raise OSError("response lost")

        with self.assertRaises(OSError):
            with ordinary_agent_provider_token_lease(
                record_store=self.store,
                secret_store=self.store,
                candidate=_candidate(),
                idempotency_key="request-unknown",
                request_payload={"action": "guarded_merge"},
                api_request=api_request,
            ):
                self.fail("lost-response token must never be yielded")

        attempt_id = "custody_" + hashlib.sha256(b"request-unknown").hexdigest()
        unknown = self.store.read_ordinary_agent_custody_issue_attempt(attempt_id)
        self.assertEqual(unknown.state, "issue_unknown")
        self.assertIsNone(unknown.residual_expires_at)

        for candidate, key in (
            (_candidate(), "request-unknown"),
            (_candidate(credential_version=2), "request-after-rotation"),
            (
                _candidate(repository="example/renamed", base_branch="release/next"),
                "request-after-rename",
            ),
        ):
            with self.assertRaises(OrdinaryAgentCustodyUnavailable):
                with ordinary_agent_provider_token_lease(
                    record_store=self.store,
                    secret_store=self.store,
                    candidate=candidate,
                    idempotency_key=key,
                    request_payload={"action": "guarded_merge"},
                    api_request=api_request,
                ):
                    self.fail("fenced issue must not mint")
        self.assertEqual(mint_calls, 1)

    def test_late_valid_token_is_revoked_without_being_yielded(self) -> None:
        now = datetime.now(timezone.utc)
        clock_values = iter((0.0, 0.0, 0.0, 0.0, 31.0))

        def api_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/app":
                return {"id": 42}
            if path == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            if path == "/app/installations/77/access_tokens":
                return {
                    "token": "late-token",
                    "expires_at": (now + timedelta(minutes=45)).isoformat(),
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "read",
                    },
                    "repositories": [{"id": 123, "full_name": "example/repo"}],
                }
            if path == "/installation/token":
                return None
            raise AssertionError(path)

        with self.assertRaisesRegex(OrdinaryAgentCustodyError, "arrived after"):
            with ordinary_agent_provider_token_lease(
                record_store=self.store,
                secret_store=self.store,
                candidate=_candidate(),
                idempotency_key="late-response",
                request_payload={"action": "guarded_merge"},
                api_request=api_request,
                monotonic=lambda: next(clock_values),
                utc_now=lambda: now,
            ):
                self.fail("late token must never be yielded")

        attempt_id = "custody_" + hashlib.sha256(b"late-response").hexdigest()
        attempt = self.store.read_ordinary_agent_custody_issue_attempt(attempt_id)
        self.assertEqual((attempt.state, attempt.close_reason), ("closed", "confirmed_revoked"))

    def test_known_token_with_unknown_revoke_keeps_bounded_cleanup_fence(self) -> None:
        now = datetime.now(timezone.utc)

        def api_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/app":
                return {"id": 42}
            if path == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            if path == "/app/installations/77/access_tokens":
                return {
                    "token": "known-token",
                    "expires_at": (now + timedelta(minutes=45)).isoformat(),
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "read",
                    },
                    "repositories": [{"id": 123, "full_name": "example/repo"}],
                }
            raise OSError("revoke outcome unknown")

        with self.assertRaisesRegex(OrdinaryAgentCustodyCleanupUnknown, "cleanup outcome"):
            with ordinary_agent_provider_token_lease(
                record_store=self.store,
                secret_store=self.store,
                candidate=_candidate(),
                idempotency_key="unknown-revoke",
                request_payload={"action": "guarded_merge"},
                api_request=api_request,
                utc_now=lambda: now,
            ):
                pass

        attempt_id = "custody_" + hashlib.sha256(b"unknown-revoke").hexdigest()
        attempt = self.store.read_ordinary_agent_custody_issue_attempt(attempt_id)
        self.assertEqual(attempt.state, "cleanup_unknown")
        self.assertIsNotNone(attempt.residual_expires_at)
        with self.assertRaises(OrdinaryAgentCustodyConflictError):
            self.store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=attempt_id, reason="known_expired"
            )


if __name__ == "__main__":
    unittest.main()
