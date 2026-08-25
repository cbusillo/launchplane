from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI

from control_plane import secrets as control_plane_secrets
from control_plane.authz_activation_preflight import (
    ActivationPreflightFailure,
    build_activation_preflight_self_response,
)
from control_plane.cli_policy_profiles import authz_policies
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
    SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS,
)
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
    *,
    role: Literal["read_only", "admin"] = "read_only",
    github_id: int = 123,
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=github_id,
        name="Sensitive Name",
        email="alice@example.test",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/platform"}),
        role=role,
    )


def _session(
    *,
    now: datetime,
    role: Literal["read_only", "admin"] = "read_only",
    github_id: int = 123,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    session_id: str = "session-sensitive-value",
) -> LaunchplaneHumanSession:
    return LaunchplaneHumanSession(
        session_id=session_id,
        identity=_identity(role=role, github_id=github_id),
        created_at=created_at or now - timedelta(minutes=5),
        expires_at=expires_at or now + timedelta(days=1),
    )


def _policy(*, grant: bool = True) -> LaunchplaneAuthzPolicy:
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
        if grant
        else (),
    )


def _record(
    policy: LaunchplaneAuthzPolicy,
    *,
    revision: int = 1,
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        source="test:activation-preflight-self",
        updated_at="2026-08-25T00:00:00+00:00",
        policy=policy,
        policy_sha256=digest,
    )


def _session_manager(
    *,
    store: InMemoryHumanSessionStore,
    now: datetime,
) -> HumanSessionManager:
    return HumanSessionManager(
        config=GitHubOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://launchplane.example.test",
            session_secret="session-signing-secret",
            cookie_secure=False,
        ),
        session_store=store,
        now=lambda: now,
    )


def _app(
    *,
    root: Path,
    store: object,
    record: LaunchplaneAuthzPolicyRecord,
    session_manager: HumanSessionManager | None,
) -> FastAPI:
    return create_launchplane_fastapi_app(
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
        human_session_manager=session_manager,
        control_plane_root_path=root,
        state_dir=root / "state",
    )


class AuthzActivationPreflightDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        secret_keys = patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
            clear=False,
        )
        secret_keys.start()
        self.addCleanup(secret_keys.stop)

    def test_rederives_role_and_returns_only_bounded_self_evidence(self) -> None:
        now = datetime(2026, 8, 25, 14, 37, 42, tzinfo=timezone.utc)
        record = _record(_policy())
        response = build_activation_preflight_self_response(
            trace_id="trace-id",
            session=_session(now=now),
            active_record=record,
            now=now,
        )

        self.assertEqual(
            response.model_dump(mode="json"),
            {
                "status": "ok",
                "trace_id": "trace-id",
                "decision": "allowed",
                "evaluated_at": "2026-08-25T14:00:00Z",
                "policy_generation": response.policy_generation,
            },
        )
        self.assertRegex(response.policy_generation, r"^[0-9a-f]{64}$")
        response_text = response.model_dump_json()
        for sensitive_value in (
            "session-sensitive-value",
            "alice",
            "Sensitive Name",
            "alice@example.test",
            "example-org",
            "example-org/platform",
            record.record_id,
            record.policy_sha256,
        ):
            self.assertNotIn(sensitive_value, response_text)

    def test_ignores_persisted_admin_role_and_changes_generation_with_policy_record(self) -> None:
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        denied_record = _record(_policy(grant=False))
        denied = build_activation_preflight_self_response(
            trace_id="trace-denied",
            session=_session(now=now, role="admin"),
            active_record=denied_record,
            now=now,
        )
        next_record = _record(_policy(grant=False), revision=2)
        next_response = build_activation_preflight_self_response(
            trace_id="trace-next",
            session=_session(now=now, role="admin"),
            active_record=next_record,
            now=now,
        )

        self.assertEqual(denied.decision, "denied")
        self.assertNotEqual(denied.policy_generation, next_response.policy_generation)

    def test_rejects_invalid_session_evidence(self) -> None:
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        record = _record(_policy())
        invalid_sessions = (
            _session(now=now, expires_at=now),
            _session(now=now, created_at=now + timedelta(seconds=1)),
            replace(
                _session(now=now),
                identity=replace(_identity(), github_id=0),
            ),
            replace(
                _session(now=now),
                identity=replace(_identity(), login=""),
            ),
        )

        for invalid_session in invalid_sessions:
            with self.subTest(session=invalid_session):
                with self.assertRaises(ActivationPreflightFailure) as failure:
                    build_activation_preflight_self_response(
                        trace_id="trace-invalid",
                        session=invalid_session,
                        active_record=record,
                        now=now,
                    )
                self.assertEqual(failure.exception.status_code, 401)


class AuthzActivationPreflightHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        secret_keys = patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: _key_ring()},
            clear=False,
        )
        secret_keys.start()
        self.addCleanup(secret_keys.stop)

    async def test_self_route_is_cookie_only_no_store_redacted_and_non_mutating(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db'}")
            store.ensure_schema()
            record = _record(_policy())
            store.seed_authz_policy_if_absent(record)
            session_store = InMemoryHumanSessionStore()
            manager = _session_manager(store=session_store, now=now)
            valid_session = _session(now=now)
            expired_session = _session(
                now=now,
                session_id="expired-session",
                created_at=now - timedelta(days=2),
                expires_at=now - timedelta(seconds=1),
            )
            stale_session = _session(
                now=now,
                session_id="stale-session",
                created_at=now - timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS + 1),
            )
            for session in (valid_session, expired_session, stale_session):
                session_store.write_session(session)
            valid_cookie = manager.session_cookie_header(valid_session)
            app = _app(
                root=root,
                store=store,
                record=record,
                session_manager=manager,
            )

            with (
                patch.object(session_store, "write_session", side_effect=AssertionError),
                patch.object(session_store, "delete_session", side_effect=AssertionError),
                patch.object(
                    session_store,
                    "write_session_if_csrf_generation",
                    side_effect=AssertionError,
                ),
                patch.object(store, "write_authz_denial_record", side_effect=AssertionError),
                patch.object(store, "write_idempotency_record", side_effect=AssertionError),
                patch.object(store, "write_outbox_delivery_record", side_effect=AssertionError),
                patch.object(
                    store,
                    "write_privileged_operation_plan",
                    side_effect=AssertionError,
                ),
            ):
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={"Cookie": valid_cookie},
                    )
                    bearer_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={
                            "Authorization": "Bearer forbidden",
                            "Cookie": valid_cookie,
                        },
                    )
                    missing_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self"
                    )
                    invalid_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={"Cookie": "launchplane_session=invalid.signature"},
                    )
                    expired_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={"Cookie": manager.session_cookie_header(expired_session)},
                    )
                    stale_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={"Cookie": manager.session_cookie_header(stale_session)},
                    )
                    query_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self?github_id=999",
                        headers={"Cookie": valid_cookie},
                    )
                    body_response = await client.request(
                        "GET",
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers={"Cookie": valid_cookie},
                        content=b"{}",
                    )
                    old_route_response = await client.post(
                        "/v1/authz-diagnostics/activation-preflight/read",
                        headers={"Authorization": "Bearer forbidden"},
                        json={"github_id": 123},
                    )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                set(response.json()),
                {"status", "trace_id", "decision", "evaluated_at", "policy_generation"},
            )
            self.assertEqual(response.json()["decision"], "allowed")
            self.assertNotIn("set-cookie", response.headers)
            for sensitive_value in (
                valid_session.session_id,
                valid_session.identity.login,
                valid_session.identity.name,
                valid_session.identity.email,
                *valid_session.identity.organizations,
                *valid_session.identity.teams,
                record.record_id,
                record.policy_sha256,
                "forbidden",
            ):
                self.assertNotIn(sensitive_value, response.text)
            for rejected_response, status_code in (
                (bearer_response, 403),
                (missing_response, 401),
                (invalid_response, 401),
                (expired_response, 401),
                (stale_response, 401),
                (query_response, 400),
                (body_response, 400),
            ):
                self.assertEqual(rejected_response.status_code, status_code, rejected_response.text)
                self.assertEqual(rejected_response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(old_route_response.status_code, 404)
            self.assertEqual(
                session_store.read_session_without_cleanup(valid_session.session_id), valid_session
            )
            self.assertEqual(store.list_authz_policy_records(status="active"), (record,))
            store.close()

    async def test_self_route_fails_closed_for_policy_storage_states(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db'}")
            store.ensure_schema()
            record = _record(_policy())
            session_store = InMemoryHumanSessionStore()
            manager = _session_manager(store=session_store, now=now)
            session = _session(now=now)
            session_store.write_session(session)
            app = _app(
                root=root,
                store=store,
                record=record,
                session_manager=manager,
            )
            headers = {"Cookie": manager.session_cookie_header(session)}

            async with lifespan_client(app) as client:
                missing_response = await client.get(
                    "/v1/authz-diagnostics/activation-preflight/self",
                    headers=headers,
                )
                store.seed_authz_policy_if_absent(record)
                with patch.object(
                    store,
                    "list_authz_policy_records",
                    return_value=(record, record),
                ):
                    ambiguous_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers=headers,
                    )
                with patch.object(
                    control_plane_secrets,
                    "keyed_secret_payload_fingerprint",
                    side_effect=RuntimeError("secret key ring unavailable"),
                ):
                    generation_unavailable_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers=headers,
                    )
                with patch.object(
                    session_store,
                    "read_session_without_cleanup",
                    side_effect=RuntimeError("session storage unavailable"),
                ):
                    session_unavailable_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers=headers,
                    )
                denied_record = _record(_policy(grant=False), revision=2)
                with patch.object(
                    store,
                    "list_authz_policy_records",
                    return_value=(denied_record,),
                ):
                    denied_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers=headers,
                    )
                with patch.object(
                    store,
                    "list_authz_policy_records",
                    side_effect=RuntimeError("database unavailable"),
                ):
                    unavailable_response = await client.get(
                        "/v1/authz-diagnostics/activation-preflight/self",
                        headers=headers,
                    )

            self.assertEqual(missing_response.status_code, 503, missing_response.text)
            self.assertEqual(ambiguous_response.status_code, 409, ambiguous_response.text)
            self.assertEqual(unavailable_response.status_code, 503, unavailable_response.text)
            self.assertEqual(
                generation_unavailable_response.status_code,
                503,
                generation_unavailable_response.text,
            )
            self.assertEqual(
                session_unavailable_response.status_code,
                503,
                session_unavailable_response.text,
            )
            self.assertEqual(denied_response.status_code, 200, denied_response.text)
            self.assertEqual(denied_response.json()["decision"], "denied")
            for response in (
                missing_response,
                ambiguous_response,
                unavailable_response,
                generation_unavailable_response,
                session_unavailable_response,
                denied_response,
            ):
                self.assertEqual(response.headers["cache-control"], "no-store")
            store.close()

    def test_openapi_exposes_only_parameterless_get_and_old_helper_is_deleted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record = _record(_policy())
            app = _app(root=root, store=object(), record=record, session_manager=None)

        schema = app.openapi()
        operation = schema["paths"]["/v1/authz-diagnostics/activation-preflight/self"]
        self.assertEqual(set(operation), {"get"})
        self.assertEqual(
            {(parameter["name"], parameter["in"]) for parameter in operation["get"]["parameters"]},
            {("Authorization", "header"), ("Cookie", "header")},
        )
        self.assertFalse(
            any(parameter["in"] == "query" for parameter in operation["get"]["parameters"])
        )
        self.assertNotIn("requestBody", operation["get"])
        self.assertNotIn("/v1/authz-diagnostics/activation-preflight/read", schema["paths"])
        self.assertNotIn(
            "activation-preflight",
            authz_policies.commands,
        )
