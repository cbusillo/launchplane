from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.authz_access_read import (
    AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT,
    AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyStatus,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
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


def _record(
    *,
    policy: LaunchplaneAuthzPolicy,
    revision: int = 1,
    status: AuthzPolicyStatus = "active",
    audit: dict[str, object] | None = None,
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        status=status,
        source="test:bounded-read",
        updated_at=f"2026-08-28T12:{revision % 60:02d}:00+00:00",
        policy_sha256=digest,
        policy=policy,
        audit=audit or {},
    )


def _local_admin_policy(
    *,
    administration_action: str = AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
    extra_action: str = "",
) -> LaunchplaneAuthzPolicy:
    actions = tuple(action for action in (administration_action, extra_action) if action)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "local_admins": [
                {
                    "managed_set_id": "operator.administration",
                    "managed_rule_id": "local-reader",
                    "subjects": ["authz-admin"],
                    "token_labels": ["authz-admin-label"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": actions,
                }
            ],
            "github_humans": [
                {
                    "managed_set_id": "operator.recovery",
                    "managed_rule_id": "independent-admin",
                    "github_ids": [9001],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                }
            ],
        }
    )


def _github_human_policy(
    *,
    role: str = "admin",
    include_administration_action: bool = True,
) -> LaunchplaneAuthzPolicy:
    caller_actions = ["authz_policy_grant.write"]
    if include_administration_action:
        caller_actions.append(AUTHZ_POLICY_ADMINISTRATION_READ_ACTION)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "managed_set_id": "operator.browser-administration",
                    "managed_rule_id": "browser-reader",
                    "github_ids": [101],
                    "roles": [role],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": caller_actions,
                },
                {
                    "managed_set_id": "operator.recovery",
                    "managed_rule_id": "independent-admin",
                    "github_ids": [202],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                },
            ],
        }
    )


def _app(
    *,
    root: Path,
    store: object,
    runtime_record: LaunchplaneAuthzPolicyRecord,
    human_session_manager: HumanSessionManager | None = None,
) -> FastAPI:
    return create_launchplane_fastapi_app(
        verifier=_RejectingVerifier(),
        authz_policy=runtime_record.policy,
        authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(
            runtime_record.policy,
            policy_sha256=runtime_record.policy_sha256,
            source="db",
            record_id=runtime_record.record_id,
            revision=runtime_record.revision,
        ),
        record_store_factory=lambda: store,
        human_session_manager=human_session_manager,
        bearer_identity_config=BearerIdentityConfig(
            local_admin_token="admin-token",
            local_admin_subject="authz-admin",
            local_admin_token_label="authz-admin-label",
        ),
        control_plane_root_path=root,
        state_dir=root / "state",
    )


@contextmanager
def _database_app(
    database_policy: LaunchplaneAuthzPolicy,
    *,
    runtime_policy: LaunchplaneAuthzPolicy | None = None,
    human_session_manager: HumanSessionManager | None = None,
) -> Iterator[tuple[PostgresRecordStore, LaunchplaneAuthzPolicyRecord, FastAPI]]:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
        store.ensure_schema()
        database_record = store.seed_authz_policy_if_absent(_record(policy=database_policy))
        runtime_record = _record(policy=runtime_policy or database_policy)
        app = _app(
            root=root,
            store=store,
            runtime_record=runtime_record,
            human_session_manager=human_session_manager,
        )
        try:
            yield store, database_record, app
        finally:
            store.close()


def _human_session_manager() -> tuple[HumanSessionManager, InMemoryHumanSessionStore]:
    session_store = InMemoryHumanSessionStore()
    manager = HumanSessionManager(
        config=GitHubOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://launchplane.example",
            session_secret="session-secret",
            cookie_secure=False,
        ),
        session_store=session_store,
    )
    return manager, session_store


def _browser_headers(
    manager: HumanSessionManager,
    identity: GitHubHumanIdentity,
) -> tuple[dict[str, str], str, str]:
    session = manager.issue(identity)
    csrf_token = manager.csrf_token(session)
    headers = build_browser_mutation_request_headers(
        origin="https://launchplane.example",
        csrf_token=csrf_token,
    )
    headers["Cookie"] = manager.session_cookie_header(session).split(";", 1)[0]
    return headers, session.session_id, csrf_token


class AuthzAdministrationReadHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_admin_reads_redacted_administration_and_history_without_writes(
        self,
    ) -> None:
        policy = _local_admin_policy(extra_action="sensitive.runtime.action")
        audit: dict[str, object] = {
            "operation": "managed_rule_set_reconcile",
            "mode": "apply",
            "reason": "private rollback reason",
            "related_issue": "private/repository#123",
            "managed_set_id": "private.managed-set",
            "operator": {"subject": "private-operator", "token_label": "private-token"},
            "diff": {
                "added_rule_count": 2,
                "removed_rule_count": 1,
                "private_count": 99,
            },
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            record = store.seed_authz_policy_if_absent(_record(policy=policy, audit=audit))
            app = _app(root=root, store=store, runtime_record=record)
            before = store.list_authz_policy_records()
            try:
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "write_authz_denial_record",
                        side_effect=AssertionError("administration reads must not write"),
                    ):
                        administration = await client.get(
                            "/v1/authz-policies/administration",
                            headers={"Authorization": "Bearer admin-token"},
                        )
                        history = await client.get(
                            "/v1/authz-policies/revisions",
                            headers={"Authorization": "Bearer admin-token"},
                        )
                after = store.list_authz_policy_records()
            finally:
                store.close()

        self.assertEqual(administration.status_code, 200, administration.text)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(administration.headers["cache-control"], "no-store")
        self.assertEqual(history.headers["cache-control"], "no-store")
        self.assertEqual(before, after)
        administration_payload = administration.json()
        history_payload = history.json()
        self.assertEqual(administration_payload["policy"]["record_id"], record.record_id)
        self.assertEqual(administration_payload["principal_rule_counts"]["local_admins"], 1)
        self.assertNotIn("managed_rules", administration_payload)
        self.assertEqual(history_payload["returned_count"], 1)
        audit_payload = history_payload["revisions"][0]["audit"]
        self.assertTrue(audit_payload["audit_present"])
        self.assertEqual(audit_payload["operation"], "managed_rule_set_reconcile")
        self.assertEqual(audit_payload["mode"], "apply")
        self.assertEqual(
            audit_payload["diff_counts"],
            {"added_rule_count": 2, "removed_rule_count": 1},
        )
        self.assertEqual(len(audit_payload["audit_sha256"]), 64)
        serialized = json.dumps(
            {"administration": administration_payload, "history": history_payload},
            sort_keys=True,
        )
        for private_value in (
            "authz-admin",
            "authz-admin-label",
            "9001",
            "independent-admin",
            "sensitive.runtime.action",
            "private rollback reason",
            "private/repository#123",
            "private.managed-set",
            "private-operator",
            "private-token",
            "private_count",
        ):
            self.assertNotIn(private_value, serialized)

    async def test_browser_admin_read_validates_same_origin_csrf_without_session_mutation(
        self,
    ) -> None:
        manager, session_store = _human_session_manager()
        identity = GitHubHumanIdentity(
            login="browser-admin",
            github_id=101,
            name="Browser Admin",
            email="browser-admin@example.test",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        headers, session_id, csrf_token = _browser_headers(manager, identity)
        policy = _github_human_policy()
        with _database_app(policy, human_session_manager=manager) as (
            _store,
            _active_record,
            app,
        ):
            session_before = session_store.read_session(session_id)
            async with lifespan_client(app) as client:
                absent_origin_headers = dict(headers)
                absent_origin_headers.pop("Origin")
                absent_origin_response = await client.get(
                    "/v1/authz-policies/administration",
                    headers=absent_origin_headers,
                )
                response = await client.get(
                    "/v1/authz-policies/administration",
                    headers=headers,
                )
                missing_csrf_headers = dict(headers)
                missing_csrf_headers.pop("X-CSRF-Token")
                missing_csrf = await client.get(
                    "/v1/authz-policies/administration",
                    headers=missing_csrf_headers,
                )
                cross_site_headers = dict(headers)
                cross_site_headers["Origin"] = "https://attacker.example"
                cross_site = await client.get(
                    "/v1/authz-policies/revisions",
                    headers=cross_site_headers,
                )
            session_after = session_store.read_session(session_id)

        self.assertEqual(absent_origin_response.status_code, 200, absent_origin_response.text)
        self.assertEqual(absent_origin_response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", absent_origin_response.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(session_after, session_before)
        self.assertIsNotNone(session_before)
        assert session_before is not None
        self.assertTrue(manager.csrf_token_is_valid(session_before, csrf_token))
        for rejected in (missing_csrf, cross_site):
            self.assertEqual(rejected.status_code, 403, rejected.text)
            self.assertEqual(rejected.headers["cache-control"], "no-store")
            self.assertEqual(rejected.json()["error"]["code"], "browser_mutation_denied")
            self.assertNotIn("set-cookie", rejected.headers)

    async def test_browser_without_origin_reaches_authorization_denial_without_writes(
        self,
    ) -> None:
        manager, session_store = _human_session_manager()
        identity = GitHubHumanIdentity(
            login="browser-admin",
            github_id=101,
            name="Browser Admin",
            email="browser-admin@example.test",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        headers, session_id, csrf_token = _browser_headers(manager, identity)
        headers.pop("Origin")
        policy = _github_human_policy(include_administration_action=False)
        with _database_app(policy, human_session_manager=manager) as (
            store,
            _active_record,
            app,
        ):
            session_before = session_store.read_session(session_id)
            async with lifespan_client(app) as client:
                with patch.object(
                    store,
                    "write_authz_denial_record",
                    create=True,
                    side_effect=AssertionError("administration denial must not write"),
                ):
                    response = await client.get(
                        "/v1/authz-policies/administration",
                        headers=headers,
                    )
            session_after = session_store.read_session(session_id)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(session_after, session_before)
        self.assertIsNotNone(session_before)
        assert session_before is not None
        self.assertTrue(manager.csrf_token_is_valid(session_before, csrf_token))

    async def test_administration_read_requires_both_runtime_and_fresh_database_grants(
        self,
    ) -> None:
        allowed_policy = _local_admin_policy()
        denied_policy = _local_admin_policy(administration_action="unrelated.read")
        cases = (
            (denied_policy, allowed_policy),
            (allowed_policy, denied_policy),
        )
        for runtime_policy, database_policy in cases:
            with self.subTest(runtime_allows=runtime_policy is allowed_policy):
                with _database_app(
                    database_policy,
                    runtime_policy=runtime_policy,
                ) as (store, _active_record, app):
                    async with lifespan_client(app) as client:
                        with patch.object(
                            store,
                            "write_authz_denial_record",
                            create=True,
                            side_effect=AssertionError("administration denial must not write"),
                        ):
                            response = await client.get(
                                "/v1/authz-policies/administration",
                                headers={"Authorization": "Bearer admin-token"},
                            )

                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_ungranted_action_denies_before_route_database_reads_or_writes(self) -> None:
        policy = _local_admin_policy(administration_action="unrelated.read")
        with _database_app(policy) as (store, _active_record, app):
            async with lifespan_client(app) as client:
                with (
                    patch.object(
                        store,
                        "list_authz_policy_records",
                        create=True,
                        side_effect=AssertionError("runtime denial must precede route DB reads"),
                    ),
                    patch.object(
                        store,
                        "write_authz_denial_record",
                        create=True,
                        side_effect=AssertionError("administration denial must not write"),
                    ),
                ):
                    response = await client.get(
                        "/v1/authz-policies/revisions",
                        headers={"Authorization": "Bearer admin-token"},
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_nonadministrator_human_is_denied_even_when_action_is_granted(self) -> None:
        manager, _session_store = _human_session_manager()
        identity = GitHubHumanIdentity(
            login="browser-reader",
            github_id=101,
            name="Browser Reader",
            email="browser-reader@example.test",
            organizations=frozenset(),
            teams=frozenset(),
            role="read_only",
        )
        headers, _session_id, _csrf_token = _browser_headers(manager, identity)
        policy = _github_human_policy(role="read_only")
        with _database_app(policy, human_session_manager=manager) as (
            store,
            _active_record,
            app,
        ):
            async with lifespan_client(app) as client:
                with patch.object(
                    store,
                    "write_authz_denial_record",
                    create=True,
                    side_effect=AssertionError("administration denial must not write"),
                ):
                    response = await client.get(
                        "/v1/authz-policies/administration",
                        headers=headers,
                    )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_administration_reads_fail_closed_for_storage_and_active_record_gaps(
        self,
    ) -> None:
        policy = _local_admin_policy()
        runtime_record = _record(policy=policy)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem_app = _app(
                root=root,
                store=FilesystemRecordStore(root / "filesystem-state"),
                runtime_record=runtime_record,
            )
            missing_store = PostgresRecordStore(
                database_url=_database_url(root / "missing.sqlite3")
            )
            missing_store.ensure_schema()
            missing_app = _app(
                root=root,
                store=missing_store,
                runtime_record=runtime_record,
            )
            ambiguous_store = PostgresRecordStore(
                database_url=_database_url(root / "ambiguous.sqlite3")
            )
            ambiguous_store.ensure_schema()
            active_record = ambiguous_store.seed_authz_policy_if_absent(runtime_record)
            ambiguous_app = _app(
                root=root,
                store=ambiguous_store,
                runtime_record=runtime_record,
            )
            try:
                async with lifespan_client(filesystem_app) as client:
                    storage_response = await client.get(
                        "/v1/authz-policies/administration",
                        headers={"Authorization": "Bearer admin-token"},
                    )
                async with lifespan_client(missing_app) as client:
                    missing_response = await client.get(
                        "/v1/authz-policies/revisions",
                        headers={"Authorization": "Bearer admin-token"},
                    )
                with patch.object(
                    ambiguous_store,
                    "list_authz_policy_records",
                    return_value=(active_record, active_record.model_copy()),
                ):
                    async with lifespan_client(ambiguous_app) as client:
                        ambiguous_response = await client.get(
                            "/v1/authz-policies/administration",
                            headers={"Authorization": "Bearer admin-token"},
                        )
            finally:
                missing_store.close()
                ambiguous_store.close()

        expectations = (
            (storage_response, 503, "database_required"),
            (missing_response, 503, "authz_policy_unavailable"),
            (ambiguous_response, 409, "active_authz_policy_ambiguous"),
        )
        for response, status_code, error_code in expectations:
            self.assertEqual(response.status_code, status_code, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["error"]["code"], error_code)

    async def test_revision_history_is_newest_first_and_bounded_to_fifty(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            current_policy = _local_admin_policy(extra_action="revision.1")
            current_record = store.seed_authz_policy_if_absent(_record(policy=current_policy))
            for revision in range(2, AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT + 2):
                replacement_policy = _local_admin_policy(extra_action=f"revision.{revision}")
                replacement_record = _record(
                    policy=replacement_policy,
                    revision=revision,
                    audit={
                        "operation": "unexpected_operation",
                        "mode": "unexpected_mode",
                        "reason": f"private reason {revision}",
                        "diff": {
                            "updated_rule_count": revision,
                            "invalid_negative_count": -1,
                        },
                    },
                )
                result = store.compare_and_write_authz_policy_record(
                    expected_record=current_record,
                    replacement_record=replacement_record,
                )
                self.assertEqual(result.status, "written")
                current_record = replacement_record
            app = _app(root=root, store=store, runtime_record=current_record)
            try:
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/authz-policies/revisions",
                        headers={"Authorization": "Bearer admin-token"},
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["returned_count"], AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT)
        self.assertTrue(payload["truncated"])
        revisions = [entry["policy"]["revision"] for entry in payload["revisions"]]
        self.assertEqual(
            revisions,
            list(range(AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT + 1, 1, -1)),
        )
        newest_audit = payload["revisions"][0]["audit"]
        self.assertEqual(newest_audit["operation"], "unknown")
        self.assertEqual(newest_audit["mode"], "unknown")
        self.assertEqual(
            newest_audit["diff_counts"],
            {"updated_rule_count": AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT + 1},
        )
        self.assertNotIn("private reason", response.text)

    async def test_administration_routes_publish_bounded_openapi_contracts(self) -> None:
        with _database_app(_local_admin_policy()) as (_store, _active_record, app):
            paths = app.openapi()["paths"]

        administration = paths["/v1/authz-policies/administration"]["get"]
        revisions = paths["/v1/authz-policies/revisions"]["get"]
        self.assertEqual(
            administration["operationId"],
            "read_authz_policy_administration",
        )
        self.assertEqual(
            revisions["operationId"],
            "read_authz_policy_revision_history",
        )
        self.assertEqual(
            administration["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AuthzPolicyAdministrationResponse",
        )
        self.assertEqual(
            revisions["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AuthzPolicyRevisionHistoryResponse",
        )

    async def test_sensitive_route_errors_are_no_store(self) -> None:
        with _database_app(_local_admin_policy()) as (_store, _active_record, app):
            async with lifespan_client(app) as client:
                unauthenticated = await client.get("/v1/authz-policies/administration")
                method_not_allowed = await client.post(
                    "/v1/authz-policies/revisions",
                    headers={"Authorization": "Bearer admin-token"},
                )

        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)
        self.assertEqual(method_not_allowed.status_code, 405, method_not_allowed.text)
        self.assertEqual(unauthenticated.headers["cache-control"], "no-store")
        self.assertEqual(method_not_allowed.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
