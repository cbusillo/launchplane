from __future__ import annotations

import json
import contextlib
import io
import os
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_config import apply_product_config_bundle
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _RejectingVerifier
from tests.support.auth import _local_operator_policy, scoped_agent_test
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url, _write_runtime_key_safety_policy


class TestingLocalCredentialsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        database_url = _sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        _write_runtime_key_safety_policy(database_url=database_url)
        self.store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(self.store.close)
        profile_payload: dict[str, Any] = _generic_site_profile_payload()
        profile_payload["expected_config"]["managed_secret_bindings"].append(
            {"binding_key": "DATABASE_PASSWORD", "testing_local_credential": True}
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        self.store.write_product_profile_record(self.profile)
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": "example-site",
                "instance": "prod",
                "secrets": [{"binding_key": "DATABASE_PASSWORD", "value": "production-fixture"}],
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        self.production = self.store.list_secret_records(instance_name="prod")
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=(
                    "product_config.plan",
                    "product_config.apply",
                    "product_environment.read",
                    "product_profile.read",
                )
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        self.payload: dict[str, Any] = {
            "product": "example-site",
            "context": "example-site",
            "instance": "testing",
            "mode": "dry-run",
            "reason": "Independent local database password",
            "secrets": [
                {
                    "binding_key": "DATABASE_PASSWORD",
                    "generate": "testing_local",
                    "secret_class": "testing",
                }
            ],
        }

    async def post(self, payload: dict[str, Any], key: str = "") -> Any:
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/product-config/apply",
            payload=payload,
            headers={
                "Authorization": "Bearer test-operator-token",
                **({"Idempotency-Key": key} if key else {}),
            },
        )

    async def audit(self, *, inventory: bool = False) -> Any:
        return await _asgi_request(
            self.app,
            "GET",
            "/v1/testing-credentials"
            if inventory
            else "/v1/products/example-site/testing-credentials",
            headers={"Authorization": "Bearer test-operator-token"},
        )

    async def test_audit_finds_cross_key_overlap_then_reports_generated_readback(self) -> None:
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": "example-site",
                "context": "example-site",
                "instance": "testing",
                "secrets": [{"binding_key": "DATABASE_PASSWORD", "value": "production-fixture"}],
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        report = await self.audit(inventory=True)
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["products"], [self.profile.product])
        lane = report.json()["lanes"][0]
        self.assertEqual(lane["status"], "overlap")
        self.assertEqual(lane["overlaps"][0]["testing_key"], "DATABASE_PASSWORD")
        self.assertNotIn("production-fixture", report.text)
        self.assertEqual((await self.post(self.payload)).status_code, 202)
        response = await self.post({**self.payload, "mode": "apply"}, "isolate")
        self.assertEqual(response.status_code, 202, response.text)
        readback = await self.audit()
        lane = readback.json()["lanes"][0]
        self.assertEqual(lane["status"], "pass")
        self.assertFalse(lane["overlaps"])
        self.assertEqual(set(lane["generated_keys"]), set(lane["local_keys"]))

    async def test_external_overlap_fails_until_retired_or_recorded_as_shared(self) -> None:
        self.assertEqual((await self.post(self.payload)).status_code, 202)
        response = await self.post({**self.payload, "mode": "apply"}, "local-first")
        self.assertEqual(response.status_code, 202, response.text)

        def store_testing_key(secret: dict[str, Any]) -> None:
            for instance in ("prod", "testing"):
                apply_product_config_bundle(
                    record_store=self.store,
                    payload={
                        "product": "example-site",
                        "context": "example-site",
                        "instance": instance,
                        "secrets": [
                            {
                                "binding_key": "SOURCE_TOKEN",
                                "value": "production-external-fixture",
                                **(secret if instance == "testing" else {}),
                            }
                        ],
                    },
                    mode="apply",
                    actor="fixture",
                    source_label="test",
                )

        store_testing_key({})
        lane = (await self.audit()).json()["lanes"][0]
        self.assertEqual(lane["status"], "overlap")
        self.assertEqual(
            [(item["testing_key"], item["sharing_kind"]) for item in lane["overlaps"]],
            [("SOURCE_TOKEN", None)],
        )

        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "schema_version": 2,
                "product": "example-site",
                "context": "example-site",
                "instance": "testing",
                "runtime_env": {
                    "env": {"SITE_MODE": "testing"},
                    "retired_provider_keys": ["SOURCE_TOKEN"],
                },
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        lane = (await self.audit()).json()["lanes"][0]
        self.assertEqual((lane["status"], lane["overlaps"]), ("pass", []))

        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "schema_version": 2,
                "product": "example-site",
                "context": "example-site",
                "instance": "testing",
                "runtime_env": {"retired_provider_keys": []},
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        # A decision that does not record read-only access stays a failing overlap.
        store_testing_key(
            {
                "secret_class": "shared_safe",
                "sharing_reason": {
                    "kind": "site_shared",
                    "reason": "Shared on purpose",
                    "evidence": "Fixture token can write",
                },
            }
        )
        lane = (await self.audit()).json()["lanes"][0]
        self.assertEqual(lane["status"], "overlap")
        self.assertEqual(
            [(item["testing_key"], item["sharing_kind"]) for item in lane["overlaps"]],
            [("SOURCE_TOKEN", "site_shared")],
        )

        store_testing_key(
            {
                "secret_class": "shared_safe",
                "sharing_reason": {
                    "kind": "read_only_source",
                    "reason": "Read-only integration",
                    "evidence": "Fixture source has read-only permissions",
                },
            }
        )
        report = await self.audit()
        lane = report.json()["lanes"][0]
        self.assertEqual(lane["status"], "pass")
        self.assertEqual(
            [(item["testing_key"], item["sharing_kind"]) for item in lane["overlaps"]],
            [("SOURCE_TOKEN", "read_only_source")],
        )
        self.assertNotIn("production-external-fixture", report.text)

    async def test_missing_or_undecryptable_credentials_are_unavailable(self) -> None:
        report = await self.audit()
        self.assertEqual(report.json()["lanes"][0]["status"], "unavailable")
        with patch.object(
            secrets, "_decrypt_secret_value", side_effect=ValueError("private-fixture")
        ):
            report = await self.audit()
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["lanes"][0]["status"], "unavailable")
        self.assertNotIn("private-fixture", report.text)

    async def test_undeclared_external_key_cannot_be_generated(self) -> None:
        response = await self.post(
            {
                **self.payload,
                "secrets": [{**self.payload["secrets"][0], "binding_key": "SMTP_PASSWORD"}],
            }
        )
        self.assertEqual(response.status_code, 400, response.text)

    async def test_audit_checks_other_key_names_and_refuses_ambiguous_bindings(self) -> None:
        for instance, key in (("prod", "ANOTHER_PASSWORD"), ("testing", "DATABASE_PASSWORD")):
            apply_product_config_bundle(
                record_store=self.store,
                payload={
                    "product": "example-site",
                    "context": "example-site",
                    "instance": instance,
                    "secrets": [{"binding_key": key, "value": "cross-key-fixture"}],
                },
                mode="apply",
                actor="fixture",
                source_label="test",
            )
        report = await self.audit()
        self.assertEqual(
            report.json()["lanes"][0]["overlaps"][0]["production_key"], "ANOTHER_PASSWORD"
        )
        binding = next(
            binding
            for binding in self.store.list_secret_bindings()
            if binding.instance == "testing"
        )
        self.store.write_secret_binding(
            binding.model_copy(update={"binding_id": "ambiguous-legacy-binding"})
        )
        report = await self.audit()
        self.assertEqual(report.json()["lanes"][0]["status"], "unavailable")
        self.assertNotIn("cross-key-fixture", report.text)
        response = await self.post(self.payload)
        self.assertEqual(response.status_code, 400, response.text)

    async def test_client_saves_value_free_review_then_independently_reads_apply(self) -> None:
        from unittest.mock import Mock

        # An approved shared integration remains visible without failing local-password readback.
        for instance in ("prod", "testing"):
            apply_product_config_bundle(
                record_store=self.store,
                payload={
                    "product": "example-site",
                    "context": "example-site",
                    "instance": instance,
                    "secrets": [
                        {
                            "binding_key": "SOURCE_TOKEN",
                            "value": "read-only-fixture",
                            "secret_class": "shared_safe",
                            "sharing_reason": {
                                "kind": "read_only_source",
                                "reason": "Read-only integration",
                                "evidence": "Fixture source has read-only permissions",
                            },
                        }
                    ],
                },
                mode="apply",
                actor="fixture",
                source_label="test",
            )

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/prepare-testing-credentials.py")
        )
        helper = Path(self.directory.name) / "launchplane-write-action.py"
        helper.touch()
        evidence = Path(self.directory.name) / "review.json"
        report_before = (await self.audit()).json()
        response = await self.post(self.payload)
        plan = response.json()
        settings = {"service_url": "https://fixture.example.invalid", "token": "fixture-only"}
        reader = Mock(return_value=report_before)
        writer = Mock(return_value=plan)
        transport = {
            "resolve_settings": lambda _: settings,
            "validate_service_url": lambda _: None,
            "request_launchplane_read": reader,
            "request_launchplane": writer,
            "public_trace_id": lambda value: value,
        }
        argv = [
            "client",
            "dry-run",
            "--product",
            "example-site",
            "--context",
            "example-site",
            "--operator-helper",
            str(helper),
            "--reason",
            self.payload["reason"],
            "--evidence-file",
            str(evidence),
        ]
        with (
            patch("runpy.run_path", return_value=transport),
            patch("sys.argv", argv),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(client["main"](), 0)
        saved = json.loads(evidence.read_text())
        self.assertNotIn("value", saved["request"]["secrets"][0])
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        applied = await self.post({**self.payload, "mode": "apply"}, "client-fixture")
        writer.return_value = applied.json()
        report_after = (await self.audit()).json()
        self.assertTrue(report_after["lanes"][0]["overlaps"])
        reader.reset_mock(side_effect=True)
        reader.side_effect = [report_before, report_after]
        argv[1] = "apply"
        argv += ["--reviewed-dry-run", "--idempotency-key", "client-fixture"]
        with (
            patch("runpy.run_path", return_value=transport),
            patch("sys.argv", argv),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(client["main"](), 0)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(writer.call_args.kwargs["body"]["secrets"], saved["request"]["secrets"])

    async def test_testing_only_local_key_needs_no_same_named_production_key(self) -> None:
        from control_plane.contracts.product_profile_record import ProductSecretConfigRequirement

        requirement = ProductSecretConfigRequirement(
            binding_key="TESTING_SIGNING_SECRET",
            context="example-site",
            instance="testing",
            testing_local_credential=True,
        )
        self.profile = self.profile.model_copy(
            update={
                "expected_config": self.profile.expected_config.model_copy(
                    update={"managed_secret_bindings": (requirement,)}
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        payload = {
            **self.payload,
            "secrets": [
                {
                    "binding_key": requirement.binding_key,
                    "generate": "testing_local",
                    "secret_class": "testing",
                }
            ],
        }
        self.assertEqual((await self.post(payload)).status_code, 202)
        applied = await self.post({**payload, "mode": "apply"}, "testing-only-local")
        self.assertEqual(applied.status_code, 202, applied.text)
        report = (await self.audit()).json()["lanes"][0]
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["missing_keys"])
        self.assertEqual(
            report["production_keys"]["example-site/example-site/prod"], ["DATABASE_PASSWORD"]
        )

    async def test_inventory_compares_against_other_products_production(self) -> None:
        other = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload("another-site")
        )
        self.store.write_product_profile_record(other)
        for product, instance, key in (
            (other.product, "prod", "ANOTHER_PASSWORD"),
            (self.profile.product, "testing", "DATABASE_PASSWORD"),
        ):
            apply_product_config_bundle(
                record_store=self.store,
                payload={
                    "product": product,
                    "context": product,
                    "instance": instance,
                    "secrets": [{"binding_key": key, "value": "cross-product-fixture"}],
                },
                mode="apply",
                actor="fixture",
                source_label="test",
            )
        report = await self.audit(inventory=True)
        self.assertEqual(report.status_code, 200, report.text)
        lane = next(
            lane for lane in report.json()["lanes"] if lane["product"] == self.profile.product
        )
        self.assertEqual(lane["overlaps"][0]["production_product"], other.product)
        self.assertNotIn("cross-product-fixture", report.text)

    @scoped_agent_test
    async def test_inventory_authorizes_every_product_before_decryption(self) -> None:
        other = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload("another-site")
        )
        self.store.write_product_profile_record(other)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                products=(self.profile.product,),
                actions=("product_profile.read", "product_environment.read"),
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        with patch.object(
            secrets, "_decrypt_secret_value", side_effect=AssertionError("unauthorized audit read")
        ):
            response = await _asgi_request(
                app,
                "GET",
                "/v1/testing-credentials",
                headers={"Authorization": "Bearer test-operator-token"},
            )
        self.assertEqual(response.status_code, 403, response.text)

    def test_onboarding_import_preserves_existing_lane_credentials(self) -> None:
        from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
        from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest
        from tests.test_product_onboarding import _manifest_payload

        payload: dict[str, Any] = _manifest_payload()
        payload["expected_config"]["managed_secret_bindings"].append(
            {"binding_key": "DATABASE_PASSWORD", "testing_local_credential": True}
        )
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": "example-site-testing",
                "instance": "testing",
                "secrets": [
                    {"binding_key": "DATABASE_PASSWORD", "value": "retained-import-password"}
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="test",
        )
        before = self.store.list_secret_records(
            context_name="example-site-testing", instance_name="testing"
        )
        manifest = ProductOnboardingManifest.model_validate(payload)
        apply_product_onboarding_manifest(record_store=self.store, manifest=manifest)
        records = self.store.list_secret_records(
            context_name="example-site-testing", instance_name="testing"
        )
        self.assertEqual(records, before)
        self.assertTrue(all(record.scope == "context_instance" for record in records))
        apply_product_onboarding_manifest(record_store=self.store, manifest=manifest)
        self.assertEqual(
            self.store.list_secret_records(
                context_name="example-site-testing", instance_name="testing"
            ),
            records,
        )
        self.assertEqual(self.store.list_secret_records(instance_name="prod"), self.production)

    def test_replacement_bundle_rotates_only_testing_and_commits_atomically(self) -> None:
        from control_plane.testing_local_credentials import with_generated_credentials
        from control_plane.storage.product_authority_bundle import (
            ProductAuthorityBundle,
            ProductProfileConflictError,
        )

        bundle = with_generated_credentials(
            ProductAuthorityBundle(expected_product_profiles=(self.profile,)),
            self.store,
            self.profile,
            "example-site",
            actor="test:rebuild",
            generation="new-empty-recipient",
        )
        self.assertFalse(self.store.list_secret_records(instance_name="testing"))
        changed = self.profile.model_copy(update={"display_name": "Changed during rebuild"})
        self.store.write_product_profile_record(changed)
        with self.assertRaises(ProductProfileConflictError):
            self.store.write_product_authority_bundle(bundle)
        self.assertFalse(self.store.list_secret_records(instance_name="testing"))
        self.store.write_product_profile_record(self.profile)
        self.store.write_product_authority_bundle(bundle)
        first = self.store.list_secret_records(instance_name="testing")[0]
        replacement = with_generated_credentials(
            ProductAuthorityBundle(expected_product_profiles=(self.profile,)),
            self.store,
            self.profile,
            "example-site",
            actor="test:rebuild",
            generation="another-empty-recipient",
        )
        self.store.write_product_authority_bundle(replacement)
        second = self.store.read_secret_record(first.secret_id)
        self.assertNotEqual(first.current_version_id, second.current_version_id)
        self.assertEqual(self.store.list_secret_records(instance_name="prod"), self.production)

    async def test_service_generates_only_on_apply_and_replays_without_generation(self) -> None:
        before = self.store.list_secret_records()
        planned = await self.post(self.payload)
        self.assertEqual(self.store.list_secret_records(), before)
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertFalse(self.store.list_secret_records(instance_name="testing"))
        payload = {**self.payload, "mode": "apply"}
        applied = await self.post(payload, "generate-once")
        self.assertEqual(applied.status_code, 202, applied.text)
        values = secrets.resolve_site_secret_values(
            context_name="example-site",
            instance_name="testing",
            database_url=self.store.database_url,
            include_site_shared=True,
        )
        password = values["DATABASE_PASSWORD"]
        self.assertNotEqual(password, "production-fixture")
        self.assertNotIn(password, planned.text + applied.text)
        binding = next(b for b in self.store.list_secret_bindings() if b.instance == "testing")
        self.assertEqual(binding.value_source, "generated")
        audit = self.store.list_secret_audit_events(secret_id=binding.secret_id)
        self.assertTrue(any(e.metadata.get("value_source") == "service_generated" for e in audit))
        versions = self.store.list_secret_versions(secret_id=binding.secret_id)
        replay = await self.post(payload, "generate-once")
        self.assertEqual(self.store.list_secret_versions(secret_id=binding.secret_id), versions)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.store.list_secret_records(instance_name="prod"), self.production)
        self.assertNotIn(password, json.dumps([e.model_dump() for e in audit]))

    async def test_generation_refuses_production_shared_and_other_purpose_destinations(
        self,
    ) -> None:
        for mutation in (
            {"instance": "prod"},
            {"instance": "pr-12"},
            {"secrets": [{**self.payload["secrets"][0], "scope": "context"}]},
            {"secrets": [{**self.payload["secrets"][0], "integration": "launchplane_worker"}]},
            {"secrets": [{**self.payload["secrets"][0], "value": "caller-value"}]},
        ):
            with self.subTest(mutation=mutation):
                response = await self.post({**self.payload, **mutation})
                self.assertIn(response.status_code, (400, 403), response.text)
        self.assertEqual(self.store.list_secret_records(instance_name="prod"), self.production)

    async def test_apply_requires_review_and_each_new_request_generates_independently(self) -> None:
        payload = {**self.payload, "mode": "apply"}
        response = await self.post(payload, "unreviewed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.store.list_secret_records(instance_name="testing"))
        first = await self.post(self.payload)
        self.assertEqual(first.status_code, 202, first.text)
        applied = await self.post(payload, "first")
        self.assertEqual(applied.status_code, 202, applied.text)
        first_record = self.store.list_secret_records(instance_name="testing")[0]
        rotated = await self.post(payload, "second")
        self.assertEqual(rotated.status_code, 202, rotated.text)
        second_record = self.store.read_secret_record(first_record.secret_id)
        first_version = self.store.read_secret_version(first_record.current_version_id)
        second_version = self.store.read_secret_version(second_record.current_version_id)
        first_value = secrets._decrypt_secret_value(first_version.ciphertext, first_version.key_id)
        second_value = secrets._decrypt_secret_value(
            second_version.ciphertext, second_version.key_id
        )
        self.assertNotEqual(first_value, second_value)
