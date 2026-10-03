import os
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Any

from fastapi import FastAPI
from httpx2 import Response

from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_config import (
    apply_product_config_bundle,
    plan_product_config_authority_bundle,
)
from control_plane.product_config_http import ProductConfigApplyEnvelope
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductContextOwnershipError,
    ProductProfileConflictError,
    SecretCopySourceConflictError,
)
from tests.http_app_test_support import _asgi_request, _RejectingVerifier
from tests.support.auth import _local_operator_policy
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url, _write_runtime_key_safety_policy


class ProductSecretCopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database_url = _sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        _write_runtime_key_safety_policy(database_url=database_url)
        self.store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(self.store.close)
        self.environment = patch.dict(
            os.environ,
            {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.source_payload: dict[str, Any] = {
            "product": "example-site",
            "context": "example-site",
            "instance": "prod",
            "secrets": [{"binding_key": "REPAIRSHOPR_TOKEN", "value": "source-only-test-token"}],
        }
        apply_product_config_bundle(
            record_store=self.store,
            payload=self.source_payload,
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        self.source = self.store.list_secret_records(integration="runtime_environment")[0]
        self.payload: dict[str, Any] = {
            "product": "example-site",
            "context": "example-site",
            "instance": "testing",
            "mode": "dry-run",
            "reason": "Copy verified read-only source",
            "secrets": [
                {
                    "binding_key": "REPAIRSHOPR_TOKEN",
                    "copy_from": {
                        "context": "example-site",
                        "instance": "prod",
                        "version_id": self.source.current_version_id,
                    },
                    "secret_class": "shared_safe",
                    "sharing_reason": {
                        "kind": "read_only_source",
                        "reason": "Read-only API access",
                        "evidence": "Client verified view permissions on 2026-10-02",
                    },
                }
            ],
        }
        self.app = self.make_app()

    def make_app(self, *, source_read: bool = True) -> FastAPI:
        actions: tuple[str, ...] = (
            "product_config.plan",
            "product_config.apply",
            "product_profile.read",
            "secret.list",
        )
        if source_read:
            actions += ("secret.read",)
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(actions=actions),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    async def post(
        self, payload: dict[str, object] | None = None, *, app: FastAPI | None = None, key: str = ""
    ) -> Response:
        return await _asgi_request(
            app or self.app,
            "POST",
            "/v1/product-config/apply",
            headers={
                "Authorization": "Bearer test-operator-token",
                **({"Idempotency-Key": key} if key else {}),
            },
            payload=payload or self.payload,
        )

    async def test_copy_stays_inside_service_and_replays_without_reading_value(self) -> None:
        with patch.object(
            secrets, "_decrypt_secret_value", side_effect=AssertionError("dry-run must not decrypt")
        ):
            dry_run = await self.post()
        self.assertEqual(dry_run.status_code, 202, dry_run.text)
        apply_payload = {**self.payload, "mode": "apply"}
        response = await self.post(apply_payload, key="copy-once")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn("source-only-test-token", dry_run.text + response.text)
        destination = secrets.resolve_site_secret_values(
            context_name="example-site",
            instance_name="testing",
            database_url=self.store.database_url,
            include_site_shared=True,
        )
        self.assertEqual(destination["REPAIRSHOPR_TOKEN"], "source-only-test-token")
        self.assertEqual(self.store.read_secret_record(self.source.secret_id), self.source)
        with patch.object(
            secrets, "_decrypt_secret_value", side_effect=AssertionError("replay must not decrypt")
        ):
            replay = await self.post(apply_payload, key="copy-once")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        binding = next(
            item
            for item in self.store.list_secret_bindings(limit=None)
            if item.instance == "testing"
        )
        self.assertEqual(binding.declared_secret_class, "shared_safe")
        assert binding.sharing_reason is not None
        self.assertEqual(binding.sharing_reason.kind, "read_only_source")

    async def test_copy_requires_matching_dry_run_and_source_read(self) -> None:
        response = await self.post({**self.payload, "mode": "apply"}, key="no-review")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")
        response = await self.post(app=self.make_app(source_read=False))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_plain_value_request_does_not_add_copy_fields_to_legacy_response(self) -> None:
        response = await self.post(
            {**self.source_payload, "mode": "dry-run", "reason": "Ordinary rotation"}
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn("copy_from", response.json()["result"]["secrets"][0])

    async def test_copy_refuses_foreign_lane_stale_source_and_unreasoned_class(self) -> None:
        for change in (
            {
                "copy_from": {
                    "context": "other-site",
                    "instance": "prod",
                    "version_id": self.source.current_version_id,
                }
            },
            {
                "copy_from": {
                    "context": "example-site",
                    "instance": "prod",
                    "version_id": "old-version",
                }
            },
            {"secret_class": None},
            {"sharing_reason": None},
            {"integration": "launchplane_worker"},
            {"scope": "context"},
            {"value": "should-not-be-accepted"},
        ):
            with self.subTest(change=change):
                payload = {**self.payload, "secrets": [{**self.payload["secrets"][0], **change}]}
                response = await self.post(payload)
                self.assertEqual(
                    response.status_code,
                    403 if change.get("scope") == "context" else 400,
                    response.text,
                )
                self.assertFalse(
                    any(item.instance == "testing" for item in self.store.list_secret_records())
                )

    async def test_source_rotation_invalidates_review(self) -> None:
        self.assertEqual((await self.post()).status_code, 202)
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                **self.source_payload,
                "secrets": [{"binding_key": "REPAIRSHOPR_TOKEN", "value": "rotated-token"}],
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        response = await self.post({**self.payload, "mode": "apply"}, key="stale-copy")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "secret_copy_refused")
        self.assertFalse(
            any(item.instance == "testing" for item in self.store.list_secret_records())
        )

    async def test_metadata_returns_owned_bindings_without_reading_versions(self) -> None:
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": "other-site",
                "context": "other-site",
                "instance": "prod",
                "secrets": [{"binding_key": "PASSWORD", "value": "foreign-secret"}],
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        with patch.object(
            self.store,
            "read_secret_version",
            side_effect=AssertionError("metadata must not read ciphertext"),
        ):
            response = await _asgi_request(
                self.app,
                "GET",
                "/v1/products/example-site/secret-bindings",
                headers={"Authorization": "Bearer test-operator-token"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        binding = response.json()["bindings"][0]
        self.assertEqual(binding["binding_key"], "REPAIRSHOPR_TOKEN")
        self.assertEqual(binding["version_id"], self.source.current_version_id)
        self.assertNotIn("foreign-secret", response.text)
        self.assertNotIn("other-site", response.text)
        self.assertNotIn("source-only-test-token", response.text)

    def test_copy_bundle_rechecks_product_and_context_ownership(self) -> None:
        for alter_profile in (True, False):
            with self.subTest(alter_profile=alter_profile):
                _, bundle = plan_product_config_authority_bundle(
                    record_store=self.store,
                    payload=self.payload,
                    mode="apply",
                    actor="test",
                    source_label="test",
                )
                if alter_profile:
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"updated_at": "2026-10-03T00:00:00Z"})
                    )
                    expected_error: type[Exception] = ProductProfileConflictError
                else:
                    self.store.write_product_profile_record(
                        LaunchplaneProductProfileRecord.model_validate(
                            {
                                **_generic_site_profile_payload("other-site"),
                                "lanes": [{"context": "example-site", "instance": "prod"}],
                            }
                        )
                    )
                    expected_error = ProductContextOwnershipError
                with self.assertRaises(expected_error):
                    self.store.write_product_authority_bundle(bundle)
                self.store.write_product_profile_record(self.profile)
                self.assertFalse(
                    any(item.instance == "testing" for item in self.store.list_secret_records())
                )

    def test_reference_contract_rejects_extra_product_or_missing_version(self) -> None:
        for reference in (
            {"context": "example-site", "instance": "prod"},
            {**self.payload["secrets"][0]["copy_from"], "product": "other-site"},
        ):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                ProductConfigApplyEnvelope.model_validate(
                    {
                        **self.payload,
                        "secrets": [{**self.payload["secrets"][0], "copy_from": reference}],
                    }
                )

    def test_source_change_or_binding_disable_aborts_atomic_copy(self) -> None:
        for disable_binding in (False, True):
            with self.subTest(disable_binding=disable_binding):
                _, bundle = plan_product_config_authority_bundle(
                    record_store=self.store,
                    payload=self.payload,
                    mode="apply",
                    actor="test",
                    source_label="test",
                )
                binding = self.store.list_secret_bindings(limit=None)[0]
                if disable_binding:
                    self.store.write_secret_binding(
                        binding.model_copy(update={"status": "disabled"})
                    )
                else:
                    self.store.write_secret_record(
                        self.source.model_copy(update={"status": "disabled"})
                    )
                with self.assertRaises(SecretCopySourceConflictError):
                    self.store.write_product_authority_bundle(bundle)
                self.store.write_secret_record(self.source)
                self.store.write_secret_binding(binding)
                self.assertFalse(
                    any(item.instance == "testing" for item in self.store.list_secret_records())
                )

    async def test_context_shared_source_is_copied_to_one_lane(self) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context",
            integration="runtime_environment",
            name="SITE_PASSWORD",
            binding_key="SITE_PASSWORD",
            plaintext_value="shared-test-password",
            context_name="example-site",
            actor="fixture",
            source_label="test",
        )
        shared = next(
            record for record in self.store.list_secret_records() if record.name == "SITE_PASSWORD"
        )
        payload = {
            **self.payload,
            "secrets": [
                {
                    **self.payload["secrets"][0],
                    "binding_key": "SITE_PASSWORD",
                    "copy_from": {
                        "context": "example-site",
                        "instance": "prod",
                        "version_id": shared.current_version_id,
                    },
                }
            ],
        }
        self.assertEqual((await self.post(payload)).status_code, 202)
        response = await self.post({**payload, "mode": "apply"}, key="shared-source")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.store.read_secret_record(shared.secret_id), shared)
        destination = next(
            record
            for record in self.store.list_secret_records()
            if record.name == "SITE_PASSWORD" and record.instance == "testing"
        )
        self.assertEqual(destination.scope, "context_instance")

    async def test_explicit_source_class_requires_authorized_source_reclassification(self) -> None:
        binding = self.store.list_secret_bindings(limit=None)[0]
        self.store.write_secret_binding(
            binding.model_copy(update={"declared_secret_class": "prod_only"})
        )
        refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "secret_copy_refused")
        reclassification = {**self.payload, "instance": "prod"}
        write_rule = (
            _local_operator_policy(actions=("product_config.plan", "product_config.apply"))
            .local_operators[0]
            .model_copy(update={"instances": ("testing",)})
        )
        read_rule = _local_operator_policy(actions=("secret.read",)).local_operators[0]
        testing_writer = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2, local_operators=(write_rule, read_rule)
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        denied = await self.post(reclassification, app=testing_writer)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        dry_run = await self.post(reclassification)
        self.assertEqual(dry_run.status_code, 202, dry_run.text)
        applied = await self.post({**reclassification, "mode": "apply"}, key="reclassify-source")
        self.assertEqual(applied.status_code, 202, applied.text)
        source = self.store.read_secret_record(self.source.secret_id)
        request = {
            **self.payload,
            "secrets": [
                {
                    **self.payload["secrets"][0],
                    "copy_from": {
                        "context": "example-site",
                        "instance": "prod",
                        "version_id": source.current_version_id,
                    },
                }
            ],
        }
        self.assertEqual((await self.post(request)).status_code, 202)
        copied = await self.post({**request, "mode": "apply"}, key="reclassified-copy")
        self.assertEqual(copied.status_code, 202, copied.text)
        version = self.store.read_secret_version(source.current_version_id)
        self.assertEqual(
            secrets._decrypt_secret_value(version.ciphertext, version.key_id),
            "source-only-test-token",
        )

    async def test_metadata_filters_restricted_bindings_instead_of_blocking_copy_discovery(
        self,
    ) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context",
            integration="runtime_environment",
            name="SITE_PASSWORD",
            binding_key="SITE_PASSWORD",
            context_name="example-site",
            plaintext_value="shared-test-password",
        )
        profile_rule = _local_operator_policy(actions=("product_profile.read",)).local_operators[0]
        list_rule = (
            _local_operator_policy(actions=("secret.list",))
            .local_operators[0]
            .model_copy(update={"instances": ("prod", "testing")})
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2, local_operators=(profile_rule, list_rule)
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        response = await _asgi_request(
            app,
            "GET",
            "/v1/products/example-site/secret-bindings",
            headers={"Authorization": "Bearer test-operator-token"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [item["binding_key"] for item in response.json()["bindings"]], ["REPAIRSHOPR_TOKEN"]
        )
        self.assertNotIn("SITE_PASSWORD", response.text)

    def test_filesystem_copy_commits_without_nested_lock_deadlock(self) -> None:
        root = Path(self.directory.name) / "file-state"
        store = FilesystemRecordStore(root)
        store.write_product_profile_record(self.profile)
        store.write_secret_record(self.source)
        store.write_secret_version(self.store.read_secret_version(self.source.current_version_id))
        store.write_secret_binding(self.store.list_secret_bindings(limit=None)[0])
        for policy in self.store.list_runtime_key_safety_policy_records():
            store.write_runtime_key_safety_policy_record(policy)
        script = """
import json, sys
from pathlib import Path
from control_plane import secrets
from control_plane.product_config import apply_product_config_bundle
from control_plane.storage.filesystem import FilesystemRecordStore
store = FilesystemRecordStore(Path(sys.argv[1]))
result = apply_product_config_bundle(record_store=store, payload=json.loads(sys.argv[2]), mode='apply', actor='test', source_label='test')
assert result['status'] == 'ok'
record = next(record for record in store.list_secret_records() if record.instance == 'testing')
version = store.read_secret_version(record.current_version_id)
assert secrets._decrypt_secret_value(version.ciphertext, version.key_id) == 'source-only-test-token'
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(root), json.dumps(self.payload)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
