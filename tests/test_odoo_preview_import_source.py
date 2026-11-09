import base64
import click
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any, cast
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
from control_plane.odoo_preview_import_source import PreviewImportSourceError
from control_plane import secrets
from control_plane.odoo_runtime_source_prerequisites import (
    PAYLOAD_ADDON,
    PAYLOAD_FILES,
    SOURCE_ADDONS,
)
from control_plane.build_provenance import BuildProvenanceError, VerifiedBuildArtifact
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.artifact_release_compatibility import ReleaseInputFile
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
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult
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
from tests.test_release_compatibility import artifact
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
                config_parameter_runtime_key("fishbowl.ssl_verify"): "True",
                config_parameter_runtime_key("repairshopr.sync_db.password"): "must-not-copy",
            },
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.manifest = self.recorded_manifest("preview-artifact")
        self.built = self.manifest
        proof = patch(
            "control_plane.product_reconcile.verified_preview_build_artifact",
            side_effect=self.build_run_proof,
        )
        self.proof = proof.start()
        self.addCleanup(proof.stop)

    def build_run_proof(
        self,
        _store: object,
        _profile: LaunchplaneProductProfileRecord,
        *,
        pull_request_number: int,
        source_git_ref: str,
    ) -> VerifiedBuildArtifact:
        built = self.built.source_build
        assert built is not None
        if (pull_request_number, source_git_ref) != (
            built.pull_request_number,
            self.built.source_commit,
        ):
            raise BuildProvenanceError("No successful Build run.", code="build_run_missing")
        return VerifiedBuildArtifact(manifest=self.built)

    def recorded_manifest(
        self,
        artifact_id: str,
        *,
        producer: bool = True,
        payload: bool = True,
        repository: str = "",
        complete: bool = True,
        pull_request: int | None = 42,
    ) -> ArtifactIdentityManifest:
        addon, model, client = SOURCE_ADDONS["fishbowl"]
        files = (
            (
                "__init__.py",
                "__manifest__.py",
                "models/__init__.py",
                f"models/{model}.py",
                "services/__init__.py",
                f"services/{client}.py",
                "services/connection_settings.py",
            )
            if producer
            else ()
        )
        manifest = artifact(
            artifact_id,
            files=tuple(
                ReleaseInputFile(path=f"addons/{addon}/{path}", kind="unknown", sha256="a" * 64)
                for path in files
            ),
            shared_files=tuple(
                ReleaseInputFile(path=f"{PAYLOAD_ADDON}/{path}", kind="unknown", sha256="b" * 64)
                for path in (PAYLOAD_FILES if payload else ("unrelated.py",))
            ),
        )
        assert manifest.source_build is not None
        assert manifest.release_compatibility is not None
        source_repository = repository or self.profile.repository
        manifest.source_build = manifest.source_build.model_copy(
            update={
                "repository": source_repository,
                "event": "push" if pull_request is None else "pull_request",
                "purpose": "release" if pull_request is None else "preview",
                "pull_request_number": pull_request,
            }
        )
        manifest.release_compatibility = manifest.release_compatibility.model_copy(
            update={
                "complete": complete,
                "sources": tuple(
                    source.model_copy(update={"repository": source_repository})
                    if source.input_name == "tenant"
                    else source
                    for source in manifest.release_compatibility.sources
                ),
            }
        )
        if pull_request is None:
            self.store.write_artifact_manifest(manifest)
        return manifest

    def envelope(self) -> OdooPreviewApplyEnvelope:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        image = self.manifest.image
        return request.model_copy(
            update={
                "apply": request.apply.model_copy(
                    update={
                        "manifest": self.manifest,
                        "image_reference": f"{image.repository}@{image.digest}",
                    }
                )
            }
        )

    def plan(self) -> OdooPreviewApplyInputsResult:
        return self.planned_with(_issued_plan(self.envelope()))

    def planned_with(self, planned: OdooPreviewApplyInputsResult) -> OdooPreviewApplyInputsResult:
        inputs = planned.plan_request.model_copy(
            update={"fishbowl_source": self.reference, "manifest": self.manifest}
        )
        return planned.model_copy(update={"plan_request": inputs, "plan_provenance": None})

    def write_setting(self, field: str, value: str | int | None) -> None:
        env = dict(self.runtime.env)
        key = config_parameter_runtime_key(f"fishbowl.{field}")
        env.pop(key, None)
        if value is not None:
            env[key] = value
        self.runtime = self.runtime.model_copy(update={"env": env})
        self.store.write_runtime_environment_record(self.runtime)

    def write_override(self, key: str, value: OdooOverrideValue) -> None:
        self.store.write_odoo_instance_override_record(
            OdooInstanceOverrideRecord(
                context="cm",
                instance="testing",
                config_parameters=(OdooConfigParameterOverride(key=key, value=value),),
                updated_at=self.profile.updated_at,
            )
        )

    def test_unavailable_stable_testing_consumer_does_not_stop_the_preview(self) -> None:
        planned = self.plan()
        refused = AssertionError("Stable testing must not be read for an independent preview")
        with (
            patch(
                "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                return_value=planned,
            ),
            patch("control_plane.odoo_runtime_reads.select_stable_runtime", side_effect=refused),
            patch("control_plane.odoo_runtime_reads.connect_runtime", side_effect=refused),
        ):
            result = _build_odoo_preview_apply_inputs(
                control_plane_root=self.root,
                record_store=self.store,
                profile=self.profile,
                request=planned.plan_request,
                database_url=self.store.database_url,
            )
        self.assertTrue(result.fishbowl_source_sha256)

    def test_source_capture_does_not_resolve_import_secret_before_reservation(self) -> None:
        from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation

        _write_odoo_preview_template_runtime_environment(store=self.store)
        self.profile.preview.data_transport_mode = "driver"
        request = self.envelope()
        issued = _issued_plan(request)
        issued.plan_request.fishbowl_source = self.reference
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=request,
            issued_plan=issued,
            reservation_scope="source-capture",
            idempotency_key="source-capture",
            request_fingerprint="source-capture",
            trace_id="source-capture",
        )
        with patch(
            "control_plane.odoo_preview_apply_http.resolve_fishbowl_preview_source"
        ) as deliver:
            operation.adapter.capture_source_decision()
        deliver.assert_not_called()
        assert operation.adapter.source_decision is not None
        self.assertTrue(operation.adapter.source_decision.fishbowl_import)
        self.assertIsNone(
            self.store.read_idempotency_record_by_id(operation.adapter._deployment_record_id)
        )

    def resolve(self, *, include_secret_value: bool = False) -> FishbowlPreviewSource:
        return resolve_fishbowl_preview_source(
            store=self.store,
            profile=self.profile,
            preview_slug="pr-42",
            copy_from=self.reference,
            manifest=self.manifest,
            include_secret_value=include_secret_value,
        )

    def test_exact_preview_withdrawal_also_controls_import_source_composition(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooWebsiteBootstrapPayload,
        )
        from control_plane.odoo_instance_overrides import (
            LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
        )

        _write_odoo_preview_template_runtime_environment(store=self.store)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={
                    "env": {**self.runtime.env, "ODOO_DB_USER": "odoo"},
                }
            )
        )
        request = self.envelope()
        template = OdooInstanceOverrideRecord(
            context=self.profile.preview.context,
            instance=self.profile.preview.template_instance,
            website_bootstrap=OdooWebsiteBootstrapPayload(name="Template site"),
            updated_at=self.profile.updated_at,
        )
        self.store.write_odoo_instance_override_record(template)
        self.store.write_odoo_instance_override_record(
            OdooInstanceOverrideRecord(
                context=self.profile.preview.context,
                instance=request.apply.dry_run_plan.preview_slug,
                apply_on=("preview",),
                updated_at=self.profile.updated_at,
            )
        )
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
        self.assertFalse(payload.get("website_bootstrap"))
        self.assertTrue(payload["config_parameters"])
        self.assertNotIn(LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY, result.values)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=template.context,
                instance_name=template.instance,
            ),
            template,
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
        request = self.envelope()
        addon_settings = [
            {
                "addon": "authentik_sso",
                "setting": "client_id",
                "value": {"source": "literal", "value": "preview-client-fixture"},
            }
        ]
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.profile.preview.context,
                instance="preview",
                env={
                    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: base64.b64encode(
                        json.dumps(
                            {
                                "context": self.profile.preview.context,
                                "instance": "preview",
                                "addon_settings": addon_settings,
                            }
                        ).encode()
                    ).decode()
                },
                updated_at="2026-10-26T00:00:00Z",
            )
        )
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
        self.assertEqual(payload["addon_settings"], addon_settings)
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
                    manifest=self.manifest,
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
        self.write_setting("port", 3499)
        self.assertNotEqual(self.resolve().authority_sha256, original.authority_sha256)
        self.write_setting("port", 3306)
        self.manifest = self.built = self.recorded_manifest("another-preview-artifact")
        self.assertNotEqual(self.resolve().authority_sha256, original.authority_sha256)
        self.store.write_secret_record(
            self.record.model_copy(update={"current_version_id": "rotated"})
        )
        with self.assertRaises(ValueError):
            self.resolve()

    def assert_unqualified(self, stage: str) -> None:
        with self.assertRaises(PreviewImportSourceError) as caught:
            self.resolve(include_secret_value=True)
        self.assertEqual(caught.exception.code, "source_unqualified")
        self.assertEqual(caught.exception.failure_stage, stage)
        self.assertIn(stage, str(caught.exception))
        self.assertNotIn("source.example.test", str(caught.exception))
        self.assertNotIn("private-fixture-source-password", str(caught.exception))

    def test_pull_request_build_qualifies_by_its_build_run_not_the_release_store(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertIn("fishbowl.password", {item.key for item in self.resolve().parameters})
        self.assertEqual(
            self.proof.call_args.kwargs,
            {"pull_request_number": 42, "source_git_ref": self.manifest.source_commit},
        )

    def test_only_the_exact_proven_build_of_this_product_qualifies(self) -> None:
        proven = self.manifest
        changed = proven.model_copy(update={"source_commit": "c" * 40})
        foreign = self.recorded_manifest("foreign-artifact", repository="another/product")
        other_attempt = proven.model_copy(update={"artifact_id": "another-run-attempt"})
        release = self.recorded_manifest("release-artifact", pull_request=None)
        unrecorded_release = release.model_copy(update={"artifact_id": "never-recorded"})
        for name, manifest in (
            ("missing", None),
            ("another_run_attempt", other_attempt),
            ("changed_after_build", changed),
            ("another_product", foreign),
            ("another_pull_request", self.recorded_manifest("other-pr", pull_request=43)),
            ("unrecorded_release", unrecorded_release),
        ):
            with self.subTest(name=name), patch.object(self, "manifest", manifest):
                self.assert_unqualified("artifact")
        self.manifest = release
        self.assertIn("fishbowl.password", {item.key for item in self.resolve().parameters})

    def test_preview_artifact_must_carry_the_source_and_payload_producers(self) -> None:
        for name, manifest in (
            ("no_importer", self.recorded_manifest("no-importer", producer=False)),
            ("no_payload_producer", self.recorded_manifest("no-payload", payload=False)),
            ("incomplete_inventory", self.recorded_manifest("incomplete", complete=False)),
        ):
            with self.subTest(name=name):
                self.manifest = self.built = manifest
                self.assert_unqualified("producer_inventory")

    def test_unknown_or_unsafe_effective_inputs_refuse_without_a_default(self) -> None:
        for field, value in (
            ("host", None),
            ("user", None),
            ("db", None),
            ("port", None),
            ("port", "not-a-port"),
            ("port", "70000"),
            ("ssl_verify", None),
            ("ssl_verify", "flase"),
        ):
            with self.subTest(field=field, value=value):
                previous = self.runtime
                self.write_setting(field, value)
                self.assert_unqualified("source_resolution")
                self.runtime = previous
                self.store.write_runtime_environment_record(previous)

    def test_conflicting_managed_authority_refuses(self) -> None:
        literal = OdooOverrideValue(source="literal", value="other.example.test")
        secret = OdooOverrideValue(source="secret_binding", secret_binding_id="wrong")
        for key, value, stage in (
            ("fishbowl.host", literal, "source_resolution"),
            (
                "fishbowl.host",
                OdooOverrideValue(source="literal", value=""),
                "source_resolution",
            ),
            ("fishbowl.user", secret, "source_resolution"),
            ("fishbowl.password", secret, "authority"),
            (
                "fishbowl.password",
                OdooOverrideValue(source="literal", value="not-the-binding"),
                "authority",
            ),
        ):
            with self.subTest(key=key, source=value.source):
                self.write_override(key, value)
                self.assert_unqualified(stage)

    def test_typed_setting_supplies_only_what_the_lane_record_omits(self) -> None:
        self.write_setting("db", None)
        self.write_override(
            "fishbowl.db", OdooOverrideValue(source="literal", value="typed-inventory")
        )
        parameters = {item.key: item.value.value for item in self.resolve().parameters}
        self.assertEqual(parameters["fishbowl.db"], "typed-inventory")

    def test_source_verification_off_stays_only_on_its_explicit_setting(self) -> None:
        self.write_setting("ssl_verify", "false")
        parameters = {item.key: item.value.value for item in self.resolve().parameters}
        self.assertEqual(parameters["fishbowl.ssl_verify"], "False")

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

    def test_plan_never_copies_password_and_rotated_source_becomes_stale_before_apply(self) -> None:
        request = self.envelope()
        planned = _issued_plan(request)
        inputs = planned.plan_request.model_copy(update={"fishbowl_source": self.reference})
        planned = planned.model_copy(update={"plan_request": inputs, "plan_provenance": None})
        with (
            patch(
                "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                return_value=planned,
            ),
            patch(
                "control_plane.odoo_preview_import_source.copy_source_value",
                side_effect=AssertionError("planning must not copy source passwords"),
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

    def test_plan_without_a_manifest_takes_the_preview_head_build_run_manifest(self) -> None:
        planned = self.plan()
        image = self.manifest.image
        inputs = planned.plan_request.model_copy(
            update={
                "manifest": None,
                "pr_number": 42,
                "source_git_ref": self.manifest.source_commit,
                "image_reference": f"{image.repository}@{image.digest}",
            }
        )

        def plan_with(request: object) -> object:
            with patch(
                "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                return_value=planned,
            ) as build:
                _build_odoo_preview_apply_inputs(
                    control_plane_root=self.root,
                    record_store=self.store,
                    profile=self.profile,
                    request=cast(Any, request),
                    database_url=self.store.database_url,
                )
            return build.call_args.kwargs["request"]

        self.assertEqual(cast(Any, plan_with(inputs)).manifest, self.manifest)
        for name, update in (
            ("another_image", {"image_reference": f"{image.repository}@sha256:{'e' * 64}"}),
            ("another_head", {"source_git_ref": "c" * 40}),
            ("no_head", {"source_git_ref": ""}),
        ):
            with self.subTest(name=name), self.assertRaises(PreviewImportSourceError) as caught:
                plan_with(inputs.model_copy(update=update))
            self.assertEqual(caught.exception.failure_stage, "artifact")

    def test_credential_shaped_non_secret_settings_are_refused(self) -> None:
        self.write_setting("host", "https://user:private@source.example.test")
        with self.assertRaises(ValueError):
            self.resolve()

    def test_effective_source_options_are_copied_without_local_defaults(self) -> None:
        self.write_setting("port", 3499)
        parameters = {item.key: item.value.value for item in self.resolve().parameters}
        self.assertEqual(parameters["fishbowl.port"], "3499")

    def test_authority_change_during_environment_resolution_refuses_before_provider(self) -> None:
        _write_odoo_preview_template_runtime_environment(store=self.store)
        self.runtime = self.runtime.model_copy(
            update={"env": {**self.runtime.env, "ODOO_DB_USER": "odoo"}}
        )
        self.store.write_runtime_environment_record(self.runtime)
        request = self.envelope()
        issued = _issued_plan(request)
        issued = issued.model_copy(
            update={
                "plan_request": issued.plan_request.model_copy(
                    update={"fishbowl_source": self.reference}
                ),
                "fishbowl_source_sha256": self.resolve().authority_sha256,
            }
        )
        self.write_setting("port", 3499)
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
