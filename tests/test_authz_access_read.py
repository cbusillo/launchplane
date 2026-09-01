from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.authz_access_read import (
    AUTHZ_DENIAL_EXPLANATION_READ_ACTION,
    AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
    AUTHZ_POLICY_HEALTH_READ_ACTION,
    AUTHZ_REPOSITORY_SCOPE_READ_ACTION,
    EFFECTIVE_ACCESS_READ_ACTION,
    AuthzPolicyCandidatePreviewRequest,
    EffectiveAccessEvaluateRequest,
)
from control_plane.contracts.authz_denial_record import build_authz_denial_record
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane import secrets as control_plane_secrets
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import (
    AuthzEvaluation,
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
    build_browser_mutation_request_headers,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise ValueError(f"Unexpected GitHub token: {token}")


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _nested_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _nested_mapping_keys(nested_value)
        }
    if isinstance(value, list):
        return {
            nested_key
            for nested_value in value
            for nested_key in _nested_mapping_keys(nested_value)
        }
    return set()


def _seed_policy(
    store: PostgresRecordStore,
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return store.seed_authz_policy_if_absent(
        LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
            revision=1,
            status="active",
            source="test:authz-access-read",
            updated_at="2026-08-18T12:00:00+00:00",
            policy_sha256=digest,
            policy=policy,
        )
    )


def _support_reader_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "local_operators": [
                {
                    "subjects": ["support-reader"],
                    "token_labels": ["support-reader-label"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [AUTHZ_DENIAL_EXPLANATION_READ_ACTION],
                }
            ],
        }
    )


def _repository_scope_reader_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "repository": "example/private-product",
                    "repository_id": "123456",
                    "repository_owner_id": "654321",
                    "actions": ["deploy.write"],
                }
            ],
            "local_operators": [
                {
                    "subjects": ["support-reader"],
                    "token_labels": ["support-reader-label"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [AUTHZ_REPOSITORY_SCOPE_READ_ACTION],
                }
            ],
        }
    )


def _app(
    *,
    root: Path,
    store: object,
    record: LaunchplaneAuthzPolicyRecord,
    human_session_manager: HumanSessionManager | None = None,
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
        human_session_manager=human_session_manager,
        bearer_identity_config=BearerIdentityConfig(
            local_admin_token="admin-token",
            local_admin_subject="authz-admin",
            local_admin_token_label="authz-admin-label",
            local_operator_token="support-token",
            local_operator_subject="support-reader",
            local_operator_token_label="support-reader-label",
        ),
        control_plane_root_path=root,
        state_dir=root / "state",
    )


@contextmanager
def _database_app(
    policy: LaunchplaneAuthzPolicy,
    *,
    human_session_manager: HumanSessionManager | None = None,
) -> Iterator[tuple[PostgresRecordStore, LaunchplaneAuthzPolicyRecord, FastAPI]]:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
        store.ensure_schema()
        record = _seed_policy(store, policy)
        app = _app(
            root=root,
            store=store,
            record=record,
            human_session_manager=human_session_manager,
        )
        try:
            yield store, record, app
        finally:
            store.close()


def _repository_scope_product_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="example-product",
        display_name="Example Product",
        repository="example/private-product",
        repository_id="123456",
        repository_owner_id="654321",
        driver_id="generic-web",
        image=ProductImageProfile(),
        updated_at="2026-08-20T21:00:00+00:00",
        source="test:authz-access-read",
    )


def _repository_scope_key_ring(value: str) -> str:
    encoded_key = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())
    return json.dumps(
        {
            "active_key_id": "repository-scope-key",
            "keys": {"repository-scope-key": encoded_key.decode()},
        }
    )


class AuthzAccessReadHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_reads_complete_redacted_repository_scope_without_mutation(
        self,
    ) -> None:
        with _database_app(_repository_scope_reader_policy()) as (store, record, app):
            store.write_product_profile_record(_repository_scope_product_profile())
            policy_records_before = store.list_authz_policy_records()
            repository_scope_operation = app.openapi()["paths"][
                "/v1/authz-diagnostics/repository-scope/read"
            ]["post"]
            self.assertEqual(
                repository_scope_operation["operationId"],
                "read_authz_repository_scope",
            )
            with patch.dict(
                os.environ,
                {
                    control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: (
                        _repository_scope_key_ring("authz-repository-scope-test-key")
                    )
                },
                clear=True,
            ):
                with (
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore.write_authz_denial_record",
                        side_effect=AssertionError(
                            "successful repository scope reads must not write"
                        ),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore.write_product_profile_record",
                        side_effect=AssertionError(
                            "repository scope reads must not mutate products"
                        ),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore.write_repository_human_role_policy_record",
                        side_effect=AssertionError(
                            "repository scope reads must not mutate repository records"
                        ),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore.write_every_code_work_request_record",
                        side_effect=AssertionError(
                            "repository scope reads must not mutate work graph"
                        ),
                    ),
                ):
                    async with lifespan_client(app) as client:
                        response = await client.post(
                            "/v1/authz-diagnostics/repository-scope/read",
                            headers={"Authorization": "Bearer support-token"},
                            json={
                                "candidates": [
                                    {
                                        "repository": "example/private-product",
                                        "repository_id": "123456",
                                        "repository_owner_id": "654321",
                                    }
                                ]
                            },
                        )
            policy_records_after = store.list_authz_policy_records()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(policy_records_after, policy_records_before)
        payload = response.json()
        self.assertEqual(payload["coverage"]["state"], "complete")
        self.assertEqual(payload["coverage"]["repository_count"], 1)
        self.assertEqual(payload["coverage"]["matched_repository_count"], 1)
        self.assertEqual(payload["candidates"][0]["state"], "matched")
        self.assertEqual(
            payload["candidates"][0]["memberships"],
            ["product", "authorization_chain"],
        )
        self.assertEqual(
            payload["provenance"]["authorization_chain"]["record_id"], record.record_id
        )
        serialized = json.dumps(payload, sort_keys=True)
        for private_value in (
            "example/private-product",
            "123456",
            "654321",
            "support-reader",
            "support-reader-label",
            "deploy.write",
        ):
            self.assertNotIn(private_value, serialized)

    async def test_browser_repository_scope_post_requires_origin(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "github_ids": [12345],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_REPOSITORY_SCOPE_READ_ACTION],
                    }
                ],
            }
        )
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=GitHubOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                public_url="https://launchplane.example",
                session_secret="session-secret",
                cookie_secure=False,
            ),
            session_store=session_store,
        )
        session = session_manager.issue(
            GitHubHumanIdentity(
                login="browser-admin",
                github_id=12345,
                name="Browser Admin",
                email="browser-admin@example.test",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
        )
        headers = build_browser_mutation_request_headers(
            origin="https://launchplane.example",
            csrf_token=session_manager.csrf_token(session),
        )
        headers["Cookie"] = session_manager.session_cookie_header(session).split(";", 1)[0]
        headers.pop("Origin")
        with _database_app(policy, human_session_manager=session_manager) as (
            _store,
            _record,
            app,
        ):
            session_before = session_store.read_session(session.session_id)
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/authz-diagnostics/repository-scope/read",
                    headers=headers,
                    json={"candidates": []},
                )
            session_after = session_store.read_session(session.session_id)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "browser_mutation_denied")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(session_after, session_before)

    async def test_repository_scope_authorization_precedes_route_database_reads(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_operators": [
                    {
                        "subjects": ["support-reader"],
                        "token_labels": ["support-reader-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_DENIAL_EXPLANATION_READ_ACTION],
                    }
                ],
            }
        )
        with _database_app(policy) as (store, _record, app):
            with (
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.list_authz_policy_records",
                    side_effect=AssertionError("authorization must precede active policy reads"),
                ),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.list_product_profile_records",
                    side_effect=AssertionError("authorization must precede product reads"),
                ),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.list_repository_human_role_policy_records",
                    side_effect=AssertionError("authorization must precede repository reads"),
                ),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.list_every_code_work_request_records",
                    side_effect=AssertionError("authorization must precede work graph reads"),
                ),
            ):
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-diagnostics/repository-scope/read",
                        headers={"Authorization": "Bearer support-token"},
                        json={"candidates": []},
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_repository_scope_rechecks_fresh_policy_and_only_records_denial(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            active_record = _seed_policy(store, _support_reader_policy())
            runtime_policy = _repository_scope_reader_policy()
            runtime_digest = authz_policy_sha256(runtime_policy)
            runtime_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=runtime_digest,
                ),
                revision=2,
                status="active",
                source="test:runtime-only",
                updated_at="2026-08-20T22:00:00+00:00",
                policy_sha256=runtime_digest,
                policy=runtime_policy,
            )
            app = _app(root=root, store=store, record=runtime_record)
            try:
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-diagnostics/repository-scope/read",
                        headers={"Authorization": "Bearer support-token"},
                        json={"candidates": []},
                    )
                denial_record = store.read_authz_denial_record(
                    trace_id=response.json()["trace_id"],
                    observed_at="2026-08-20T23:00:00+00:00",
                )
                policy_records = store.list_authz_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(policy_records, (active_record,))
        self.assertIsNotNone(denial_record)
        assert denial_record is not None
        self.assertEqual(
            denial_record.route_path,
            "/v1/authz-diagnostics/repository-scope/read",
        )
        serialized = json.dumps(denial_record.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn("example/private-product", serialized)
        self.assertNotIn("123456", serialized)

    async def test_repository_scope_fails_closed_without_handle_root_or_valid_request(
        self,
    ) -> None:
        with _database_app(_repository_scope_reader_policy()) as (store, _record, app):
            store.write_product_profile_record(_repository_scope_product_profile())
            async with lifespan_client(app) as client:
                with patch.dict(os.environ, {}, clear=True):
                    unavailable_response = await client.post(
                        "/v1/authz-diagnostics/repository-scope/read",
                        headers={"Authorization": "Bearer support-token"},
                        json={"candidates": []},
                    )
                with patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                            "legacy-low-entropy-key"
                        )
                    },
                    clear=True,
                ):
                    legacy_key_response = await client.post(
                        "/v1/authz-diagnostics/repository-scope/read",
                        headers={"Authorization": "Bearer support-token"},
                        json={"candidates": []},
                    )
                invalid_response = await client.post(
                    "/v1/authz-diagnostics/repository-scope/read",
                    headers={"Authorization": "Bearer support-token"},
                    json={
                        "candidates": [
                            {
                                "repository": "example/private-product",
                                "repository_id": "private-id-must-not-echo",
                            }
                        ]
                    },
                )

        self.assertEqual(unavailable_response.status_code, 503, unavailable_response.text)
        self.assertEqual(
            unavailable_response.json()["error"]["code"],
            "authz_repository_scope_handle_unavailable",
        )
        self.assertEqual(legacy_key_response.status_code, 503, legacy_key_response.text)
        self.assertEqual(
            legacy_key_response.json()["error"]["code"],
            "authz_repository_scope_handle_unavailable",
        )
        self.assertEqual(invalid_response.status_code, 400, invalid_response.text)
        self.assertEqual(invalid_response.headers["cache-control"], "no-store")
        self.assertEqual(invalid_response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("private-id-must-not-echo", invalid_response.text)

    async def test_administrator_reads_bounded_policy_health_without_selector_leakage(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "administrator_quorum": 1,
                    "local_admins": [
                        {
                            "managed_set_id": "operator.authz-health",
                            "managed_rule_id": "reader",
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                AUTHZ_POLICY_HEALTH_READ_ACTION,
                                "authz_policy_grant.write",
                            ],
                        },
                    ],
                    "github_humans": [
                        {
                            "managed_set_id": "operator.recovery",
                            "managed_rule_id": "independent-admin",
                            "github_ids": [2002],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            records_before_read = store.list_authz_policy_records()
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "write_authz_denial_record",
                        side_effect=AssertionError("successful health reads must not write"),
                    ):
                        response = await client.get(
                            "/v1/authz-diagnostics/active-policy/health",
                            headers={"Authorization": "Bearer admin-token"},
                        )
                records_after_read = store.list_authz_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(records_after_read, records_before_read)
        payload = response.json()
        self.assertEqual(payload["policy"]["record_id"], record.record_id)
        self.assertEqual(payload["policy"]["revision"], record.revision)
        self.assertEqual(payload["policy"]["policy_sha256"], record.policy_sha256)
        self.assertEqual(payload["health"]["state"], "attention_required")
        self.assertEqual(
            payload["health"]["reason_codes"], ["authz_policy_solo_administration_active"]
        )
        self.assertEqual(payload["health"]["administrator_quorum"], 1)
        self.assertEqual(payload["health"]["reachable_administrator_count"], 1)
        self.assertTrue(payload["health"]["solo_administration_active"])
        self.assertEqual(payload["managed_sets"]["total_count"], 2)
        self.assertEqual(
            [item["managed_set_id"] for item in payload["managed_sets"]["items"]],
            ["operator.authz-health", "operator.recovery"],
        )
        self.assertTrue(payload["reachable_administrators"]["policy_reachable"])
        self.assertTrue(payload["reachable_administrators"]["caller_has_policy_administration"])
        self.assertTrue(payload["reachable_administrators"]["quorum_satisfied"])
        self.assertTrue(payload["reachable_administrators"]["solo_administration_active"])
        serialized = json.dumps(payload, sort_keys=True)
        for secret_value in (
            "authz-admin",
            "authz-admin-label",
        ):
            self.assertNotIn(secret_value, serialized)
        self.assertTrue(
            {
                "managed_rule_id",
                "subjects",
                "token_labels",
                "actions",
                "products",
                "contexts",
            }.isdisjoint(_nested_mapping_keys(payload))
        )
        route = app.openapi()["paths"]["/v1/authz-diagnostics/active-policy/health"]
        self.assertEqual(route["get"]["operationId"], "read_authz_policy_health")

    async def test_administrator_previews_exact_candidate_policy_without_persistence_or_leakage(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            active_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "managed_set_id": "operator.authz-preview",
                            "managed_rule_id": "reader",
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
                                "authz_policy_grant.write",
                            ],
                        },
                        {
                            "managed_set_id": "operator.recovery",
                            "managed_rule_id": "independent-admin",
                            "subjects": ["independent-admin-secret"],
                            "token_labels": ["independent-token-secret"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        },
                    ],
                }
            )
            candidate_payload = active_policy.model_dump(mode="json")
            candidate_payload["local_operators"] = [
                {
                    "managed_set_id": "operator.candidate-secret",
                    "managed_rule_id": "probe-reader-secret",
                    "subjects": ["probe-subject-secret"],
                    "token_labels": ["probe-token-secret"],
                    "products": ["example"],
                    "contexts": ["testing"],
                    "actions": ["artifact_protection.read"],
                }
            ]
            candidate_policy = LaunchplaneAuthzPolicy.model_validate(candidate_payload)
            record = _seed_policy(store, active_policy)
            app = _app(root=root, store=store, record=record)
            records_before_preview = store.list_authz_policy_records()
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "write_authz_denial_record",
                        side_effect=AssertionError(
                            "candidate previews must not persist denial rows"
                        ),
                    ):
                        response = await client.post(
                            "/v1/authz-diagnostics/candidate-policy/preview",
                            headers={"Authorization": "Bearer admin-token"},
                            json={
                                "candidate_policy": candidate_policy.model_dump(mode="json"),
                                "probes": [
                                    {
                                        "principal": {
                                            "principal_type": "local_operator",
                                            "subject": "probe-subject-secret",
                                            "token_label": "probe-token-secret",
                                        },
                                        "action": "artifact_protection.read",
                                        "product": "example",
                                        "context": "testing",
                                        "target_scope": "context",
                                    }
                                ],
                            },
                        )
                records_after_preview = store.list_authz_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(records_after_preview, records_before_preview)
        payload = response.json()
        self.assertEqual(payload["active_policy"]["record_id"], record.record_id)
        self.assertEqual(payload["active_policy"]["revision"], record.revision)
        self.assertEqual(payload["active_policy"]["policy_sha256"], record.policy_sha256)
        self.assertTrue(payload["diff"]["changed"])
        self.assertEqual(payload["diff"]["added_rule_count"], 1)
        self.assertEqual(payload["probes"][0]["active_evaluation"]["decision"], "denied")
        self.assertEqual(payload["probes"][0]["candidate_evaluation"]["decision"], "allowed")
        self.assertEqual(payload["probes"][0]["delta"], "granted")
        serialized = json.dumps(payload, sort_keys=True)
        for private_value in (
            "authz-admin",
            "authz-admin-label",
            "independent-admin-secret",
            "independent-token-secret",
            "operator.candidate-secret",
            "probe-reader-secret",
            "probe-subject-secret",
            "probe-token-secret",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertTrue(
            {
                "managed_set_id",
                "managed_rule_id",
                "subjects",
                "token_labels",
                "actions",
                "products",
                "contexts",
                "policy",
                "rule_sha256",
            }.isdisjoint(_nested_mapping_keys(payload))
        )
        route = app.openapi()["paths"]["/v1/authz-diagnostics/candidate-policy/preview"]
        self.assertEqual(route["post"]["operationId"], "preview_authz_candidate_policy")

    async def test_browser_administrator_preview_does_not_renew_or_rotate_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "managed_set_id": "operator.authz-preview",
                            "managed_rule_id": "browser-admin",
                            "github_ids": [12345],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION,
                                "authz_policy_grant.write",
                            ],
                        }
                    ],
                    "local_admins": [
                        {
                            "managed_set_id": "operator.recovery",
                            "managed_rule_id": "independent-admin",
                            "subjects": ["independent-admin"],
                            "token_labels": ["independent-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=GitHubOAuthConfig(
                    client_id="client-id",
                    client_secret="client-secret",
                    public_url="https://launchplane.example",
                    session_secret="session-secret",
                    cookie_secure=False,
                ),
                session_store=session_store,
            )
            session = session_manager.issue(
                GitHubHumanIdentity(
                    login="browser-admin",
                    github_id=12345,
                    name="Browser Admin",
                    email="browser-admin@example.test",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                )
            )
            app = _app(
                root=root,
                store=store,
                record=record,
                human_session_manager=session_manager,
            )
            csrf_token = session_manager.csrf_token(session)
            headers = build_browser_mutation_request_headers(
                origin="https://launchplane.example",
                csrf_token=csrf_token,
            )
            headers["Cookie"] = session_manager.session_cookie_header(session).split(";", 1)[0]
            session_before_preview = session_store.read_session(session.session_id)
            try:
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers=headers,
                        json={"candidate_policy": policy.model_dump(mode="json")},
                    )
                    rejected_headers = dict(headers)
                    rejected_headers.pop("X-CSRF-Token")
                    rejected_response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers=rejected_headers,
                        json={"candidate_policy": policy.model_dump(mode="json")},
                    )
                    absent_origin_headers = dict(headers)
                    absent_origin_headers.pop("Origin")
                    absent_origin_response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers=absent_origin_headers,
                        json={"candidate_policy": policy.model_dump(mode="json")},
                    )
                session_after_preview = session_store.read_session(session.session_id)
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(session_after_preview, session_before_preview)
        self.assertTrue(session_manager.csrf_token_is_valid(session, csrf_token))
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(rejected_response.status_code, 403, rejected_response.text)
        self.assertEqual(
            rejected_response.json()["error"]["code"],
            "browser_mutation_denied",
        )
        self.assertEqual(absent_origin_response.status_code, 403, absent_origin_response.text)
        self.assertEqual(
            absent_origin_response.json()["error"]["code"],
            "browser_mutation_denied",
        )

    async def test_unauthorized_candidate_preview_skips_route_database_reads(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "subjects": ["authz-admin"],
                        "token_labels": ["authz-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                    }
                ],
            }
        )
        with _database_app(policy) as (store, _record, app):
            async with lifespan_client(app) as client:
                with (
                    patch.object(
                        store,
                        "list_authz_policy_records",
                        create=True,
                        side_effect=AssertionError("unauthorized preview must not read DB policy"),
                    ),
                    patch.object(
                        store,
                        "write_authz_denial_record",
                        create=True,
                        side_effect=AssertionError(
                            "unauthorized preview must not persist denial evidence"
                        ),
                    ),
                ):
                    response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers={"Authorization": "Bearer admin-token"},
                        json={"candidate_policy": {"schema_version": 2}},
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_non_administrator_cannot_use_granted_candidate_preview_action(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_operators": [
                    {
                        "subjects": ["support-reader"],
                        "token_labels": ["support-reader-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION],
                    }
                ],
            }
        )
        with _database_app(policy) as (store, _record, app):
            async with lifespan_client(app) as client:
                with (
                    patch.object(
                        store,
                        "list_authz_policy_records",
                        create=True,
                        side_effect=AssertionError(
                            "non-administrator preview must not read DB policy"
                        ),
                    ),
                    patch.object(
                        store,
                        "write_authz_denial_record",
                        create=True,
                        side_effect=AssertionError(
                            "non-administrator preview must not persist denial evidence"
                        ),
                    ),
                ):
                    response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers={"Authorization": "Bearer support-token"},
                        json={"candidate_policy": {"schema_version": 2}},
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_candidate_preview_rechecks_fresh_database_policy_without_denial_write(
        self,
    ) -> None:
        runtime_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "subjects": ["authz-admin"],
                        "token_labels": ["authz-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION],
                    }
                ],
            }
        )
        denied_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "subjects": ["independent-admin"],
                        "token_labels": ["independent-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION],
                    }
                ],
            }
        )
        denied_digest = authz_policy_sha256(denied_policy)
        denied_record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                revision=2,
                policy_sha256=denied_digest,
            ),
            revision=2,
            status="active",
            source="test:authz-candidate-preview-recheck",
            updated_at="2026-08-20T19:45:00+00:00",
            policy_sha256=denied_digest,
            policy=denied_policy,
        )
        with _database_app(runtime_policy) as (store, _record, app):
            async with lifespan_client(app) as client:
                with (
                    patch.object(
                        store,
                        "list_authz_policy_records",
                        create=True,
                        return_value=(denied_record,),
                    ),
                    patch.object(
                        store,
                        "write_authz_denial_record",
                        create=True,
                        side_effect=AssertionError(
                            "fresh-record denial must not persist denial evidence"
                        ),
                    ),
                ):
                    response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers={"Authorization": "Bearer admin-token"},
                        json={"candidate_policy": {"schema_version": 2}},
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_candidate_preview_rejects_invalid_complete_managed_contract(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "subjects": ["authz-admin"],
                        "token_labels": ["authz-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION],
                    }
                ],
            }
        )
        with _database_app(policy) as (store, _record, app):
            async with lifespan_client(app) as client:
                with patch.object(
                    store,
                    "write_authz_denial_record",
                    create=True,
                    side_effect=AssertionError("invalid previews must not persist"),
                ):
                    response = await client.post(
                        "/v1/authz-diagnostics/candidate-policy/preview",
                        headers={"Authorization": "Bearer admin-token"},
                        json={
                            "candidate_policy": {
                                "schema_version": 2,
                                "local_operators": [
                                    {
                                        "managed_set_id": "operator.invalid",
                                        "managed_rule_id": "missing-actions",
                                        "subjects": ["operator"],
                                        "token_labels": ["operator-label"],
                                        "products": ["launchplane"],
                                        "contexts": ["launchplane"],
                                    }
                                ],
                            }
                        },
                    )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "authz_candidate_policy_invalid")

    async def test_policy_health_reports_missing_reachable_policy_administrator(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "managed_set_id": "operator.authz-health",
                            "managed_rule_id": "reader",
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/authz-diagnostics/active-policy/health",
                        headers={"Authorization": "Bearer admin-token"},
                    )
                    active_policy_response = await client.get(
                        "/v1/authz-policies/active",
                        headers={"Authorization": "Bearer admin-token"},
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["health"]["state"], "blocked")
        self.assertIn("authz_policy_admin_unreachable", payload["health"]["reason_codes"])
        self.assertFalse(payload["reachable_administrators"]["policy_reachable"])
        self.assertEqual(payload["reachable_administrators"]["rule_count"], 0)
        self.assertEqual(active_policy_response.status_code, 403, active_policy_response.text)

    async def test_policy_health_reports_when_caller_is_only_policy_administrator(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "managed_set_id": "operator.authz-health",
                            "managed_rule_id": "sole-administrator",
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                AUTHZ_POLICY_HEALTH_READ_ACTION,
                                "authz_policy_grant.write",
                            ],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/authz-diagnostics/active-policy/health",
                        headers={"Authorization": "Bearer admin-token"},
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["health"]["state"], "blocked")
        self.assertEqual(
            payload["health"]["reason_codes"],
            [
                "authz_policy_strict_human_admin_unreachable",
                "authz_policy_administrator_quorum_unsatisfied",
            ],
        )
        self.assertTrue(payload["reachable_administrators"]["caller_has_policy_administration"])
        self.assertFalse(payload["reachable_administrators"]["quorum_satisfied"])
        self.assertEqual(
            payload["reachable_administrators"]["strict_github_human_id_count"],
            0,
        )

    async def test_unauthorized_policy_health_skips_route_database_reads(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_operators": [
                        {
                            "subjects": ["support-reader"],
                            "token_labels": ["support-reader-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "list_authz_policy_records",
                        side_effect=AssertionError("authorization must precede policy reads"),
                    ):
                        response = await client.get(
                            "/v1/authz-diagnostics/active-policy/health",
                            headers={"Authorization": "Bearer support-token"},
                        )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)

    async def test_policy_health_rechecks_authorization_against_active_database_policy(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            runtime_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            runtime_record = _seed_policy(store, runtime_policy)
            denied_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["independent-admin"],
                            "token_labels": ["independent-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            denied_digest = authz_policy_sha256(denied_policy)
            denied_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=denied_digest,
                ),
                revision=2,
                status="active",
                source="test:authz-policy-health-recheck",
                updated_at="2026-08-20T12:00:00+00:00",
                policy_sha256=denied_digest,
                policy=denied_policy,
            )
            app = _app(root=root, store=store, record=runtime_record)
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "list_authz_policy_records",
                        return_value=(denied_record,),
                    ):
                        response = await client.get(
                            "/v1/authz-diagnostics/active-policy/health",
                            headers={"Authorization": "Bearer admin-token"},
                        )
                    denial_record = store.read_authz_denial_record(
                        trace_id=response.json()["trace_id"],
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNotNone(denial_record)
        assert denial_record is not None
        self.assertEqual(denial_record.policy_record_id, denied_record.record_id)
        self.assertEqual(denial_record.policy_revision, denied_record.revision)
        self.assertEqual(denial_record.policy_sha256, denied_record.policy_sha256)

    async def test_policy_health_requires_authentication_and_database_storage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            digest = authz_policy_sha256(policy)
            record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
                revision=1,
                status="active",
                source="test:authz-policy-health-database-required",
                updated_at="2026-08-20T12:00:00+00:00",
                policy_sha256=digest,
                policy=policy,
            )
            app = _app(
                root=root,
                store=FilesystemRecordStore(root / "filesystem-state"),
                record=record,
            )
            async with lifespan_client(app) as client:
                unauthenticated_response = await client.get(
                    "/v1/authz-diagnostics/active-policy/health"
                )
                database_required_response = await client.get(
                    "/v1/authz-diagnostics/active-policy/health",
                    headers={"Authorization": "Bearer admin-token"},
                )

        self.assertEqual(unauthenticated_response.status_code, 401)
        self.assertEqual(
            unauthenticated_response.json()["error"]["code"],
            "authentication_required",
        )
        self.assertEqual(database_required_response.status_code, 503)
        self.assertEqual(
            database_required_response.json()["error"]["code"],
            "database_required",
        )

    async def test_policy_health_fails_closed_for_missing_or_ambiguous_active_policy(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [AUTHZ_POLICY_HEALTH_READ_ACTION],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            second_record = record.model_copy(
                update={
                    "record_id": f"{record.record_id}-duplicate",
                    "revision": record.revision + 1,
                }
            )
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "list_authz_policy_records",
                        return_value=(),
                    ):
                        missing_response = await client.get(
                            "/v1/authz-diagnostics/active-policy/health",
                            headers={"Authorization": "Bearer admin-token"},
                        )
                    with patch.object(
                        store,
                        "list_authz_policy_records",
                        return_value=(record, second_record),
                    ):
                        ambiguous_response = await client.get(
                            "/v1/authz-diagnostics/active-policy/health",
                            headers={"Authorization": "Bearer admin-token"},
                        )
            finally:
                store.close()

        self.assertEqual(missing_response.status_code, 503, missing_response.text)
        self.assertEqual(
            missing_response.json()["error"]["code"],
            "authz_policy_unavailable",
        )
        self.assertEqual(ambiguous_response.status_code, 409, ambiguous_response.text)
        self.assertEqual(
            ambiguous_response.json()["error"]["code"],
            "active_authz_policy_ambiguous",
        )

    async def test_administrator_evaluates_one_explicit_principal_without_selector_leakage(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [EFFECTIVE_ACCESS_READ_ACTION],
                        }
                    ],
                    "local_operators": [
                        {
                            "subjects": ["evaluated-principal-secret"],
                            "token_labels": ["evaluated-token-label-secret"],
                            "products": ["example-product"],
                            "contexts": ["example-context"],
                            "actions": ["example_access.read"],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            paths = app.openapi()["paths"]
            self.assertIn("/v1/authz-diagnostics/effective-access/evaluate", paths)
            self.assertIn("/v1/authz-diagnostics/denials/{trace_id}", paths)
            request_payload = {
                "principal": {
                    "principal_type": "local_operator",
                    "subject": "evaluated-principal-secret",
                    "token_label": "evaluated-token-label-secret",
                },
                "action": "example_access.read",
                "product": "example-product",
                "context": "example-context",
                "target_scope": "context",
            }
            try:
                async with lifespan_client(app) as client:
                    allowed_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json=request_payload,
                    )
                    denied_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json={**request_payload, "action": "example_access.write"},
                    )
                    instance_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json={
                            **request_payload,
                            "target_scope": "instance",
                            "instance": "prod",
                        },
                    )
                    active_records_after_reads = store.list_authz_policy_records(
                        status="active",
                        limit=2,
                    )
            finally:
                store.close()

        self.assertEqual(allowed_response.status_code, 200, allowed_response.text)
        self.assertEqual(allowed_response.json()["evaluation"]["decision"], "allowed")
        self.assertEqual(denied_response.status_code, 200, denied_response.text)
        self.assertEqual(denied_response.json()["evaluation"]["decision"], "denied")
        self.assertEqual(
            denied_response.json()["evaluation"]["reason_code"],
            "no_matching_grant",
        )
        self.assertEqual(instance_response.status_code, 200, instance_response.text)
        self.assertEqual(instance_response.json()["request"]["target_scope"], "instance")
        self.assertEqual(instance_response.json()["evaluation"]["decision"], "denied")
        self.assertEqual(active_records_after_reads, (record,))
        rendered = json.dumps(
            {"allowed": allowed_response.json(), "denied": denied_response.json()},
            sort_keys=True,
        )
        self.assertNotIn("evaluated-principal-secret", rendered)
        self.assertNotIn("evaluated-token-label-secret", rendered)

    async def test_support_reader_explains_persisted_denial_without_policy_write_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = _support_reader_policy()
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    denied_response = await client.get(
                        "/v1/service/runtime",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    denied_trace_id = denied_response.json()["trace_id"]
                    explanation_response = await client.get(
                        f"/v1/authz-diagnostics/denials/{denied_trace_id}",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    evaluate_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer support-token"},
                        json={
                            "principal": {
                                "principal_type": "local_operator",
                                "subject": "support-reader",
                                "token_label": "support-reader-label",
                            },
                            "action": "launchplane_service.read",
                            "product": "launchplane",
                            "context": "launchplane",
                            "target_scope": "context",
                        },
                    )
                    stored_record = store.read_authz_denial_record(
                        trace_id=denied_trace_id,
                        observed_at="2026-08-18T12:01:00+00:00",
                    )
            finally:
                store.close()

        self.assertEqual(denied_response.status_code, 403, denied_response.text)
        self.assertEqual(explanation_response.status_code, 200, explanation_response.text)
        self.assertEqual(explanation_response.json()["trace_id"], denied_trace_id)
        self.assertEqual(
            explanation_response.json()["reason_code"],
            "no_matching_grant",
        )
        self.assertEqual(evaluate_response.status_code, 403, evaluate_response.text)
        self.assertIsNotNone(stored_record)
        rendered = json.dumps(explanation_response.json(), sort_keys=True)
        self.assertNotIn("support-reader", rendered)
        self.assertNotIn("support-reader-label", rendered)
        self.assertNotIn("authz_policy_grant.write", rendered)

    async def test_unknown_denial_trace_is_not_distinguishable_from_expired_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = _support_reader_policy()
            record = _seed_policy(store, policy)
            store.write_authz_denial_record(
                build_authz_denial_record(
                    trace_id="expired-trace",
                    recorded_at="2026-07-01T00:00:00+00:00",
                    expires_at="2026-07-31T00:00:00+00:00",
                    route_path="/v1/service/runtime",
                    evaluation=AuthzEvaluation(
                        decision="denied",
                        reason_code="no_matching_grant",
                        principal_type="local_operator",
                        action="launchplane_service.read",
                        product="launchplane",
                        context="launchplane",
                        target_scope="context",
                        instance_specified=False,
                    ),
                    policy_record_id=record.record_id,
                    policy_revision=record.revision,
                    policy_sha256=record.policy_sha256,
                )
            )
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    unknown_response = await client.get(
                        "/v1/authz-diagnostics/denials/unknown-trace",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    expired_response = await client.get(
                        "/v1/authz-diagnostics/denials/expired-trace",
                        headers={"Authorization": "Bearer support-token"},
                    )
            finally:
                store.close()

        self.assertEqual(unknown_response.status_code, 404, unknown_response.text)
        self.assertEqual(expired_response.status_code, 404, expired_response.text)
        self.assertEqual(unknown_response.json()["error"], expired_response.json()["error"])

    async def test_unauthorized_denial_explanation_skips_route_database_reads(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            record = _seed_policy(store, _support_reader_policy())
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    with (
                        patch.object(
                            store,
                            "list_authz_policy_records",
                            side_effect=AssertionError("authorization must precede policy reads"),
                        ),
                        patch.object(
                            store,
                            "read_authz_denial_record",
                            side_effect=AssertionError("authorization must precede denial reads"),
                        ),
                    ):
                        response = await client.get(
                            "/v1/authz-diagnostics/denials/unknown-trace",
                            headers={"Authorization": "Bearer admin-token"},
                        )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)


class AuthzDenialRecordStoreTests(unittest.TestCase):
    def test_postgres_compatible_nonpersisting_session_read_keeps_expired_row(self) -> None:
        with TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store = PostgresRecordStore(
                database_url=_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            session_manager = HumanSessionManager(
                config=GitHubOAuthConfig(
                    client_id="client-id",
                    client_secret="client-secret",
                    public_url="https://launchplane.example",
                    session_secret="session-secret",
                    cookie_secure=False,
                ),
                session_store=store,
                now=lambda: now,
            )
            expired_session = LaunchplaneHumanSession(
                session_id="expired-postgres-compatible-read",
                identity=GitHubHumanIdentity(
                    login="expired-admin",
                    github_id=12345,
                    name="Expired Admin",
                    email="expired-admin@example.test",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                ),
                created_at=now - timedelta(days=15),
                expires_at=now - timedelta(seconds=1),
            )
            store.write_session(expired_session)

            resolved_session = session_manager.read_cookie_without_renewal(
                session_manager.session_cookie_header(expired_session)
            )
            stored_session = store.read_session_without_cleanup(expired_session.session_id)
            store.close()

        self.assertIsNone(resolved_session)
        self.assertEqual(stored_session, expired_session)

    def test_candidate_policy_preview_requires_schema_v2_and_unique_probes(self) -> None:
        with self.assertRaises(ValidationError):
            AuthzPolicyCandidatePreviewRequest.model_validate(
                {"candidate_policy": {"schema_version": 1}}
            )

        probe = {
            "principal": {
                "principal_type": "local_operator",
                "subject": "operator",
                "token_label": "operator-label",
            },
            "action": "example_access.read",
            "product": "example-product",
            "context": "example-context",
            "target_scope": "context",
        }
        with self.assertRaises(ValidationError):
            AuthzPolicyCandidatePreviewRequest.model_validate(
                {
                    "candidate_policy": {"schema_version": 2},
                    "probes": [probe, probe],
                }
            )

        with self.assertRaises(ValidationError):
            AuthzPolicyCandidatePreviewRequest.model_validate(
                {
                    "candidate_policy": {"schema_version": 2},
                    "unexpected": True,
                }
            )

    def test_effective_access_target_scope_requires_one_exact_instance(self) -> None:
        base_request = {
            "principal": {
                "principal_type": "local_operator",
                "subject": "operator",
                "token_label": "operator-label",
            },
            "action": "example_access.read",
            "product": "example-product",
            "context": "example-context",
        }

        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "instance"}
            )
        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "context", "instance": "prod"}
            )
        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "instance", "instance": "prod*"}
            )

    def test_denial_records_are_immutable_by_trace_id(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            evaluation = AuthzEvaluation(
                decision="denied",
                reason_code="no_matching_grant",
                principal_type="local_operator",
                action="example_access.read",
                product="example-product",
                context="example-context",
                target_scope="context",
                instance_specified=False,
            )
            first_record = build_authz_denial_record(
                trace_id="trace-immutable",
                recorded_at="2026-08-18T12:00:00+00:00",
                expires_at="2026-09-17T12:00:00+00:00",
                route_path="/v1/deployments",
                evaluation=evaluation,
                policy_record_id="authz-policy-1",
                policy_revision=1,
                policy_sha256="a" * 64,
            )
            store.write_authz_denial_record(first_record)
            with self.assertRaisesRegex(ValueError, "different evidence"):
                store.write_authz_denial_record(
                    first_record.model_copy(update={"route_path": "/v1/promotions"})
                )
            stored_record = store.read_authz_denial_record(
                trace_id="trace-immutable",
                observed_at="2026-08-18T12:01:00+00:00",
            )
            store.close()

        self.assertEqual(stored_record, first_record)
