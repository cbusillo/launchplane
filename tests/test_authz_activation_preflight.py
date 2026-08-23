from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Literal

import click
from click.testing import CliRunner
from cryptography.fernet import Fernet

from control_plane import secrets as control_plane_secrets
from control_plane.authz_activation_preflight import (
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


def _policy(*, human_rule: bool = True, local_admin: bool = True) -> LaunchplaneAuthzPolicy:
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
        local_admins=(
            LocalAdminPolicyRule(
                subjects=("authz-admin",),
                token_labels=("authz-admin-label",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(
                    "authz_policy_health.read",
                    "authz_policy_effective_access.read",
                ),
            ),
        )
        if local_admin
        else (),
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
        session = _session(now=now, role="read_only")
        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
            clear=True,
        ):
            response = build_activation_preflight_response(
                trace_id="trace",
                github_id=123,
                active_record=_record(_policy()),
                sessions=(session,),
                now=now,
            )
        payload = response.model_dump(mode="json")
        self.assertEqual(payload["evaluation"]["decision"], "allowed")
        self.assertNotIn("alice", json.dumps(payload))
        self.assertNotIn("example-org", json.dumps(payload))
        self.assertNotIn("session-", json.dumps(payload))

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
            divergent = _session(now=now)
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

    def test_postgres_session_lookup_is_ordered_bounded_and_non_mutating(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{Path(directory) / 'db'}")
            store.ensure_schema()
            expired = _session(
                now=now, created_offset=timedelta(hours=2), expires_offset=timedelta(hours=-1)
            )
            store.write_session(expired)
            sessions = []
            for index in range(10):
                session = _session(
                    now=now,
                    created_offset=timedelta(minutes=index),
                    expires_offset=timedelta(hours=index + 1),
                )
                session = LaunchplaneHumanSession(
                    session_id=f"session-{index}",
                    identity=session.identity,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                )
                store.write_session(session)
                sessions.append(session)
            found = store.read_human_sessions_for_github_id_without_cleanup(
                123,
                limit=9,
                now=now,
            )
            self.assertEqual(len(found), 9)
            self.assertEqual(found[0].session_id, "session-9")
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
            store.write_session(_session(now=now, role="read_only"))
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
                    patch.object(store, "delete_session", side_effect=AssertionError),
                    patch.object(
                        store, "write_session_if_csrf_generation", side_effect=AssertionError
                    ),
                ):
                    async with lifespan_client(app) as client:
                        response = await client.post(
                            "/v1/authz-diagnostics/activation-preflight/read",
                            headers={"Authorization": "Bearer admin-token"},
                            json={"github_id": 123},
                        )
            self.assertEqual(response.status_code, 200)
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
                authz_policies,
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
