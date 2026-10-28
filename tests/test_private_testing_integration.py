import base64
import contextlib
import io
import json
import os
import sys
import unittest
from control_plane import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetRecord,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.integration_readback import INTEGRATION_READBACK_PROGRAM
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.odoo_secret_references import plan_secret_reference_reconciliation
from control_plane.product_config_http import (
    ProductEnvironmentConfigApplyEnvelope,
    product_environment_config_apply_request,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig
from control_plane.product_config import apply_product_config_bundle, ProductConfigError
from control_plane.storage.product_authority_bundle import OdooInstanceOverrideConflictError
from tests.http_app_test_support import _RejectingVerifier
from tests.support.auth import _local_operator_policy
from tests.support.http import request as http_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _sqlite_database_url


STAMP = "2026-10-28T12:00:00Z"


def integration_profile(key: str) -> LaunchplaneProductProfileRecord:
    payload = _odoo_profile_payload_with_prod_lane()
    payload["expected_config"] = {
        "managed_secret_bindings": [
            {
                "binding_key": config_parameter_secret_env_key(key),
                "context": "cm",
                "instance": "testing",
            }
        ],
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def seed_integration(
    store: FilesystemRecordStore | PostgresRecordStore, key: str, *, write_override: bool = True
) -> tuple[LaunchplaneProductProfileRecord, OdooInstanceOverrideRecord]:
    profile = integration_profile(key)
    store.write_product_profile_record(profile)
    store.write_runtime_key_safety_policy_record(
        RuntimeKeySafetyPolicyRecord(
            record_id="test-policy",
            source="test",
            updated_at=STAMP,
            rules=(
                RuntimeSecretSafetyRule(binding_key="UNRELATED_PASSWORD", secret_class="prod_only"),
            ),
            integration_key_markers=("ACME",),
        )
    )
    store.write_dokploy_target_record(
        DokployTargetRecord(
            context="cm",
            instance="testing",
            updated_at=STAMP,
            policies=DokployTargetPolicies(
                integration_allowances=tuple(
                    DokployTargetIntegrationAllowance(
                        integration=name,
                        kind="dev_store",
                        reason="Independently isolated test account",
                        evidence="Provider account and test device verified by the admin",
                    )
                    for name in ("printnode", "acme")
                )
            ),
        )
    )
    store.write_secret_record(
        SecretRecord(
            secret_id="private-testing-secret",
            scope="context_instance",
            integration="runtime_environment",
            name="private-testing-secret",
            context="cm",
            instance="testing",
            current_version_id="test-version",
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="private-testing-binding",
            secret_id="private-testing-secret",
            integration="runtime_environment",
            binding_key=config_parameter_secret_env_key(key),
            context="cm",
            instance="testing",
            declared_secret_class="testing",
            value_source="supplied",
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    record = OdooInstanceOverrideRecord(context="cm", instance="testing", updated_at=STAMP)
    if write_override:
        store.write_odoo_instance_override_record(record)
    return profile, record


class PrivateTestingIntegrationTests(unittest.TestCase):
    def test_supplied_testing_class_does_not_bypass_integration_safety_policy(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            profile, _ = seed_integration(store, "printnode.api_key")
            binding = store.list_secret_bindings()[0]
            store.write_secret_binding(binding.model_copy(update={"status": "disabled"}))
            lane = profile.lanes[0]
            for classification in (None, "prod_only", "testing"):
                with self.subTest(classification=classification):
                    request = ProductEnvironmentConfigApplyEnvelope.model_validate(
                        {
                            "mode": "dry-run",
                            "managed_secrets": [
                                {
                                    "binding_key": config_parameter_secret_env_key(
                                        "printnode.api_key"
                                    ),
                                    "integration": "runtime_environment",
                                    "value": "inert-private-test-value",
                                    "secret_class": classification,
                                }
                            ],
                        }
                    )
                    translated = product_environment_config_apply_request(
                        profile=profile, lane=lane, request=request, integration_key_markers=()
                    )
                    with patch.dict(
                        os.environ,
                        {
                            secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "inert-encryption-master-key"
                        },
                    ):
                        if classification == "testing":
                            result = apply_product_config_bundle(
                                record_store=store,
                                payload=translated.model_dump(mode="json", exclude_none=True),
                                mode="dry-run",
                                actor="admin",
                                source_label="test",
                            )
                            self.assertNotIn("inert-private-test-value", json.dumps(result))
                        else:
                            with self.assertRaises(ProductConfigError):
                                apply_product_config_bundle(
                                    record_store=store,
                                    payload=translated.model_dump(mode="json", exclude_none=True),
                                    mode="dry-run",
                                    actor="admin",
                                    source_label="test",
                                )

    def test_environment_form_preserves_explicit_testing_class_for_integration_key(self) -> None:
        for key in ("printnode.api_key", "acme.api_token"):
            with self.subTest(key=key):
                profile = integration_profile(key)
                request = ProductEnvironmentConfigApplyEnvelope.model_validate(
                    {
                        "mode": "dry-run",
                        "reason": "Isolated test account verified",
                        "managed_secrets": [
                            {
                                "integration": "runtime_environment",
                                "binding_key": config_parameter_secret_env_key(key),
                                "value": "inert-private-test-value",
                                "secret_class": "testing",
                            }
                        ],
                    }
                )
                translated = product_environment_config_apply_request(
                    profile=profile,
                    lane=profile.lanes[0],
                    request=request,
                    integration_key_markers=("ACME",),
                )
                self.assertEqual(translated.secrets[0].secret_class, "testing")
                self.assertEqual(translated.secrets[0].scope, "context_instance")

    def test_new_parameter_reference_uses_existing_delivery_without_reading_values(self) -> None:
        for sql in (False, True):
            for key in ("printnode.api_key", "acme.api_token"):
                with self.subTest(sql=sql, key=key), TemporaryDirectory() as directory:
                    store = (
                        PostgresRecordStore(
                            database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                        )
                        if sql
                        else FilesystemRecordStore(Path(directory))
                    )
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                        self.addCleanup(store.close)
                    profile, before = seed_integration(store, key)
                    with (
                        patch(
                            "control_plane.storage.filesystem.FilesystemRecordStore.read_secret_version",
                            side_effect=AssertionError("No secret reads"),
                        ),
                        patch(
                            "control_plane.storage.postgres.PostgresRecordStore.read_secret_version",
                            side_effect=AssertionError("No secret reads"),
                        ),
                    ):
                        plan, bundle = plan_secret_reference_reconciliation(
                            store=store,
                            profile=profile,
                            record=before,
                            keys=(key,),
                            create_missing=True,
                        )
                        self.assertEqual(plan.entries[0].action, "add_reference")
                        store.write_product_authority_bundle(bundle)
                    after = store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="testing"
                    )
                    self.assertEqual(after.config_parameters[0].key, key)
                    self.assertEqual(after.config_parameters[0].value.source, "secret_binding")
                    self.assertEqual(
                        after.config_parameters[0].value.secret_binding_id,
                        "private-testing-binding",
                    )
                    self.assertEqual(after.apply_on, ("deploy", "promotion"))
                    self.assertNotIn("private-testing-binding", plan.model_dump_json())

    def test_post_deploy_reports_parameter_names_and_nonempty_database_presence(self) -> None:
        for present in (True, False):
            with self.subTest(present=present):
                cursor = MagicMock()
                cursor.fetchall.return_value = [("printnode.api_key",)] if present else []
                connection = MagicMock()
                connection.cursor.return_value.__enter__.return_value = cursor
                spec = {
                    "families": [],
                    "allowed_integrations": [],
                    "connection_isolation_required": False,
                    "protected_shopify_store_handles": [],
                }
                payload = {
                    "config_parameters": [
                        {
                            "key": "printnode.api_key",
                            "value": {
                                "source": "secret_binding",
                                "environment_variable": config_parameter_secret_env_key(
                                    "printnode.api_key"
                                ),
                                "secret_binding_id": "private-testing-binding",
                            },
                        }
                    ]
                }
                output = io.StringIO()
                environment = {
                    "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": base64.b64encode(
                        json.dumps(payload).encode()
                    ).decode()
                }
                with (
                    patch.dict(
                        sys.modules,
                        {"psycopg2": SimpleNamespace(connect=lambda **kwargs: connection)},
                    ),
                    patch.dict(os.environ, environment),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "readback",
                            "testing",
                            base64.b64encode(json.dumps(spec).encode()).decode(),
                        ],
                    ),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    try:
                        exec(INTEGRATION_READBACK_PROGRAM, {})
                    except SystemExit:
                        self.assertFalse(present)
                expected = (
                    "odoo_config_parameter_present" if present else "odoo_config_parameter_missing"
                )
                self.assertIn(expected + "=printnode.api_key", output.getvalue())
                self.assertNotIn("private-testing-binding", output.getvalue())
                connection.close.assert_called_once()

    def test_new_reference_refuses_unqualified_scope_class_source_and_allowance(self) -> None:
        for change in (
            "class",
            "copy",
            "provider",
            "scope",
            "allowance",
            "evidence",
            "declaration",
            "literal",
            "collision",
            "production",
            "policy",
        ):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                profile, record = seed_integration(store, "printnode.api_key")
                binding = store.list_secret_bindings()[0]
                if change in {"class", "copy", "provider", "scope"}:
                    updates: dict[str, object] = (
                        {"declared_secret_class": "prod_only"}
                        if change == "class"
                        else {"value_source": change}
                    )
                    if change == "scope":
                        updates = {"instance": "", "declared_secret_class": None}
                    store.write_secret_binding(binding.model_copy(update=updates))
                elif change in {"allowance", "evidence"}:
                    target = store.read_dokploy_target_record(
                        context_name="cm", instance_name="testing"
                    )
                    allowances = tuple(
                        item.model_copy(
                            update={"kind": "pre_live"}
                            if change == "allowance"
                            else {"evidence": ""}
                        )
                        for item in target.policies.integration_allowances
                    )
                    store.write_dokploy_target_record(
                        target.model_copy(
                            update={
                                "policies": target.policies.model_copy(
                                    update={"integration_allowances": allowances}
                                )
                            }
                        )
                    )
                elif change == "declaration":
                    profile = profile.model_copy(
                        update={
                            "expected_config": profile.expected_config.model_copy(
                                update={"managed_secret_bindings": ()}
                            )
                        }
                    )
                elif change in {"literal", "collision"}:
                    record = record.model_copy(
                        update={
                            "config_parameters": (
                                OdooConfigParameterOverride(
                                    key="printnode.api_key"
                                    if change == "literal"
                                    else "printnode__api_key",
                                    value=OdooOverrideValue(
                                        source="literal", value="existing-intent"
                                    ),
                                ),
                            )
                        }
                    )
                elif change == "production":
                    record = record.model_copy(update={"instance": "prod"})
                elif change == "policy":
                    policy = store.list_runtime_key_safety_policy_records()[0]
                    store.write_runtime_key_safety_policy_record(
                        policy.model_copy(
                            update={
                                "rules": (
                                    RuntimeSecretSafetyRule(
                                        binding_key=binding.binding_key, secret_class="prod_only"
                                    ),
                                )
                            }
                        )
                    )
                with self.assertRaises((ValueError, FileNotFoundError)):
                    plan_secret_reference_reconciliation(
                        store=store,
                        profile=profile,
                        record=record,
                        keys=("printnode.api_key",),
                        create_missing=True,
                    )

    def test_absent_override_creation_is_guarded_in_both_stores(self) -> None:
        for sql in (False, True):
            with self.subTest(sql=sql), TemporaryDirectory() as directory:
                store = (
                    PostgresRecordStore(
                        database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                    )
                    if sql
                    else FilesystemRecordStore(Path(directory))
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                    self.addCleanup(store.close)
                profile, record = seed_integration(store, "printnode.api_key")
                plan, bundle = plan_secret_reference_reconciliation(
                    store=store,
                    profile=profile,
                    record=None,
                    environment="testing",
                    keys=("printnode.api_key",),
                    create_missing=True,
                )
                self.assertEqual(plan.entries[0].action, "add_reference")
                with self.assertRaises(OdooInstanceOverrideConflictError):
                    store.write_product_authority_bundle(bundle)
                self.assertEqual(
                    store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="testing"
                    ),
                    record,
                )

    def test_new_lane_without_an_override_gets_a_typed_record(self) -> None:
        for sql in (False, True):
            with self.subTest(sql=sql), TemporaryDirectory() as directory:
                store = (
                    PostgresRecordStore(
                        database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                    )
                    if sql
                    else FilesystemRecordStore(Path(directory))
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                    self.addCleanup(store.close)
                profile, _ = seed_integration(store, "printnode.api_key", write_override=False)
                _, bundle = plan_secret_reference_reconciliation(
                    store=store,
                    profile=profile,
                    record=None,
                    environment="testing",
                    keys=("printnode.api_key",),
                    create_missing=True,
                )
                store.write_product_authority_bundle(bundle)
                after = store.read_odoo_instance_override_record(
                    context_name="cm", instance_name="testing"
                )
                self.assertEqual(after.config_parameters[0].key, "printnode.api_key")
                self.assertEqual(after.config_parameters[0].value.source, "secret_binding")


class PrivateTestingIntegrationHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary.name) / "records.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile, self.before = seed_integration(self.store, "printnode.api_key")
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            authz_policy=_local_operator_policy(
                actions=("product_environment.read", "product_config.plan", "product_config.apply"),
                products=(self.profile.product,),
                contexts=("cm",),
            ),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        self.path = f"/v1/products/{self.profile.product}/environments/testing/odoo-secret-references/reconcile"

    async def test_admin_plan_apply_receipt_and_stale_review_preserve_secret_privacy(self) -> None:
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("No secret value reads")
        ):
            plan = await http_request(
                self.app,
                "POST",
                self.path,
                headers={"Authorization": "Bearer fixture-token"},
                payload={"keys": ["printnode.api_key"], "create_missing": True},
            )
            self.assertEqual(plan.status_code, 200, plan.text)
            body = {
                "mode": "apply",
                "keys": ["printnode.api_key"],
                "create_missing": True,
                "review_digest": plan.json()["result"]["review_digest"],
                "confirmation": f"APPLY {self.profile.product}/testing",
            }
            response = await http_request(
                self.app,
                "POST",
                self.path,
                headers={
                    "Authorization": "Bearer fixture-token",
                    "Idempotency-Key": "reference-original",
                },
                payload=body,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["result"]["read_back_matches"])
            replay = await http_request(
                self.app,
                "POST",
                self.path,
                headers={
                    "Authorization": "Bearer fixture-token",
                    "Idempotency-Key": "reference-original",
                },
                payload=body,
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["original_trace_id"], response.json()["trace_id"])
            stale = await http_request(
                self.app,
                "POST",
                self.path,
                headers={
                    "Authorization": "Bearer fixture-token",
                    "Idempotency-Key": "reference-new",
                },
                payload=body,
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertNotIn("private-testing-binding", response.text)
            self.assertEqual(self.store.list_secret_records()[0].current_version_id, "test-version")
