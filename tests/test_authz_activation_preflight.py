from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Literal, cast

import click
from click import Command
from click.testing import CliRunner
from cryptography.fernet import Fernet

from control_plane import authz_activation_preflight, secrets as control_plane_secrets
from control_plane.authz_activation_preflight import (
    ACTIVATION_PREFLIGHT_STORAGE_SCAN_LIMIT,
    ActivationPreflightFailure,
    build_activation_preflight_response,
)
from control_plane.cli_policy_profiles import (
    PolicyProfileCliCallbacks,
    authz_policies,
    register_policy_profile_commands,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalAdminPolicyRule,
)
from control_plane.service_human_auth import LaunchplaneHumanSession
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise ValueError(token)


def _key_ring() -> str:
    return json.dumps(
        {
            "active_key_id": "activation-preflight-test",
            "keys": {"activation-preflight-test": Fernet.generate_key().decode()},
        }
    )


def _identity(
    *, role: Literal["read_only", "admin"] = "read_only", github_id: int = 123
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=github_id,
        name="Sensitive Name",
        email="alice@example.com",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/platform"}),
        role=role,
    )


def _session(
    *,
    now: datetime,
    role: Literal["read_only", "admin"] = "read_only",
    github_id: int = 123,
    created_offset: timedelta = timedelta(hours=1),
    expires_offset: timedelta = timedelta(days=1),
) -> LaunchplaneHumanSession:
    return LaunchplaneHumanSession(
        session_id=f"session-{github_id}-{role}-{created_offset}",
        identity=_identity(role=role, github_id=github_id),
        created_at=now - created_offset,
        expires_at=now + expires_offset,
    )


