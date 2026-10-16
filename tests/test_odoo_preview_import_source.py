import base64
import click
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
from control_plane.odoo_instance_overrides import (
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    config_parameter_secret_env_key,
    build_post_deploy_environment,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.odoo_preview_apply_http import (
    _odoo_preview_service_environment,
    execute_odoo_preview_apply_result,
    _build_odoo_preview_apply_inputs,
    refresh_odoo_preview_issued_plan,
    OdooPreviewPlanProvenanceError,
    issue_odoo_preview_apply_plan,
)
from control_plane.odoo_preview_import_source import (
    FishbowlPreviewSource,
    FISHBOWL_OPTIONAL_DEFAULTS,
    preview_fishbowl_clear_program,
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
                        "binding_key": config_parameter_secret_env_key("fishbowl.password"),
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
                config_parameter_runtime_key("fishbowl.host"): "source.example.test",
                config_parameter_runtime_key("fishbowl.user"): "readonly",
                config_parameter_runtime_key("fishbowl.db"): "inventory",
                config_parameter_runtime_key("fishbowl.port"): 3306,
                config_parameter_runtime_key("repairshopr.sync_db.password"): "must-not-copy",
            },
        )
        self.store.write_runtime_environment_record(self.runtime)

    def resolve(self, *, include_secret_value: bool = False) -> FishbowlPreviewSource:
        return resolve_fishbowl_preview_source(
            store=self.store,
            profile=self.profile,
            preview_slug="pr-42",
            copy_from=self.reference,
            include_secret_value=include_secret_value,
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
            result.values[config_parameter_secret_env_key("fishbowl.password")],
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
                    store=self.store,
                    profile=self.profile,
                    preview_slug=slug,
                    copy_from=reference,
                    include_secret_value=True,
                )

    def test_qualified_preview_source_cannot_waive_another_payload_or_lane(self) -> None:
        source = self.resolve(include_secret_value=True)
        record = OdooInstanceOverrideRecord(
            context=source.context,
            instance=source.instance,
            config_parameters=source.parameters,
            updated_at=self.profile.updated_at,
        )
        self.assertTrue(
            build_post_deploy_environment(
                record, preview_import_source=source
            ).payload.config_parameters
        )
        password = source.parameters[-1]
        for changed in (
            record.model_copy(update={"instance": "prod"}),
            record.model_copy(update={"instance": "pr-43"}),
            record.model_copy(update={"context": "another-product"}),
            record.model_copy(
                update={
                    "config_parameters": (
                        *source.parameters[:-1],
                        password.model_copy(
                            update={
                                "value": OdooOverrideValue(
                                    source="secret_binding", secret_binding_id="wrong"
                                )
                            }
                        ),
                    )
                }
            ),
        ):
            with self.subTest(instance=changed.instance), self.assertRaises(click.ClickException):
                build_post_deploy_environment(changed, preview_import_source=source)

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
                        self.resolve(include_secret_value=True)

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
        changed_allowance = self.allowance.model_copy(
            update={"evidence": "Fresh SELECT-only proof"}
        )
        self.store.write_dokploy_target_record(
            self.target.model_copy(
                update={
                    "policies": DokployTargetPolicies(integration_allowances=(changed_allowance,)),
                }
            )
        )
        self.assertNotEqual(self.resolve().authority_sha256, original.authority_sha256)
        self.store.write_dokploy_target_record(self.target)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {
                        **self.runtime.env,
                        config_parameter_runtime_key("fishbowl.user"): "changed",
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
                    "env": {config_parameter_runtime_key("fishbowl.host"): "source.example.test"},
                }
            )
        )
        with self.assertRaises(ValueError):
            self.resolve()

    @scoped_agent_test
    async def test_preview_read_and_standing_metadata_read_cannot_move_source_value(self) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(request)
        issued = issued.model_copy(
            update={
                "plan_request": issued.plan_request.model_copy(
                    update={"fishbowl_source": self.reference}
                ),
                "plan_provenance": None,
            }
        )
        payload: dict[str, object] = {
            "product": self.profile.product,
            "inputs": {
                **issued.plan_request.model_dump(mode="json"),
                "fishbowl_source": self.reference.model_dump(mode="json"),
            },
        }
        for explicit_read in (False, True):
            actions = ("odoo_preview_apply_inputs.read", "odoo_preview_apply.execute")
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
            if not explicit_read:
                denied_app = app
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

        plan_id = response.json()["result"]["plan_provenance"]["plan_id"]
        with patch("control_plane.http_app.prepare_odoo_preview_apply_operation") as prepare:
            refused = await _asgi_request(
                denied_app,
                "POST",
                "/v1/drivers/odoo/preview-apply",
                payload=_refresh_payload(),
                headers={"Authorization": "Bearer test-operator-token", "Idempotency-Key": plan_id},
            )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "authorization_denied")
        prepare.assert_not_called()

    def test_context_values_instance_override_and_target_delivery_precedence(self) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="cm",
                updated_at=self.profile.updated_at,
                env=self.runtime.env,
            )
        )
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {config_parameter_runtime_key("fishbowl.user"): "lane-reader"},
                }
            )
        )
        self.store.write_dokploy_target_record(
            self.target.model_copy(
                update={
                    "env": {
                        config_parameter_runtime_key("fishbowl.host"): "delivered.example.test"
                    },
                }
            )
        )
        parameters = {item.key: item.value.value for item in self.resolve().parameters}
        self.assertEqual(parameters["fishbowl.host"], "delivered.example.test")
        self.assertEqual(parameters["fishbowl.db"], "inventory")
        self.assertEqual(parameters["fishbowl.user"], "lane-reader")

    def test_secret_owned_non_secret_parameter_refuses_stale_plain_value(self) -> None:
        self.store.write_secret_binding(
            self.binding.model_copy(
                update={
                    "binding_id": "source-host-binding",
                    "binding_key": config_parameter_runtime_key("fishbowl.host"),
                }
            )
        )
        with self.assertRaises(ValueError):
            self.resolve()

    def test_plan_never_decrypts_and_rotated_source_becomes_stale_before_apply(self) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        planned = _issued_plan(request)
        inputs = planned.plan_request.model_copy(update={"fishbowl_source": self.reference})
        planned = planned.model_copy(update={"plan_request": inputs, "plan_provenance": None})
        with (
            patch(
                "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                return_value=planned,
            ),
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=AssertionError("planning must not decrypt"),
            ),
        ):
            result = _build_odoo_preview_apply_inputs(
                control_plane_root=self.root,
                record_store=self.store,
                profile=self.profile,
                request=inputs,
                database_url=self.store.database_url,
            )
        self.assertTrue(result.fishbowl_source_sha256)
        self.assertNotIn("private-fixture-source-password", result.model_dump_json())
        issued = issue_odoo_preview_apply_plan(result=result, plan_id="source-plan")
        self.store.write_secret_record(
            self.record.model_copy(update={"current_version_id": "rotated"})
        )
        with (
            patch(
                "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                return_value=planned,
            ),
            self.assertRaises(OdooPreviewPlanProvenanceError) as refused,
        ):
            refresh_odoo_preview_issued_plan(
                control_plane_root=self.root,
                record_store=self.store,
                profile=self.profile,
                request=request,
                issued_plan=issued,
                database_url=self.store.database_url,
            )
        self.assertEqual(refused.exception.code, "odoo_preview_plan_stale")

    def test_credential_shaped_non_secret_settings_are_refused(self) -> None:
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {
                        **self.runtime.env,
                        config_parameter_runtime_key(
                            "fishbowl.host"
                        ): "https://user:private@source.example.test",
                    },
                }
            )
        )
        with self.assertRaises(ValueError):
            self.resolve()

    def test_absent_optional_source_settings_clear_previous_values(self) -> None:
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {
                        key: value
                        for key, value in self.runtime.env.items()
                        if key != config_parameter_runtime_key("fishbowl.port")
                    },
                }
            )
        )
        parameters = {item.key: item.value.value for item in self.resolve().parameters}
        self.assertEqual(parameters["fishbowl.port"], FISHBOWL_OPTIONAL_DEFAULTS["port"])
        self.assertEqual(
            parameters["fishbowl.ssl_verify"], FISHBOWL_OPTIONAL_DEFAULTS["ssl_verify"]
        )

    def test_authority_change_during_environment_resolution_refuses_before_provider(self) -> None:
        _write_odoo_preview_template_runtime_environment(store=self.store)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {**self.runtime.env, "ODOO_DB_USER": "odoo"},
                }
            )
        )
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(request)
        issued = issued.model_copy(
            update={
                "plan_request": issued.plan_request.model_copy(
                    update={"fishbowl_source": self.reference}
                ),
                "fishbowl_source_sha256": self.resolve().authority_sha256,
            }
        )
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {
                        **self.runtime.env,
                        "ODOO_DB_USER": "odoo",
                        config_parameter_runtime_key("fishbowl.host"): "changed.example.test",
                    },
                }
            )
        )
        with (
            patch(
                "control_plane.odoo_preview_apply_http.refresh_odoo_preview_issued_plan",
                return_value=request,
            ),
            patch(
                "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply"
            ) as provider,
            self.assertRaises(OdooPreviewPlanProvenanceError) as refused,
        ):
            execute_odoo_preview_apply_result(
                control_plane_root_path=self.root,
                record_store=self.store,
                profile=self.profile,
                request=request,
                issued_plan=issued,
                database_url=self.store.database_url,
                deployment_record_id="fixture",
            )
        self.assertEqual(refused.exception.code, "odoo_preview_plan_stale")
        provider.assert_not_called()

    def test_source_cleanup_refuses_production_and_testing_before_database_access(self) -> None:
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        from control_plane.dokploy.post_deploy import run_compose_post_deploy_update
        from control_plane.dokploy.source import DokployTargetDefinition

        connection = Mock(side_effect=AssertionError("must not connect"))
        for instance in ("prod", "testing", ""):
            with (
                self.subTest(instance=instance),
                patch.dict(os.environ, {"PLATFORM_INSTANCE": instance}),
                patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=connection)}),
                self.assertRaises(RuntimeError),
            ):
                exec(preview_fishbowl_clear_program(), {})
        connection.assert_not_called()
        with patch(
            "control_plane.dokploy.post_deploy.api.fetch_dokploy_target_payload"
        ) as provider:
            with self.assertRaises(ValueError):
                run_compose_post_deploy_update(
                    host="https://provider.example.test",
                    token="fixture",
                    env_file=None,
                    target_definition=DokployTargetDefinition(context="example", instance="prod"),
                    clear_preview_fishbowl_source=True,
                )
            provider.assert_not_called()
