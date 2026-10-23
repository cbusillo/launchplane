import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from httpx2 import Response

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_config import (
    ProductConfigError,
    ProductConfigMode,
    plan_product_config_authority_bundle,
)
from control_plane.provider_key_adoption import LaneProviderEnv
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    RuntimeEnvironmentConflictError,
    SecretDeliveryConflictError,
)
from tests.http_app_test_support import _asgi_request, _product_config_policy, _RejectingVerifier
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url


class ProductConfigSecretDisableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        )
        self.record = SecretRecord(
            secret_id="testing-mail-secret",
            scope="context_instance",
            integration="runtime_environment",
            name="outbound-mail",
            context="example-site",
            instance="testing",
            current_version_id="mail-version",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.store.write_secret_record(self.record)
        self.version = SecretVersion(
            version_id=self.record.current_version_id,
            secret_id=self.record.secret_id,
            created_at=self.record.created_at,
            ciphertext="opaque-test-ciphertext",
        )
        self.store.write_secret_version(self.version)
        self.binding = SecretBinding(
            binding_id="testing-mail",
            secret_id=self.record.secret_id,
            integration="runtime_environment",
            binding_key="RESEND_API_KEY",
            context="example-site",
            instance="testing",
            created_at=self.record.created_at,
            updated_at=self.record.updated_at,
        )
        self.store.write_secret_binding(self.binding)
        self.production_record = self.record.model_copy(
            update={
                "secret_id": "production-mail-secret",
                "instance": "prod",
                "current_version_id": "production-mail-version",
            }
        )
        self.store.write_secret_record(self.production_record)
        self.store.write_secret_version(
            self.version.model_copy(
                update={
                    "version_id": self.production_record.current_version_id,
                    "secret_id": self.production_record.secret_id,
                }
            )
        )
        self.production = self.binding.model_copy(
            update={
                "binding_id": "production-mail",
                "instance": "prod",
                "secret_id": self.production_record.secret_id,
            }
        )
        self.store.write_secret_binding(self.production)
        self.runtime = RuntimeEnvironmentRecord(
            schema_version=2,
            scope="instance",
            context="example-site",
            instance="testing",
            env={"CONTACT_EMAIL_MODE": "log"},
            retired_provider_keys=(self.binding.binding_key,),
            updated_at=self.record.updated_at,
            source_label="test",
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.payload: dict[str, object] = {
            "product": "example-site",
            "context": "example-site",
            "instance": "testing",
            "mode": "dry-run",
            "reason": "Remove retired testing mail binding",
            "disable_secret_bindings": [
                {"binding_key": self.binding.binding_key, "version_id": self.version.version_id}
            ],
        }
        self.provider = LaneProviderEnv(env={}, template_defaults={})

    def plan(
        self, *, mode: ProductConfigMode = "apply"
    ) -> tuple[dict[str, object], ProductAuthorityBundle]:
        return plan_product_config_authority_bundle(
            record_store=self.store,
            payload=self.payload,
            mode=mode,
            actor="test",
            source_label="test",
            lane_provider_env_reader=lambda: self.provider,
        )

    async def test_review_apply_and_replay_disable_only_testing_without_decryption(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=(
                    "product_config.plan",
                    "product_config.apply",
                    "product_profile.read",
                    "secret.list",
                )
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

        async def post(mode: str, key: str = "") -> Response:
            return await _asgi_request(
                app,
                "POST",
                "/v1/product-config/apply",
                headers={
                    "Authorization": "Bearer test-operator-token",
                    **({"Idempotency-Key": key} if key else {}),
                },
                payload={**self.payload, "mode": mode},
            )

        with (
            patch(
                "control_plane.product_config_service.read_lane_provider_env",
                return_value=self.provider,
            ),
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=AssertionError("must not decrypt"),
            ),
        ):
            unreviewed = await post("apply", "disable-unreviewed")
            self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
            dry_run = await post("dry-run")
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(
                self.store.list_secret_bindings(instance_name="testing"), (self.binding,)
            )
            applied = await post("apply", "disable-once")
            self.assertEqual(applied.status_code, 202, applied.text)
            self.assertEqual(applied.json()["result"]["secrets"][0]["action"], "disabled")
            events = self.store.list_secret_audit_events(secret_id=self.record.secret_id)
            self.assertEqual(events[0].metadata["version_id"], self.version.version_id)
            replayed = await post("apply", "disable-once")
            self.assertEqual(replayed.status_code, 202, replayed.text)
            self.assertEqual(
                self.store.list_secret_audit_events(secret_id=self.record.secret_id), events
            )
        self.assertEqual(
            self.store.list_secret_bindings(instance_name="testing")[0].status, "disabled"
        )
        self.assertEqual(self.store.list_secret_bindings(instance_name="prod"), (self.production,))
        self.assertEqual(self.store.read_secret_record(self.record.secret_id), self.record)
        self.assertEqual(self.store.read_secret_version(self.version.version_id), self.version)
        self.assertEqual(
            self.store.read_secret_record(self.production_record.secret_id), self.production_record
        )
        self.assertNotIn(self.version.ciphertext, json.dumps(applied.json()))

    def test_disable_requires_unique_lane_binding(self) -> None:
        self.store.write_secret_binding(
            self.binding.model_copy(update={"binding_id": "second-testing-mail"})
        )
        with self.assertRaises(ProductConfigError):
            self.plan()

    def test_disable_refuses_exposing_context_secret(self) -> None:
        inherited = self.record.model_copy(
            update={
                "secret_id": "context-mail-secret",
                "scope": "context",
                "instance": "",
                "current_version_id": "context-mail-version",
            }
        )
        self.store.write_secret_record(inherited)
        self.store.write_secret_version(
            self.version.model_copy(
                update={
                    "secret_id": inherited.secret_id,
                    "version_id": inherited.current_version_id,
                }
            )
        )
        self.store.write_secret_binding(
            self.binding.model_copy(
                update={
                    "binding_id": "context-mail",
                    "secret_id": inherited.secret_id,
                    "instance": "",
                }
            )
        )
        with self.assertRaises(ProductConfigError):
            self.plan()

    async def test_workflow_identity_requires_and_can_record_matching_review(self) -> None:
        policy = _product_config_policy(
            action="product_config.apply", product="example-site", context="example-site"
        )
        policy = policy.model_copy(
            update={
                "github_actions": tuple(
                    rule.model_copy(
                        update={
                            "actions": (
                                "product_config.plan",
                                "product_config.apply",
                                "product_profile.read",
                            )
                        }
                    )
                    for rule in policy.github_actions
                )
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
        )
        with patch(
            "control_plane.product_config_service.read_lane_provider_env",
            return_value=self.provider,
        ):
            unreviewed = await _asgi_request(
                app,
                "POST",
                "/v1/product-config/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "workflow-unreviewed",
                },
                payload={**self.payload, "mode": "apply"},
            )
            self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
            dry_run = await _asgi_request(
                app,
                "POST",
                "/v1/product-config/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=self.payload,
            )
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            applied = await _asgi_request(
                app,
                "POST",
                "/v1/product-config/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "workflow-reviewed",
                },
                payload={**self.payload, "mode": "apply"},
            )
            self.assertEqual(applied.status_code, 202, applied.text)

    def test_requires_existing_retirement_absence_and_reviewed_version(self) -> None:
        for change in ("retirement", "provider", "version", "production"):
            with self.subTest(change=change):
                self.store.write_runtime_environment_record(
                    self.runtime.model_copy(update={"retired_provider_keys": ()})
                    if change == "retirement"
                    else self.runtime
                )
                self.provider = LaneProviderEnv(
                    env={self.binding.binding_key: "must-not-leak"} if change == "provider" else {},
                    template_defaults={},
                )
                self.store.write_secret_record(
                    self.record.model_copy(update={"current_version_id": "new-version"})
                    if change == "version"
                    else self.record
                )
                self.payload["instance"] = "prod" if change == "production" else "testing"
                with self.assertRaises(ProductConfigError):
                    self.plan()
        self.assertEqual(self.store.list_secret_bindings(instance_name="testing"), (self.binding,))

    def test_commit_refuses_binding_replacement(self) -> None:
        _, bundle = self.plan()
        self.store.write_secret_binding(
            self.binding.model_copy(update={"updated_at": "2026-01-02T00:00:00Z"})
        )
        with self.assertRaises(SecretDeliveryConflictError):
            self.store.write_product_authority_bundle(bundle)

    def test_commit_refuses_retirement_removed_after_plan(self) -> None:
        _, bundle = self.plan()
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(update={"retired_provider_keys": ()})
        )
        with self.assertRaises(RuntimeEnvironmentConflictError):
            self.store.write_product_authority_bundle(bundle)

    def test_commit_refuses_version_rotation_after_plan(self) -> None:
        _, bundle = self.plan()
        self.store.write_secret_record(
            self.record.model_copy(update={"current_version_id": "rotated-version"})
        )
        with self.assertRaises(SecretDeliveryConflictError):
            self.store.write_product_authority_bundle(bundle)
