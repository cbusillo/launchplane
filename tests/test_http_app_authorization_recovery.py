from __future__ import annotations

import base64
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict, cast
import unittest
from unittest.mock import patch

from httpx2 import AsyncClient

from control_plane.authorization_recovery import (
    AuthorizationRecoveryApplyResult,
    AuthorizationRecoveryService,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
)
from tests.support.auth import StubVerifier, identity
from tests.support.http import lifespan_client


_RECOVERY_KEY_TYPE = "sk-ssh-ed25519@openssh.com"


class _PreparedChallenge(TypedDict):
    challenge_id: str


def _recovery_key(material: str) -> str:
    key_type = _RECOVERY_KEY_TYPE.encode("ascii")
    key_blob = len(key_type).to_bytes(4, "big") + key_type + material.encode("utf-8")
    return f"{_RECOVERY_KEY_TYPE} {base64.b64encode(key_blob).decode('ascii')} recovery@example"


def _policy_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(schema_version=2)
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test:authorization-recovery-http",
        updated_at="2026-08-25T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


class AuthorizationRecoveryHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{root / 'store.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.record = _policy_record()
        self.store.seed_authz_policy_if_absent(self.record)
        token_counter = count()
        self.service = AuthorizationRecoveryService(
            record_store=self.store,
            service_identity="launchplane.test",
            now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
            random_token=lambda byte_count: (
                f"token-{byte_count}-{next(token_counter)}-abcdefghijklmnopqrstuvwx"
            ),
        )
        self._activate_key(key_id="key-one", custody_slot="custody-a")
        self._activate_key(key_id="key-two", custody_slot="custody-b")
        self.session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        self.human_session = self.session_manager.issue(_github_human_identity())
        self.app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=self.record.policy,
            authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(
                self.record.policy,
                policy_sha256=self.record.policy_sha256,
                source="db",
                record_id=self.record.record_id,
                revision=self.record.revision,
            ),
            record_store_factory=lambda: self.store,
            human_session_manager=self.session_manager,
            control_plane_root_path=root,
            state_dir=root / "state",
        )

    def _activate_key(self, *, key_id: str, custody_slot: str) -> None:
        self.service.enroll_key(
            key_id=key_id,
            custody_slot=custody_slot,
            public_key=_recovery_key(key_id),
        )
        with patch("control_plane.authorization_recovery._verify_sshsig"):
            self.service.verify_key_proof(
                key_id=key_id,
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture",
            )

    async def _prepare(self, client: AsyncClient) -> _PreparedChallenge:
        response = await client.post(
            "/v1/authorization-recovery/public/prepare",
            json={
                "operation": "initial_bootstrap",
                "intended_github_id": 123,
                "signing_key_id": "key-one",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        return cast(_PreparedChallenge, response.json())

    async def test_public_prepare_and_status_are_bounded_and_nonauthorizing(self) -> None:
        initial_records = self.store.list_authz_policy_records()

        async with lifespan_client(self.app) as client:
            prepared = await self._prepare(client)
            status = await client.get(
                f"/v1/authorization-recovery/public/challenges/{prepared['challenge_id']}"
            )

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers["cache-control"], "no-store")
        self.assertEqual(status.json()["status"], "prepared")
        self.assertEqual(self.store.list_authz_policy_records(), initial_records)
        self.assertIsNone(self.store.read_authorization_bootstrap_state())

    async def test_public_routes_reject_authorization_and_cookie_before_processing(self) -> None:
        cases = (
            (
                "POST",
                "/v1/authorization-recovery/public/prepare",
                {"Authorization": "Bearer valid-token"},
                {"invalid": "body"},
            ),
            (
                "POST",
                "/v1/authorization-recovery/public/apply",
                {"Cookie": "launchplane_session=forbidden"},
                {"invalid": "body"},
            ),
            (
                "GET",
                "/v1/authorization-recovery/public/challenges/missing",
                {"Authorization": "Bearer valid-token"},
                None,
            ),
        )

        async with lifespan_client(self.app) as client:
            for method, path, headers, payload in cases:
                with self.subTest(method=method, path=path):
                    response = await client.request(method, path, headers=headers, json=payload)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["code"], "credentials_not_accepted")
                    self.assertEqual(response.headers["cache-control"], "no-store")

        self.assertEqual(self.store.list_authorization_recovery_challenges(), ())

    async def test_public_request_validation_is_fail_closed_and_no_store(self) -> None:
        async with lifespan_client(self.app) as client:
            malformed = await client.post(
                "/v1/authorization-recovery/public/prepare",
                content=b"{",
                headers={"Content-Type": "application/json"},
            )
            oversized = await client.post(
                "/v1/authorization-recovery/public/prepare",
                content=b"x" * (32 * 1024 + 1),
                headers={"Content-Type": "application/json"},
            )
            caller_policy = await client.post(
                "/v1/authorization-recovery/public/apply",
                json={
                    "challenge_id": "challenge",
                    "signing_key_id": "key-one",
                    "signature": "invalid",
                    "policy": {"github_humans": []},
                },
            )

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "invalid_body")
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json()["error"]["code"], "body_too_large")
        self.assertEqual(caller_policy.status_code, 400)
        self.assertEqual(caller_policy.json()["error"]["code"], "invalid_body")
        for response in (malformed, oversized, caller_policy):
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_public_apply_succeeds_once_and_replay_is_rejected(self) -> None:
        async with lifespan_client(self.app) as client:
            prepared = await self._prepare(client)
            payload = {
                "challenge_id": prepared["challenge_id"],
                "signing_key_id": "key-one",
                "signature": base64.b64encode(b"-----BEGIN SSH SIGNATURE-----\nfixture").decode(
                    "ascii"
                ),
            }
            with patch("control_plane.authorization_recovery._verify_sshsig"):
                applied = await client.post(
                    "/v1/authorization-recovery/public/apply",
                    json=payload,
                )
                replayed = await client.post(
                    "/v1/authorization-recovery/public/apply",
                    json=payload,
                )

        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["status"], "applied")
        self.assertEqual(replayed.status_code, 400)
        self.assertEqual(replayed.json()["error"]["code"], "apply_rejected")
        self.assertEqual(applied.headers["cache-control"], "no-store")
        self.assertEqual(replayed.headers["cache-control"], "no-store")

    async def test_public_prepare_capacity_is_per_key_and_does_not_block_browser_enrollment(
        self,
    ) -> None:
        async with lifespan_client(self.app) as client:
            responses = [
                await client.post(
                    "/v1/authorization-recovery/public/prepare",
                    json={
                        "operation": "initial_bootstrap",
                        "intended_github_id": intended_github_id,
                        "signing_key_id": "key-one",
                    },
                )
                for intended_github_id in (101, 102, 103)
            ]
            other_key = await client.post(
                "/v1/authorization-recovery/public/prepare",
                json={
                    "operation": "initial_bootstrap",
                    "intended_github_id": 103,
                    "signing_key_id": "key-two",
                },
            )
            enrolled = await client.post(
                "/v1/authorization-recovery/keys/enroll",
                headers=_browser_mutation_headers(
                    self.session_manager,
                    self.human_session,
                ),
                json={
                    "key_id": "key-three",
                    "custody_slot": "custody-c",
                    "public_key": _recovery_key("key-three"),
                },
            )

        self.assertEqual([response.status_code for response in responses], [200, 200, 400])
        self.assertEqual(other_key.status_code, 200)
        self.assertEqual(enrolled.status_code, 200)
        self.assertEqual(responses[-1].json()["error"]["code"], "prepare_rejected")

    async def test_public_apply_unknown_challenge_is_not_audited_and_bad_signature_is_bounded(
        self,
    ) -> None:
        async with lifespan_client(self.app) as client:
            initial_audit_count = len(self.store.list_authorization_recovery_audits())
            unknown = await client.post(
                "/v1/authorization-recovery/public/apply",
                json={
                    "challenge_id": "unknown-challenge",
                    "signing_key_id": "key-one",
                    "signature": base64.b64encode(b"invalid").decode("ascii"),
                },
            )
            after_unknown_count = len(self.store.list_authorization_recovery_audits())
            prepared = await self._prepare(client)
            with patch(
                "control_plane.authorization_recovery._verify_sshsig",
                side_effect=ValueError("invalid signature"),
            ):
                rejected = [
                    await client.post(
                        "/v1/authorization-recovery/public/apply",
                        json={
                            "challenge_id": prepared["challenge_id"],
                            "signing_key_id": "key-one",
                            "signature": base64.b64encode(b"invalid").decode("ascii"),
                        },
                    )
                    for _ in range(2)
                ]

        signature_audits = [
            audit
            for audit in self.store.list_authorization_recovery_audits()
            if audit.reason_code == "signature_invalid"
        ]
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(after_unknown_count, initial_audit_count)
        self.assertEqual([response.status_code for response in rejected], [400, 400])
        self.assertEqual(len(signature_audits), 1)

    async def test_public_apply_masks_atomic_stale_and_conflict_results(self) -> None:
        async with lifespan_client(self.app) as client:
            for result_status in ("active_policy_stale", "conflict"):
                with self.subTest(result_status=result_status):
                    prepared = await self._prepare(client)
                    with (
                        patch("control_plane.authorization_recovery._verify_sshsig"),
                        patch.object(
                            self.store,
                            "apply_authorization_recovery",
                            return_value=AuthorizationRecoveryApplyResult(status=result_status),
                        ),
                    ):
                        response = await client.post(
                            "/v1/authorization-recovery/public/apply",
                            json={
                                "challenge_id": prepared["challenge_id"],
                                "signing_key_id": "key-one",
                                "signature": base64.b64encode(
                                    b"-----BEGIN SSH SIGNATURE-----\nfixture"
                                ).decode("ascii"),
                            },
                        )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "apply_rejected")
                    self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_browser_lifecycle_rejects_bearer_and_requires_mutation_headers(self) -> None:
        cookie_headers = {"Cookie": self.session_manager.session_cookie_header(self.human_session)}
        valid_mutation_headers = _browser_mutation_headers(
            self.session_manager,
            self.human_session,
        )
        invalid_mutation_headers = []
        for header_name, header_value in (
            ("Origin", None),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
            ("X-CSRF-Token", None),
        ):
            headers = dict(valid_mutation_headers)
            if header_value is None:
                headers.pop(header_name)
            else:
                headers[header_name] = header_value
            invalid_mutation_headers.append(headers)

        async with lifespan_client(self.app) as client:
            bearer_status = await client.get(
                "/v1/authorization-recovery/status",
                headers={"Authorization": "Bearer valid-token"},
            )
            browser_status = await client.get(
                "/v1/authorization-recovery/status",
                headers=cookie_headers,
            )
            rejected_mutations = [
                await client.post(
                    "/v1/authorization-recovery/keys/enroll",
                    headers=headers,
                    json={
                        "key_id": "key-three",
                        "custody_slot": "custody-c",
                        "public_key": _recovery_key("key-three"),
                    },
                )
                for headers in invalid_mutation_headers
            ]
            bearer_mutation = await client.post(
                "/v1/authorization-recovery/keys/enroll",
                headers={"Authorization": "Bearer valid-token"},
                json={
                    "key_id": "key-three",
                    "custody_slot": "custody-c",
                    "public_key": _recovery_key("key-three"),
                },
            )
            enrolled = await client.post(
                "/v1/authorization-recovery/keys/enroll",
                headers=valid_mutation_headers,
                json={
                    "key_id": "key-three",
                    "custody_slot": "custody-c",
                    "public_key": _recovery_key("key-three"),
                },
            )

        self.assertEqual(bearer_status.status_code, 403)
        self.assertEqual(browser_status.status_code, 200)
        for rejected_mutation in rejected_mutations:
            self.assertEqual(rejected_mutation.status_code, 403)
            self.assertEqual(rejected_mutation.json()["error"]["code"], "browser_mutation_denied")
        self.assertEqual(bearer_mutation.status_code, 403)
        self.assertEqual(bearer_mutation.json()["error"]["code"], "authorization_denied")
        self.assertEqual(enrolled.status_code, 200)
        for response in (
            bearer_status,
            browser_status,
            bearer_mutation,
            enrolled,
            *rejected_mutations,
        ):
            self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