def _policy(
    *,
    human_rule: bool = True,
    local_admin: bool = True,
    unmanaged_action_empty_rule: bool = False,
) -> LaunchplaneAuthzPolicy:
    local_admin_rules = []
    if local_admin:
        local_admin_rules.append(
            LocalAdminPolicyRule(
                subjects=("authz-admin",),
                token_labels=("authz-admin-label",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(
                    "authz_policy_health.read",
                    "authz_policy_effective_access.read",
                ),
            )
        )
    if unmanaged_action_empty_rule:
        local_admin_rules.append(
            LocalAdminPolicyRule(
                subjects=("legacy-admin",),
                token_labels=("legacy-admin-label",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(),
            )
        )
    return LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                github_ids=(123,),
                roles=("admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            ),
        )
        if human_rule
        else (),
        local_admins=tuple(local_admin_rules),
    )


def _record(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test:activation-preflight",
        updated_at="2026-08-23T00:00:00+00:00",
        policy=policy,
        policy_sha256=digest,
    )


class AuthzActivationPreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_domain_rederives_role_and_redacts_session_identity(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        newer_session = _session(
            now=now,
            created_offset=timedelta(minutes=10),
            expires_offset=timedelta(minutes=15),
        )
        older_session = _session(
            now=now,
            role="admin",
            created_offset=timedelta(hours=2),
            expires_offset=timedelta(hours=3, minutes=1),
        )
        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
            clear=True,
        ):
            response = build_activation_preflight_response(
                trace_id="trace",
                github_id=123,
                active_record=_record(_policy(unmanaged_action_empty_rule=True)),
                sessions=(older_session, newer_session),
                now=now,
            )
        payload = response.model_dump(mode="json")
        self.assertEqual(payload["evaluation"]["decision"], "allowed")
        self.assertEqual(payload["session"]["session_count"], 2)
        self.assertEqual(payload["session"]["claims_age_bucket_hours"], 0)
        self.assertEqual(payload["session"]["expiry_bucket_hours"], 4)
        self.assertEqual(payload["unmanaged_action_empty_rules"]["total"], 1)
        self.assertEqual(payload["unmanaged_action_empty_rules"]["local_admins"], 1)
        self.assertNotIn("alice", json.dumps(payload))
        self.assertNotIn("example-org", json.dumps(payload))
        self.assertNotIn("session-", json.dumps(payload))
        self.assertNotIn("github_id", json.dumps(payload))

    def test_domain_does_not_trust_persisted_admin_role(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
            clear=True,
        ):
            response = build_activation_preflight_response(
                trace_id="trace",
                github_id=123,
                active_record=_record(_policy(human_rule=False)),
                sessions=(_session(now=now, role="admin"),),
                now=now,
            )
        self.assertEqual(response.evaluation.decision, "denied")
        self.assertEqual(response.evaluation.reason_code, "principal_role_restricted")

    def test_domain_supports_legacy_managed_secret_key_bootstrap(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        legacy_key_env = control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS[0]
        with patch.dict(
            os.environ,
            {legacy_key_env: Fernet.generate_key().decode()},
            clear=True,
        ):
            response = build_activation_preflight_response(
                trace_id="trace",
                github_id=123,
                active_record=_record(_policy()),
                sessions=(_session(now=now),),
                now=now,
            )
        self.assertTrue(response.session.identity_fingerprint.startswith("identity_"))

    def test_domain_fails_closed_for_stale_ambiguous_and_truncated_sessions(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        record = _record(_policy())
        with self.subTest("stale"):
            with self.assertRaisesRegex(ActivationPreflightFailure, "claims are stale"):
                build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=(
                        _session(
                            now=now,
                            created_offset=timedelta(hours=25),
                        ),
                    ),
                    now=now,
                )
        with self.subTest("ambiguous"):
            divergent = LaunchplaneHumanSession(
                session_id="divergent",
                identity=GitHubHumanIdentity(
                    login="alice",
                    github_id=123,
                    name="",
                    email="",
                    organizations=frozenset({"other-org"}),
                    teams=frozenset(),
                    role="admin",
                ),
                created_at=now - timedelta(hours=2),
                expires_at=now + timedelta(days=1),
            )
            with self.assertRaisesRegex(ActivationPreflightFailure, "ambiguous"):
                build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=(_session(now=now), divergent),
                    now=now,
                )
        with self.subTest("truncated"):
            with self.assertRaisesRegex(ActivationPreflightFailure, "too many"):
                build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=tuple(_session(now=now) for _ in range(9)),
                    now=now,
                )
        with self.subTest("stale unexpired sessions do not consume current-session bound"):
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
                clear=True,
            ):
                response = build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=(
                        _session(now=now),
                        *(
                            _session(
                                now=now,
                                created_offset=timedelta(hours=25),
                            )
                            for _ in range(8)
                        ),
                    ),
                    now=now,
                )
            self.assertEqual(response.session.session_count, 1)
        with self.subTest("history truncated"):
            with self.assertRaisesRegex(ActivationPreflightFailure, "history"):
                build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=tuple(
                        _session(now=now, expires_offset=timedelta(hours=-1))
                        for _ in range(ACTIVATION_PREFLIGHT_STORAGE_SCAN_LIMIT + 1)
                    ),
                    now=now,
                )
        with self.subTest("payload mismatch"):
            with self.assertRaisesRegex(ActivationPreflightFailure, "lookup key"):
                build_activation_preflight_response(
                    trace_id="trace",
                    github_id=123,
                    active_record=record,
                    sessions=(_session(now=now, github_id=456),),
                    now=now,
                )

    def test_postgres_session_lookup_is_bounded_and_non_mutating(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{Path(directory) / 'db'}")
            store.ensure_schema()
            expired = _session(
                now=now, created_offset=timedelta(hours=2), expires_offset=timedelta(hours=-1)
            )
            store.write_session(expired)
            offset_session = LaunchplaneHumanSession(
                session_id="offset-session",
                identity=_identity(),
                created_at=datetime.fromisoformat("2026-08-23T00:30:00-04:00"),
                expires_at=datetime.fromisoformat("2026-08-24T00:30:00-04:00"),
            )
            other_identity = _session(now=now, github_id=456)
            store.write_session(offset_session)
            store.write_session(other_identity)
            found = store.read_human_sessions_for_github_id_without_cleanup(
                123,
                limit=10,
            )
            self.assertEqual(
                {session.session_id for session in found},
                {expired.session_id, offset_session.session_id},
            )
            self.assertEqual(found[0].session_id, offset_session.session_id)
            self.assertIsNotNone(store.read_session_without_cleanup(expired.session_id))
            store.close()

    async def test_http_route_is_bearer_only_no_store_and_no_writes(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db'}")
            store.ensure_schema()
            record = _record(_policy())
            store.seed_authz_policy_if_absent(record)
            stored_session = _session(now=now)
            store.write_session(stored_session)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=record.policy,
                authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(
                    record.policy,
                    policy_sha256=record.policy_sha256,
                    source="db",
                    record_id=record.record_id,
                    revision=record.revision,
                ),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_admin_token="admin-token",
                    local_admin_subject="authz-admin",
                    local_admin_token_label="authz-admin-label",
                    local_operator_token="operator-token",
                    local_operator_subject="operator",
                    local_operator_token_label="operator-label",
                ),
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
                clear=True,
            ):
                with (
                    patch.object(store, "write_authz_denial_record", side_effect=AssertionError),
                    patch.object(store, "write_session", side_effect=AssertionError),
                    patch.object(store, "delete_session", side_effect=AssertionError),
                    patch.object(
                        store, "write_session_if_csrf_generation", side_effect=AssertionError
                    ),
                    patch.object(store, "write_idempotency_record", side_effect=AssertionError),
                    patch.object(store, "write_outbox_delivery_record", side_effect=AssertionError),
                    patch.object(
                        store, "write_privileged_operation_plan", side_effect=AssertionError
                    ),
                    patch.object(store, "write_secret_audit_event", side_effect=AssertionError),
                ):
                    async with lifespan_client(app) as client:
                        response = await client.post(
                            "/v1/authz-diagnostics/activation-preflight/read",
                            headers={"Authorization": "Bearer admin-token"},
                            json={"github_id": 123},
                        )
                        operator_response = await client.post(
                            "/v1/authz-diagnostics/activation-preflight/read",
                            headers={"Authorization": "Bearer operator-token"},
                            json={"github_id": 123},
                        )
                        invalid_response = await client.post(
                            "/v1/authz-diagnostics/activation-preflight/read",
                            headers={"Authorization": "Bearer admin-token"},
                            json={},
                        )
                        oversized_response = await client.post(
                            "/v1/authz-diagnostics/activation-preflight/read",
                            headers={"Authorization": "Bearer admin-token"},
                            json={"github_id": 2**63},
                        )
                        with patch.object(
                            authz_activation_preflight,
                            "build_activation_preflight_response",
                            side_effect=RuntimeError("unexpected"),
                        ):
                            unavailable_response = await client.post(
                                "/v1/authz-diagnostics/activation-preflight/read",
                                headers={"Authorization": "Bearer admin-token"},
                                json={"github_id": 123},
                            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            response_text = response.text
            for secret_value in (
                stored_session.session_id,
                stored_session.identity.login,
                stored_session.identity.name,
                stored_session.identity.email,
                *stored_session.identity.organizations,
                *stored_session.identity.teams,
                "admin-token",
                "authz-admin",
                "authz-admin-label",
            ):
                self.assertNotIn(secret_value, response_text)
            self.assertEqual(operator_response.status_code, 403)
            self.assertEqual(operator_response.headers["cache-control"], "no-store")
            self.assertEqual(invalid_response.status_code, 400)
            self.assertEqual(invalid_response.headers["cache-control"], "no-store")
            self.assertEqual(oversized_response.status_code, 400)
            self.assertEqual(oversized_response.headers["cache-control"], "no-store")
            self.assertEqual(unavailable_response.status_code, 503)
            self.assertEqual(unavailable_response.headers["cache-control"], "no-store")
            self.assertEqual(
                store.read_session_without_cleanup(stored_session.session_id), stored_session
            )
            self.assertEqual(store.list_authz_policy_records(status="active"), (record,))
            store.close()

    async def test_http_route_reauthorizes_against_active_database_policy(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db'}")
            store.ensure_schema()
            active_record = _record(_policy(local_admin=False))
            store.seed_authz_policy_if_absent(active_record)
            store.write_session(_session(now=now))
            runtime_policy = _policy()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=runtime_policy,
                authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(runtime_policy),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_admin_token="admin-token",
                    local_admin_subject="authz-admin",
                    local_admin_token_label="authz-admin-label",
                ),
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/authz-diagnostics/activation-preflight/read",
                    headers={"Authorization": "Bearer admin-token"},
                    json={"github_id": 123},
                )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers["cache-control"], "no-store")
            store.close()


class AuthzActivationPreflightCliTests(unittest.TestCase):
    def test_cli_reads_named_environment_only(self) -> None:
        calls: list[dict[str, object]] = []

        def post(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"status": "ok"}

        register_policy_profile_commands(
            click.Group("main"),
            callbacks=PolicyProfileCliCallbacks(post_launchplane_service_json=post),
        )
        runner = CliRunner()
        with patch.dict(os.environ, {"LOCAL_ADMIN_TEST_TOKEN": "secret-token"}, clear=True):
            result = runner.invoke(
                cast(Command, authz_policies),
                [
                    "activation-preflight",
                    "--service-url",
                    "https://launchplane.example",
                    "--bearer-token-env",
                    "LOCAL_ADMIN_TEST_TOKEN",
                    "--github-id",
                    "123",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0]["bearer_token"], "secret-token")
        self.assertNotIn("secret-token", result.output)
        self.assertEqual(calls[0]["session_cookie"], "")
        self.assertEqual(calls[0]["path"], "/v1/authz-diagnostics/activation-preflight/read")
