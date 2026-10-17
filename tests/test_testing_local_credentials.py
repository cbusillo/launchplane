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
from tests.support.auth import _local_operator_policy
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

    def test_onboarding_generates_new_lane_once_and_reimport_preserves_it(self) -> None:
        from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
        from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest
        from tests.test_product_onboarding import _manifest_payload

        payload: dict[str, Any] = _manifest_payload()
        payload["expected_config"]["managed_secret_bindings"].append(
            {"binding_key": "DATABASE_PASSWORD", "testing_local_credential": True}
        )
        manifest = ProductOnboardingManifest.model_validate(payload)
        apply_product_onboarding_manifest(record_store=self.store, manifest=manifest)
        records = self.store.list_secret_records(
            context_name="example-site-testing", instance_name="testing"
        )
        self.assertTrue(records)
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
        with patch(
            "secrets.token_urlsafe", side_effect=AssertionError("dry-run generated a password")
        ):
            planned = await self.post(self.payload)
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
        self.assertGreaterEqual(len(password), 40)
        self.assertNotEqual(password, "production-fixture")
        self.assertNotIn(password, planned.text + applied.text)
        binding = next(b for b in self.store.list_secret_bindings() if b.instance == "testing")
        self.assertEqual(binding.value_source, "generated")
        audit = self.store.list_secret_audit_events(secret_id=binding.secret_id)
        self.assertTrue(any(e.metadata.get("value_source") == "service_generated" for e in audit))
        with patch(
            "secrets.token_urlsafe", side_effect=AssertionError("replay generated a password")
        ):
            replay = await self.post(payload, "generate-once")
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
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")
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
