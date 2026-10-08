from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretRotationWrite,
    SecretVersion,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.service_github_delivery_controls import (
    SERVICE_GITHUB_DELIVERY_ROUTE,
    SERVICE_TOKEN_RETIREMENT_ROUTE,
    ServiceTokenRetirementRequest,
    ServiceTokenRetirementResponse,
    apply_service_token_retirement,
    plan_service_token_retirement,
    read_service_github_delivery,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    RuntimeEnvironmentConflictError,
    SecretRecordConflictError,
    SecretCopySourceConflictError,
)
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.stores import sqlite_database_url


def seed_metadata(store: PostgresRecordStore | FilesystemRecordStore) -> None:
    # No encryption, decryption, token mint, or secret value is necessary for these controls.
    for secret_id, integration, key, scope, context in (
        ("key", "existing-delivery", "private_key", "context", "launchplane"),
        ("token-global", "launchplane_service", "GITHUB_TOKEN", "global", ""),
        ("token-context", "launchplane_service", "GITHUB_TOKEN", "context", "launchplane"),
        ("unrelated", "launchplane_service", "WEBHOOK_SECRET", "context", "launchplane"),
        ("product-token", "runtime_environment", "GITHUB_TOKEN", "context", "sample-site"),
    ):
        store.write_secret_record(
            SecretRecord.model_validate(
                {
                    "secret_id": secret_id,
                    "integration": integration,
                    "name": key,
                    "scope": scope,
                    "context": context,
                    "current_version_id": f"version-{secret_id}",
                    "created_at": "2026-10-05T00:00:00Z",
                    "updated_at": "2026-10-05T00:00:00Z",
                }
            )
        )
        store.write_secret_binding(
            SecretBinding(
                binding_id=f"binding-{secret_id}",
                secret_id=secret_id,
                integration=integration,
                binding_key=key,
                context=context,
                created_at="2026-10-05T00:00:00Z",
                updated_at="2026-10-05T00:00:00Z",
            )
        )
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="context",
            context="launchplane",
            updated_at="2026-10-05T00:00:00Z",
            env={
                "LAUNCHPLANE_DELIVERY_GITHUB_APP_ID": "76",
                "LAUNCHPLANE_DELIVERY_GITHUB_APP_INTEGRATION": "existing-delivery",
                "LAUNCHPLANE_ADVISORY_GITHUB_APP_ID": "77",
            },
        )
    )


def retirement_request() -> ServiceTokenRetirementRequest:
    return ServiceTokenRetirementRequest(
        secret_ids=("token-global", "token-context"),
        reason="Retire obsolete service PAT records.",
        advisory_check_url="https://github.com/example/site/actions/runs/11/job/12",
        delivery_comment_url="https://github.com/example/site/pull/13#issuecomment-14",
        delivery_release_issue_url="https://github.com/example/site/issues/15",
        consumer_check_evidence="Director checked remaining service consumers after the App migration.",
    )


class ServiceTokenRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "state.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        seed_metadata(self.store)
        self.request = retirement_request()
        for method in ("read_secret_version", "write_secret_version"):
            guard = patch.object(
                self.store,
                method,
                side_effect=AssertionError("No values or encrypted versions may be accessed."),
            )
            guard.start()
            self.addCleanup(guard.stop)

    def call(
        self, request: ServiceTokenRetirementRequest, key: str = "", actor: str = "github:42"
    ) -> ServiceTokenRetirementResponse:
        return apply_service_token_retirement(
            store=self.store,
            request=request,
            actor=actor,
            trace_id="test-retire",
            idempotency_key=key,
        )

    def apply_request(self, digest: str) -> ServiceTokenRetirementRequest:
        return ServiceTokenRetirementRequest.model_validate(
            {
                **self.request.model_dump(),
                "mode": "apply",
                "director_confirmed": True,
                "expected_plan_digest": digest,
            }
        )

    def test_metadata_review_disable_and_replay_preserve_unrelated_records(self) -> None:
        before = self.store.list_secret_records()
        runtime_before = self.store.list_runtime_environment_records()
        status = read_service_github_delivery(store=self.store, trace_id="read")
        self.assertEqual([key.integration for key in status.existing_keys], ["existing-delivery"])
        self.assertEqual(
            {token.secret_id for token in status.obsolete_tokens}, set(self.request.secret_ids)
        )
        preview = self.call(self.request)
        self.assertEqual(self.store.list_secret_records(), before)
        applied = self.call(self.apply_request(preview.plan_digest), "retire-once")
        self.assertEqual(self.call(self.apply_request(preview.plan_digest), "retire-once"), applied)
        for record in before:
            current = self.store.read_secret_record(record.secret_id)
            if record.secret_id in self.request.secret_ids:
                self.assertEqual(current.status, "disabled")
                self.assertEqual(current.current_version_id, record.current_version_id)
                audit = self.store.list_secret_audit_events(secret_id=record.secret_id)
                self.assertEqual(len(audit), 1)
                self.assertEqual(audit[0].actor, "github:42")
                self.assertEqual(
                    audit[0].metadata["delivery_comment_url"], self.request.delivery_comment_url
                )
            else:
                self.assertEqual(current, record)
        for binding in self.store.list_secret_bindings():
            self.assertEqual(
                binding.status,
                "disabled" if binding.secret_id in self.request.secret_ids else "configured",
            )
        self.assertEqual(self.store.list_runtime_environment_records(), runtime_before)

    def test_non_service_record_and_mixed_consumer_refuse(self) -> None:
        before = self.store.list_secret_records()
        for secret_id in ("key", "unrelated", "product-token", "missing"):
            with (
                self.subTest(secret_id=secret_id),
                self.assertRaises((ValueError, FileNotFoundError)),
            ):
                self.call(self.request.model_copy(update={"secret_ids": (secret_id,)}))
        binding = next(
            item for item in self.store.list_secret_bindings() if item.secret_id == "token-global"
        )
        self.store.write_secret_binding(
            binding.model_copy(update={"binding_id": "other-consumer", "binding_key": "OTHER_KEY"})
        )
        with self.assertRaisesRegex(ValueError, "no other consumers"):
            self.call(self.request)
        self.assertEqual(self.store.list_secret_records(), before)

    def test_stale_record_receipt_reason_or_actor_cannot_apply(self) -> None:
        preview = self.call(self.request)
        for updates, actor in (
            ({"reason": "changed"}, "github:42"),
            (
                {"delivery_release_issue_url": "https://github.com/example/site/issues/19"},
                "github:42",
            ),
            ({}, "github:43"),
        ):
            with (
                self.subTest(updates=updates, actor=actor),
                self.assertRaisesRegex(ValueError, "changed since"),
            ):
                self.call(
                    self.apply_request(preview.plan_digest).model_copy(update=updates),
                    "stale",
                    actor,
                )
        record = self.store.read_secret_record("token-context")
        self.store.write_secret_record(
            record.model_copy(update={"updated_at": "2026-10-05T01:00:00Z"})
        )
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.call(self.apply_request(preview.plan_digest), "stale-record")
        self.assertEqual(self.store.read_secret_record("token-global").status, "configured")

    def test_commit_drift_and_write_failure_roll_back_entire_disable(self) -> None:
        _, bundle = plan_service_token_retirement(
            store=self.store, request=self.request, actor="github:42", trace_id="plan"
        )
        binding = next(
            item for item in self.store.list_secret_bindings() if item.secret_id == "token-global"
        )
        self.store.write_secret_binding(binding.model_copy(update={"binding_id": "new-consumer"}))
        with self.assertRaises(SecretCopySourceConflictError):
            self.store.write_product_authority_bundle(bundle)
        self.assertEqual(self.store.read_secret_record("token-global").status, "configured")
        preview = self.call(self.request)

        def fail(step: str) -> None:
            if step == "write_secret_binding":
                raise RuntimeError("write failed")

        with (
            patch.object(self.store, "_after_product_authority_bundle_step", side_effect=fail),
            self.assertRaises(RuntimeError),
        ):
            self.call(self.apply_request(preview.plan_digest), "failed-write")
        self.assertEqual(self.store.read_secret_record("token-context").status, "configured")
        self.assertEqual(self.store.list_secret_audit_events(secret_id="token-global"), ())
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope="service-token-retirement:github:42",
                route_path=SERVICE_TOKEN_RETIREMENT_ROUTE,
                idempotency_key="failed-write",
            )
        )

    def test_new_global_selector_at_commit_is_guarded(self) -> None:
        _, bundle = plan_service_token_retirement(
            store=self.store, request=self.request, actor="github:42", trace_id="plan"
        )
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="global",
                env={"LAUNCHPLANE_DELIVERY_GITHUB_APP_ID": "79"},
                updated_at="2026-10-05T01:00:00Z",
            )
        )
        with self.assertRaises(RuntimeEnvironmentConflictError):
            self.store.write_product_authority_bundle(bundle)
        self.assertEqual(self.store.read_secret_record("token-global").status, "configured")

    def test_idempotency_conflicts_and_missing_selectors_refuse(self) -> None:
        preview = self.call(self.request)
        apply = self.apply_request(preview.plan_digest)
        with self.assertRaisesRegex(ValueError, "Idempotency-Key"):
            self.call(apply)
        self.call(apply, "same-key")
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.call(apply.model_copy(update={"reason": "different"}), "same-key")
        self.assertEqual(self.store.read_secret_record("key").status, "configured")

    def test_request_requires_receipts_consumer_check_and_director_confirmation(self) -> None:
        for updates in (
            {"consumer_check_evidence": " "},
            {"secret_ids": ()},
            {"secret_ids": ("token-global", "token-global")},
            {"mode": "apply", "expected_plan_digest": "a" * 64},
            {"value": "unacceptable"},
            {"delivery_comment_url": "https://example.invalid/receipt"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                ServiceTokenRetirementRequest.model_validate(
                    {**self.request.model_dump(), **updates}
                )

    def test_filesystem_bundle_guards_match_shared_store(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            seed_metadata(store)
            # The planner is metadata-only, and its bundle can rehearse in a file store.
            with patch.object(self.store, "write_product_authority_bundle"):
                _, bundle = plan_service_token_retirement(
                    store=self.store, request=self.request, actor="github:42", trace_id="plan"
                )
            binding = next(
                item for item in store.list_secret_bindings() if item.secret_id == "token-global"
            )
            store.write_secret_binding(binding.model_copy(update={"binding_id": "other-consumer"}))
            with self.assertRaises(SecretCopySourceConflictError):
                store.write_product_authority_bundle(bundle)
            self.assertEqual(store.read_secret_record("token-global").status, "configured")


class RetiredSecretRotationTests(unittest.TestCase):
    def test_prepared_rotation_cannot_reenable_a_retired_record(self) -> None:
        for backend in ("database", "filesystem"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                store: PostgresRecordStore | FilesystemRecordStore
                if backend == "database":
                    store = PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "state.sqlite3")
                    )
                    store.ensure_schema()
                    self.addCleanup(store.close)
                else:
                    store = FilesystemRecordStore(Path(directory))
                seed_metadata(store)
                record = store.read_secret_record("token-global")
                rotation = SecretRotationWrite(
                    expected_current_version_id=record.current_version_id,
                    record=record.model_copy(update={"current_version_id": "new-fixture-version"}),
                    version=SecretVersion(
                        version_id="new-fixture-version",
                        secret_id=record.secret_id,
                        created_at=record.updated_at,
                        ciphertext="opaque-fixture-ciphertext",
                    ),
                    audit_event=SecretAuditEvent(
                        event_id="rotation-event",
                        secret_id=record.secret_id,
                        event_type="rotated",
                        recorded_at=record.updated_at,
                    ),
                )
                disabled = record.model_copy(update={"status": "disabled"})
                store.write_secret_record(disabled)
                with self.assertRaisesRegex(ValueError, "changed after rotation preflight"):
                    store.write_secret_rotations((rotation,))
                self.assertEqual(store.read_secret_record(record.secret_id), disabled)
                self.assertEqual(store.list_secret_audit_events(secret_id=record.secret_id), ())

    def test_filesystem_rotation_guards_status_change_after_its_precheck(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            seed_metadata(store)
            record = store.read_secret_record("token-global")
            rotation = SecretRotationWrite(
                expected_current_version_id=record.current_version_id,
                record=record.model_copy(update={"current_version_id": "new-fixture-version"}),
                version=SecretVersion(
                    version_id="new-fixture-version",
                    secret_id=record.secret_id,
                    created_at=record.updated_at,
                    ciphertext="opaque-fixture-ciphertext",
                ),
                audit_event=SecretAuditEvent(
                    event_id="rotation-event",
                    secret_id=record.secret_id,
                    event_type="rotated",
                    recorded_at=record.updated_at,
                ),
            )
            original = store.write_product_authority_bundle

            def retire_then_commit(bundle: ProductAuthorityBundle) -> None:
                store.write_secret_record(record.model_copy(update={"status": "disabled"}))
                original(bundle)

            with (
                patch.object(
                    store, "write_product_authority_bundle", side_effect=retire_then_commit
                ),
                self.assertRaises(SecretRecordConflictError),
            ):
                store.write_secret_rotations((rotation,))
            self.assertEqual(store.read_secret_record(record.secret_id).status, "disabled")


class ServiceDeliveryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_scopes_csrf_confirmation_and_apply_readback(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "state.sqlite3")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            seed_metadata(store)
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            policy = LaunchplaneAuthzPolicy(
                github_humans=(
                    GitHubHumanPolicyRule(
                        github_ids=(42,),
                        roles=("admin",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        actions=("product_config.plan", "product_config.apply"),
                    ),
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                human_session_manager=manager,
                record_store_factory=lambda: store,
            )
            identity = GitHubHumanIdentity(
                login="admin-fixture",
                github_id=42,
                name="Fixture",
                email="fixture@example.invalid",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
            session = manager.issue(identity=identity)
            headers = {"Cookie": manager.session_cookie_header(session)}
            status = await _asgi_request(app, "GET", SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers)
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["app_id"], "76")
            for unauthorized in (replace(identity, github_id=43), replace(identity, role="owner")):
                other = manager.issue(identity=unauthorized)
                response = await _asgi_request(
                    app,
                    "GET",
                    SERVICE_GITHUB_DELIVERY_ROUTE,
                    headers={"Cookie": manager.session_cookie_header(other)},
                )
                self.assertIn(response.status_code, (401, 403))
            payload = retirement_request().model_dump(mode="json")
            no_csrf = await _asgi_request(
                app, "POST", SERVICE_TOKEN_RETIREMENT_ROUTE, headers=headers, payload=payload
            )
            self.assertIn(no_csrf.status_code, (400, 403))
            preview = await _asgi_request(
                app,
                "POST",
                SERVICE_TOKEN_RETIREMENT_ROUTE,
                headers=_browser_mutation_headers(manager, session),
                payload=payload,
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            apply = {
                **payload,
                "mode": "apply",
                "expected_plan_digest": preview.json()["plan_digest"],
            }
            unconfirmed = await _asgi_request(
                app,
                "POST",
                SERVICE_TOKEN_RETIREMENT_ROUTE,
                headers=_browser_mutation_headers(manager, session),
                payload=apply,
            )
            self.assertEqual(unconfirmed.status_code, 400)
            applied = await _asgi_request(
                app,
                "POST",
                SERVICE_TOKEN_RETIREMENT_ROUTE,
                headers={
                    **_browser_mutation_headers(manager, session),
                    "Idempotency-Key": "retire",
                },
                payload={**apply, "director_confirmed": True},
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            readback = await _asgi_request(
                app, "GET", SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers
            )
            self.assertTrue(
                all(item["status"] == "disabled" for item in readback.json()["obsolete_tokens"])
            )
