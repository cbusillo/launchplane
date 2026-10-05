from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane import secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.github_delivery_configuration import (
    DeliveryGitHubAppConfigurationRequest,
    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
    apply_delivery_github_configuration,
    plan_delivery_github_configuration,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    RuntimeEnvironmentConflictError,
    SecretCopySourceConflictError,
)
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.stores import sqlite_database_url


def _seed(store: PostgresRecordStore) -> None:
    secrets.write_secret_value(
        record_store=store,
        scope="context",
        integration="existing-delivery-key",
        name="delivery-key",
        plaintext_value="fixture-key-material",
        binding_key="private_key",
        context_name="launchplane",
        actor="test",
    )
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="context",
            context="launchplane",
            env={"LAUNCHPLANE_ADVISORY_GITHUB_APP_ID": "77"},
            updated_at="2026-10-05T00:00:00Z",
            source_label="test",
        )
    )


class DeliveryConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ,
            {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "state.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        _seed(self.store)
        self.request = DeliveryGitHubAppConfigurationRequest(
            app_id=76, integration="existing-delivery-key", reason="Use the existing Delivery App."
        )

    def call(
        self, request: DeliveryGitHubAppConfigurationRequest, key: str = ""
    ) -> dict[str, object]:
        with patch(
            "control_plane.secrets._decrypt_secret_value",
            side_effect=AssertionError("Configuration must not decrypt a key."),
        ):
            return apply_delivery_github_configuration(
                store=self.store,
                request=request,
                actor="github:42",
                trace_id="test-trace",
                idempotency_key=key,
            )

    def apply_request(self, digest: object) -> DeliveryGitHubAppConfigurationRequest:
        return self.request.model_copy(update={"mode": "apply", "expected_plan_digest": digest})

    def test_review_apply_and_replay_preserve_other_settings_and_the_key(self) -> None:
        before = self.store.list_runtime_environment_records()
        keys_before = self.store.list_secret_records()
        versions_before = tuple(
            self.store.read_secret_version(record.current_version_id) for record in keys_before
        )
        preview = self.call(self.request)
        self.assertEqual(self.store.list_runtime_environment_records(), before)
        apply = self.apply_request(preview["plan_digest"])
        result = self.call(apply, "apply-once")
        written = self.store.list_runtime_environment_records()
        self.assertEqual(written[0].env["LAUNCHPLANE_ADVISORY_GITHUB_APP_ID"], "77")
        self.assertEqual(written[0].env["LAUNCHPLANE_DELIVERY_GITHUB_APP_ID"], "76")
        self.assertEqual(self.call(apply, "apply-once"), result)
        self.assertEqual(self.store.list_runtime_environment_records(), written)
        self.assertEqual(self.store.list_secret_records(), keys_before)
        self.assertEqual(
            tuple(
                self.store.read_secret_version(record.current_version_id) for record in keys_before
            ),
            versions_before,
        )
        self.assertNotIn("fixture-key-material", str(result))

    def test_stale_review_and_idempotency_conflict_write_nothing(self) -> None:
        plan = self.call(self.request)
        old = self.store.list_runtime_environment_records()[0]
        self.store.write_runtime_environment_record(
            old.model_copy(update={"env": {**old.env, "OTHER_SETTING": "changed"}})
        )
        before = self.store.list_runtime_environment_records()
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.call(self.apply_request(plan["plan_digest"]), "stale")
        self.assertEqual(self.store.list_runtime_environment_records(), before)
        fresh = self.call(self.request)
        self.call(self.apply_request(fresh["plan_digest"]), "shared-key")
        before = self.store.list_runtime_environment_records()
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.call(
                self.apply_request(fresh["plan_digest"]).model_copy(update={"app_id": 78}),
                "shared-key",
            )
        self.assertEqual(self.store.list_runtime_environment_records(), before)

    def test_runtime_or_key_binding_drift_at_commit_is_atomic(self) -> None:
        _, bundle = plan_delivery_github_configuration(
            store=self.store, request=self.request, actor="github:42"
        )
        old = self.store.list_runtime_environment_records()[0]
        self.store.write_runtime_environment_record(
            old.model_copy(update={"env": {**old.env, "OTHER_SETTING": "changed"}})
        )
        with self.assertRaises(RuntimeEnvironmentConflictError):
            self.store.write_product_authority_bundle(bundle)
        self.assertNotIn(
            "LAUNCHPLANE_DELIVERY_GITHUB_APP_ID",
            self.store.list_runtime_environment_records()[0].env,
        )
        _, bundle = plan_delivery_github_configuration(
            store=self.store, request=self.request, actor="github:42"
        )
        binding = self.store.list_secret_bindings()[0]
        self.store.write_secret_binding(binding.model_copy(update={"status": "disabled"}))
        with self.assertRaises(SecretCopySourceConflictError):
            self.store.write_product_authority_bundle(bundle)
        self.assertNotIn(
            "LAUNCHPLANE_DELIVERY_GITHUB_APP_ID",
            self.store.list_runtime_environment_records()[0].env,
        )

    def test_missing_binding_or_review_cannot_apply(self) -> None:
        with self.assertRaises(ValueError):
            self.call(self.request.model_copy(update={"integration": "missing"}))
        with self.assertRaises(ValueError):
            self.call(self.apply_request("0" * 64))
        with self.assertRaises(ValidationError):
            DeliveryGitHubAppConfigurationRequest(
                app_id=76, integration="existing", reason="configure", mode="apply"
            )
        with self.assertRaises(ValidationError):
            DeliveryGitHubAppConfigurationRequest.model_validate(
                {
                    "app_id": 76,
                    "integration": "existing",
                    "reason": "configure",
                    "private_key": "key",
                }
            )


class DeliveryConfigurationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_in_admin_scope_and_csrf_are_required_without_a_product_profile(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ),
        ):
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "state.sqlite3")
            )
            store.ensure_schema()
            try:
                _seed(store)
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
                payload = {
                    "app_id": 76,
                    "integration": "existing-delivery-key",
                    "reason": "Use the existing key.",
                }
                rejected = await _asgi_request(
                    app,
                    "POST",
                    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
                    headers={"Cookie": manager.session_cookie_header(session)},
                    payload=payload,
                )
                self.assertIn(rejected.status_code, (400, 403))
                preview = await _asgi_request(
                    app,
                    "POST",
                    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
                    headers=_browser_mutation_headers(manager, session),
                    payload=payload,
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                applied = await _asgi_request(
                    app,
                    "POST",
                    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
                    headers={
                        **_browser_mutation_headers(manager, session),
                        "Idempotency-Key": "delivery-config",
                    },
                    payload={
                        **payload,
                        "mode": "apply",
                        "expected_plan_digest": preview.json()["plan_digest"],
                    },
                )
                self.assertEqual(applied.status_code, 200, applied.text)
                other = manager.issue(identity=replace(identity, github_id=43))
                denied = await _asgi_request(
                    app,
                    "POST",
                    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
                    headers=_browser_mutation_headers(manager, other),
                    payload=payload,
                )
                self.assertEqual(denied.status_code, 403, denied.text)
            finally:
                store.close()
