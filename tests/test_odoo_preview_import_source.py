import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)
from tests.support.auth import _local_operator_policy, scoped_agent_test
from tests.http_app_test_support import _asgi_request, _RejectingVerifier
from tests.test_odoo_preview_apply_execution import _issued_plan
from control_plane import secrets
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.odoo_preview_apply_http import _odoo_preview_service_environment
from control_plane.odoo_preview_import_source import (
    FishbowlPreviewSource,
    resolve_fishbowl_preview_source,
)
from control_plane.product_config import apply_product_config_bundle
from control_plane.product_secret_copy import ProductSecretCopyFrom
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import (
    _sqlite_database_url,
    _write_odoo_preview_template_runtime_environment,
    _write_runtime_key_safety_policy,
)
from tests.test_odoo_preview_apply_execution import _refresh_payload
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope


class FishbowlPreviewSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(self.root / "state.sqlite")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        _write_runtime_key_safety_policy(database_url=self.store.database_url)
        env = patch.dict(
            os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.allowance = DokployTargetIntegrationAllowance(
            integration="fishbowl",
            kind="read_only_source",
            reason="Import-only source",
            evidence="Fixture account verified SELECT-only",
        )
        self.target = DokployTargetRecord(
            context="cm",
            instance="testing",
            updated_at=self.profile.updated_at,
            policies=DokployTargetPolicies(integration_allowances=(self.allowance,)),
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id="fixture-target",
                updated_at=self.profile.updated_at,
            )
        )
        apply_product_config_bundle(
            record_store=self.store,
            mode="apply",
            actor="fixture",
            source_label="test",
            payload={
                "product": self.profile.product,
                "context": "cm",
                "instance": "testing",
                "secrets": [
                    {
                        "binding_key": "ODOO_OVERRIDE_SECRET__CONFIG_PARAM__FISHBOWL__PASSWORD",
                        "value": "private-fixture-source-password",
                        "secret_class": "shared_safe",
                        "sharing_reason": {
                            "kind": "read_only_source",
                            "reason": "Source import",
                            "evidence": "Fixture SELECT-only grant",
                        },
                    }
                ],
            },
        )
        self.record = self.store.list_secret_records(integration="runtime_environment")[0]
        self.binding = self.store.list_secret_bindings(integration="runtime_environment")[0]
        self.reference = ProductSecretCopyFrom(
            context="cm",
            instance="testing",
            version_id=self.record.current_version_id,
        )
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            updated_at=self.profile.updated_at,
            env={
                "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__HOST": "source.example.test",
                "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__USER": "readonly",
                "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__DB": "inventory",
                "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__PORT": 3306,
                "ENV_OVERRIDE_CONFIG_PARAM__REPAIRSHOPR__SYNC_DB__PASSWORD": "must-not-copy",
            },
        )
        self.store.write_runtime_environment_record(self.runtime)

    def resolve(self) -> FishbowlPreviewSource:
        return resolve_fishbowl_preview_source(
            store=self.store,
            profile=self.profile,
            preview_slug="pr-42",
            copy_from=self.reference,
        )

    def test_only_selected_source_enters_normal_post_deploy_payload(self) -> None:
        _write_odoo_preview_template_runtime_environment(store=self.store)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {**self.runtime.env, "ODOO_DB_USER": "odoo"},
                }
            )
        )
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        before = self.store.list_secret_bindings(limit=None)
        result = _odoo_preview_service_environment(
            control_plane_root_path=self.root,
            record_store=self.store,
            profile=self.profile,
            plan=request.apply.dry_run_plan,
            manifest=request.apply.manifest,
            database_url=self.store.database_url,
            fishbowl_source=self.reference,
        )
        payload = json.loads(
            base64.b64decode(result.values[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY])
        )
        fields = {item["key"]: item["value"] for item in payload["config_parameters"]}
        self.assertEqual(fields["fishbowl.host"]["value"], "source.example.test")
        self.assertEqual(fields["fishbowl.user"]["value"], "readonly")
        self.assertEqual(fields["fishbowl.db"]["value"], "inventory")
        self.assertEqual(fields["fishbowl.password"]["source"], "secret_binding")
        self.assertNotIn("private-fixture-source-password", json.dumps(payload))
        self.assertFalse(any(key.startswith("repairshopr.") for key in fields))
        self.assertEqual(result.integration_allowances, (self.allowance,))
        self.assertEqual(
            result.values["ODOO_OVERRIDE_SECRET__CONFIG_PARAM__FISHBOWL__PASSWORD"],
            "private-fixture-source-password",
        )
        self.assertEqual(self.store.list_secret_bindings(limit=None), before)

    def test_production_unknown_and_other_product_routes_refuse_before_decryption(self) -> None:
        for reference, slug in (
            (self.reference.model_copy(update={"instance": "prod"}), "pr-42"),
            (self.reference.model_copy(update={"context": "other-product"}), "pr-42"),
            (self.reference, "prod"),
            (self.reference, "testing"),
        ):
            with (
                self.subTest(reference=reference, slug=slug),
                patch(
                    "control_plane.odoo_preview_import_source.copy_source_value",
                    side_effect=AssertionError("must not decrypt"),
                ),
                self.assertRaises(ValueError),
            ):
                resolve_fishbowl_preview_source(
                    store=self.store, profile=self.profile, preview_slug=slug, copy_from=reference
                )

    def test_unclassified_or_writable_class_and_non_read_only_reason_refuse(self) -> None:
        assert self.binding.sharing_reason is not None
        for updates in (
            {"declared_secret_class": "prod_only"},
            {"declared_secret_class": "testing"},
            {"declared_secret_class": None},
            {"sharing_reason": None},
            {"sharing_reason": self.binding.sharing_reason.model_copy(update={"kind": "pre_live"})},
        ):
            with self.subTest(updates=updates):
                self.store.write_secret_binding(self.binding.model_copy(update=updates))
                with patch(
                    "control_plane.odoo_preview_import_source.copy_source_value",
                    side_effect=AssertionError("must not decrypt"),
                ):
                    with self.assertRaises(ValueError):
                        self.resolve()

    def test_missing_or_pre_live_allowance_refuses(self) -> None:
        for allowances in ((), (self.allowance.model_copy(update={"kind": "pre_live"}),)):
            self.store.write_dokploy_target_record(
                self.target.model_copy(
                    update={
                        "policies": DokployTargetPolicies(integration_allowances=allowances),
                    }
                )
            )
            with self.assertRaises(ValueError):
                self.resolve()

    def test_rotated_source_refuses_and_setting_or_allowance_change_invalidates_plan(self) -> None:
        original = self.resolve()
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {
                        **self.runtime.env,
                        "ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__USER": "changed",
                    },
                }
            )
        )
        self.assertNotEqual(self.resolve().authority_sha256, original.authority_sha256)
        self.store.write_secret_record(
            self.record.model_copy(update={"current_version_id": "rotated"})
        )
        with self.assertRaises(ValueError):
            self.resolve()

    def test_incomplete_runtime_cannot_fall_back_to_database_or_override_literals(self) -> None:
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {"ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__HOST": "source.example.test"},
                }
            )
        )
        with self.assertRaises(ValueError):
            self.resolve()

    @scoped_agent_test
    async def test_preview_read_and_standing_metadata_read_cannot_move_source_value(self) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(request)
        payload: dict[str, object] = {
            "product": self.profile.product,
            "inputs": {
                **issued.plan_request.model_dump(mode="json"),
                "fishbowl_source": self.reference.model_dump(mode="json"),
            },
        }
        for explicit_read in (False, True):
            actions = ("odoo_preview_apply_inputs.read",)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    local_operators=(
                        *_local_operator_policy(actions=actions).local_operators,
                        *(
                            (
                                LocalOperatorPolicyRule(
                                    subjects=("local-owner-agent",),
                                    token_labels=("local-owner-write",),
                                    products=("launchplane",),
                                    contexts=("cm",),
                                    actions=("secret.read",),
                                    instances=("testing",),
                                ),
                            )
                            if explicit_read
                            else ()
                        ),
                        LocalOperatorPolicyRule(
                            subjects=("local-owner-agent",),
                            token_labels=("local-owner-write",),
                            products=(self.profile.product,),
                            contexts=("*",),
                            actions=("agent.read",),
                            instances=("*",),
                        ),
                    ),
                ),
                record_store_factory=lambda: self.store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="test-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )
            with patch(
                "control_plane.http_app.build_odoo_preview_apply_inputs_result",
                return_value=issued.model_dump(mode="json"),
            ) as build:
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/preview-apply-inputs",
                    payload=payload,
                    headers={
                        "Authorization": "Bearer test-operator-token",
                        "Idempotency-Key": f"source-authority-{explicit_read}",
                    },
                )
            self.assertEqual(response.status_code, 202 if explicit_read else 403, response.text)
            self.assertEqual(build.call_count, int(explicit_read))
