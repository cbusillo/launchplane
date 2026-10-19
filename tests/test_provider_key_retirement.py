import os
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import Mock, patch

from click.testing import CliRunner
from click import Command

from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import COMPOSE_IMAGE_REFERENCE_ENV_KEYS
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.live_target_runtime import (
    LiveTargetRuntimeError,
    apply_live_target_runtime_environment,
    validate_provider_key_retirement,
)
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.product_onboarding import build_runtime_environment_records
from tests.http_app_test_support import _post_product_config_apply, _RejectingVerifier
from tests.support.auth import _local_operator_policy
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url

CLI_MAIN = cast(Command, main)


def _profile() -> LaunchplaneProductProfileRecord:
    payload = _generic_site_profile_payload()
    payload["expected_config"] = {
        "runtime_environment_keys": [
            {"key": "APP_MODE", "context": "example-site", "instance": "testing"}
        ]
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _runtime_record(*, retired: tuple[str, ...] = ()) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        schema_version=2 if retired else 1,
        scope="instance",
        context="example-site",
        instance="testing",
        env={"APP_MODE": "private-mode-value"},
        retired_provider_keys=retired,
        updated_at="2026-09-27T00:00:00Z",
        source_label="test",
    )


class TestingOverridePointerRetirementTests(unittest.TestCase):
    def test_current_consumers_protect_pointers_without_generation_or_secret_resolution(
        self,
    ) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooAddonSettingOverride,
            OdooConfigParameterOverride,
            OdooInstanceOverrideRecord,
            OdooOverrideValue,
            OdooOverrideApplyPhase,
        )
        from control_plane.odoo_instance_overrides import (
            addon_setting_secret_env_key,
            config_parameter_secret_env_key,
        )

        cases: tuple[str, ...] = (
            "unused",
            "runtime_declaration",
            "secret_declaration",
            "context",
            "instance",
            "target",
            "other_lane",
            "wrong_product",
            "production",
            "ambiguous_owner",
            "inferred_owner",
            "missing_owner",
        )
        cases += tuple(
            f"{kind}_{phase}"
            for kind in ("config", "addon")
            for phase in ("restore", "deploy", "promotion")
        )
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary_directory:
                database_url = _sqlite_database_url(Path(temporary_directory) / "records.sqlite3")
                store = PostgresRecordStore(database_url=database_url)
                self.addCleanup(store.close)
                store.ensure_schema()
                key = (
                    config_parameter_secret_env_key("private.fixture")
                    if case.startswith("config_")
                    else addon_setting_secret_env_key(addon_name="fixture", setting_name="token")
                )
                profile = _profile().model_dump(mode="json")
                if case.endswith("declaration"):
                    name, field = (
                        ("runtime_environment_keys", "key")
                        if case == "runtime_declaration"
                        else ("managed_secret_bindings", "binding_key")
                    )
                    profile["expected_config"][name] = [
                        {field: key, "context": "example-site", "instance": "testing"}
                    ]
                if case != "missing_owner":
                    store.write_product_profile_record(
                        LaunchplaneProductProfileRecord.model_validate(profile)
                    )
                if case == "ambiguous_owner":
                    store.write_product_profile_record(
                        _profile().model_copy(update={"product": "other-product"})
                    )
                if case in {"context", "instance", "other_lane"}:
                    store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            scope="context" if case == "context" else "instance",
                            context="example-site",
                            instance=""
                            if case == "context"
                            else ("preview" if case == "other_lane" else "testing"),
                            env={key: "private-value"},
                            updated_at=_runtime_record().updated_at,
                            source_label="test",
                        )
                    )
                if case == "target":
                    _seed_tracked_target_records(
                        database_url=database_url,
                        context="example-site",
                        instance="testing",
                        target_id="fixture-app",
                        target_type="application",
                        target_name="fixture-app",
                    )
                    target = store.read_dokploy_target_record(
                        context_name="example-site", instance_name="testing"
                    )
                    store.write_dokploy_target_record(
                        target.model_copy(update={"env": {key: "private-value"}})
                    )
                if case.startswith(("config_", "addon_")):
                    value = OdooOverrideValue(
                        source="secret_binding", secret_binding_id="fixture-binding"
                    )
                    store.write_odoo_instance_override_record(
                        OdooInstanceOverrideRecord(
                            context="example-site",
                            instance="testing",
                            apply_on=(cast(OdooOverrideApplyPhase, case.split("_")[1]),),
                            updated_at=_runtime_record().updated_at,
                            config_parameters=(
                                OdooConfigParameterOverride(key="private.fixture", value=value),
                            )
                            if case.startswith("config_")
                            else (),
                            addon_settings=(
                                OdooAddonSettingOverride(
                                    addon="fixture", setting="token", value=value
                                ),
                            )
                            if case.startswith("addon_")
                            else (),
                        )
                    )

                def validate() -> None:
                    validate_provider_key_retirement(
                        retired_keys={key},
                        application_keys=set(),
                        record_store=store,
                        product_name="other-product"
                        if case == "wrong_product"
                        else (
                            "" if case in {"inferred_owner", "ambiguous_owner"} else "example-site"
                        ),
                        context_name="example-site",
                        instance_name="prod" if case == "production" else "testing",
                    )

                if case in {"unused", "other_lane", "inferred_owner"}:
                    validate()
                else:
                    with self.assertRaises(LiveTargetRuntimeError) as caught:
                        validate()
                    self.assertEqual(caught.exception.code, "runtime_retirement_conflict")


class ProviderKeyRetirementApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_url = _sqlite_database_url(
            Path(self.temporary_directory.name) / "records.sqlite3"
        )
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_product_profile_record(_profile())
        self.store.write_runtime_environment_record(_runtime_record())
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan", "product_config.apply"),
                products=("example-site",),
                contexts=("example-site",),
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    @staticmethod
    def _payload(*, mode: str = "dry-run", key: str = "LEGACY_PASSWORD") -> dict[str, object]:
        return {
            "schema_version": 2,
            "mode": mode,
            "product": "example-site",
            "context": "example-site",
            "instance": "testing",
            "reason": "Retire the reviewed obsolete provider key.",
            "runtime_env": {"retired_provider_keys": [key]},
        }

    async def test_unused_pointers_allow_config_edits_but_protect_every_override_phase(
        self,
    ) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooConfigParameterOverride,
            OdooInstanceOverrideRecord,
            OdooOverrideValue,
        )
        from control_plane.odoo_instance_overrides import config_parameter_secret_env_key

        key = config_parameter_secret_env_key("private.fixture")
        self.store.write_runtime_environment_record(_runtime_record(retired=(key,)))
        payload = self._payload()
        payload["runtime_env"] = {"env": {"APP_MODE": "new-private-mode"}}
        for phase in (None, "restore", "deploy", "promotion"):
            with self.subTest(phase=phase):
                if phase is not None:
                    self.store.write_odoo_instance_override_record(
                        OdooInstanceOverrideRecord(
                            context="example-site",
                            instance="testing",
                            apply_on=(phase,),
                            updated_at=_runtime_record().updated_at,
                            config_parameters=(
                                OdooConfigParameterOverride(
                                    key="private.fixture",
                                    value=OdooOverrideValue(
                                        source="secret_binding", secret_binding_id="fixture-binding"
                                    ),
                                ),
                            ),
                        )
                    )
                response = await _post_product_config_apply(
                    self.app, payload, authorization="Bearer local-operator-token"
                )
                if phase is not None:
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "runtime_retirement_conflict"
                    )
                else:
                    self.assertEqual(response.status_code, 202, response.text)
                    applied = await _post_product_config_apply(
                        self.app,
                        {**payload, "mode": "apply"},
                        authorization="Bearer local-operator-token",
                        idempotency_key="qualified-edit",
                    )
                    self.assertEqual(applied.status_code, 202, applied.text)
                    self.assertIn(
                        key, self.store.list_runtime_environment_records()[0].retired_provider_keys
                    )

    async def test_incomplete_generation_does_not_block_unrelated_config_edit(self) -> None:
        from tests.test_odoo_testing_restore import generation

        key = "ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN"
        self.store.write_runtime_environment_record(_runtime_record(retired=(key,)))
        self.store.write_fresh_odoo_testing_generation(
            generation().model_copy(update={"product": "example-site", "context": "example-site"})
        )
        payload = self._payload()
        payload["runtime_env"] = {"env": {"APP_MODE": "new-private-mode"}}
        response = await _post_product_config_apply(
            self.app, payload, authorization="Bearer local-operator-token"
        )
        self.assertEqual(response.status_code, 202, response.text)

    async def test_retirement_requires_exact_review_and_replays_persisted_intent(self) -> None:
        apply_payload = self._payload(mode="apply")
        missing_review = await _post_product_config_apply(
            self.app,
            apply_payload,
            authorization="Bearer local-operator-token",
            idempotency_key="unreviewed",
        )
        self.assertEqual(missing_review.status_code, 409)
        self.assertEqual(missing_review.json()["error"]["code"], "matching_dry_run_required")
        review = await _post_product_config_apply(
            self.app,
            self._payload(),
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(review.status_code, 202, review.text)
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        summary = review.json()["result"]["runtime_environment"]
        self.assertEqual(summary["retired_provider_keys_before"], [])
        self.assertEqual(summary["retired_provider_keys_after"], ["LEGACY_PASSWORD"])
        different_request = await _post_product_config_apply(
            self.app,
            self._payload(mode="apply", key="UNREVIEWED_KEY"),
            authorization="Bearer local-operator-token",
            idempotency_key="different",
        )
        self.assertEqual(different_request.status_code, 409)
        applied = await _post_product_config_apply(
            self.app,
            apply_payload,
            authorization="Bearer local-operator-token",
            idempotency_key="reviewed",
        )
        replay = await _post_product_config_apply(
            self.app,
            apply_payload,
            authorization="Bearer local-operator-token",
            idempotency_key="reviewed",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        with closing(PostgresRecordStore(database_url=self.database_url)) as independent_store:
            records = independent_store.list_runtime_environment_records()
        self.assertEqual(records[0].retired_provider_keys, ("LEGACY_PASSWORD",))
        self.assertEqual(records[0].env, _runtime_record().env)
        self.assertNotIn("private-mode-value", review.text + applied.text + replay.text)

    async def test_retirement_rejects_application_driver_and_wrong_lane_keys(self) -> None:
        for key in (
            "APP_MODE",
            "ODOO_DB_PASSWORD",
            "ODOO_DATA_VOLUME",
            "PLATFORM_INSTANCE",
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64",
            "LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED",
            "LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED",
        ):
            with self.subTest(key=key):
                response = await _post_product_config_apply(
                    self.app,
                    self._payload(key=key),
                    authorization="Bearer local-operator-token",
                )
                self.assertEqual(response.status_code, 400, response.text)
        wrong_lane = self._payload()
        wrong_lane["instance"] = "unknown"
        wrong_lane["runtime_env"] = {
            "env": {"APP_MODE": "value"},
            "retired_provider_keys": ["LEGACY_PASSWORD"],
        }
        response = await _post_product_config_apply(
            self.app,
            wrong_lane,
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "product_config_lane_not_owned")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_lane_without_declarations_can_retire_a_platform_credential(self) -> None:
        undeclared_profile = _profile().model_copy(
            update={
                "expected_config": _profile().expected_config.model_copy(
                    update={"runtime_environment_keys": ()}
                )
            }
        )
        self.store.write_product_profile_record(undeclared_profile)

        review = await _post_product_config_apply(
            self.app,
            self._payload(key="GITHUB_TOKEN"),
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(review.status_code, 202, review.text)
        self.assertEqual(
            review.json()["result"]["runtime_environment"]["retired_provider_keys_after"],
            ["GITHUB_TOKEN"],
        )
        driver_key = await _post_product_config_apply(
            self.app,
            self._payload(key="ODOO_DB_PASSWORD"),
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(driver_key.status_code, 400, driver_key.text)
        self.assertEqual(driver_key.json()["error"]["code"], "runtime_retirement_conflict")

    async def test_compose_driver_image_inputs_cannot_be_retired(self) -> None:
        for key in COMPOSE_IMAGE_REFERENCE_ENV_KEYS:
            with self.subTest(key=key):
                response = await _post_product_config_apply(
                    self.app,
                    self._payload(key=key),
                    authorization="Bearer local-operator-token",
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "runtime_retirement_conflict")

    async def test_concurrent_retirement_edit_is_not_lost(self) -> None:
        review = await _post_product_config_apply(
            self.app,
            self._payload(),
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(review.status_code, 202, review.text)
        original_write = self.store.write_product_authority_bundle
        competing_record = _runtime_record(retired=("OTHER_LEGACY_KEY",))

        def concurrent_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_runtime_environment_record(competing_record)
            original_write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=concurrent_write
        ):
            applied = await _post_product_config_apply(
                self.app,
                self._payload(mode="apply"),
                authorization="Bearer local-operator-token",
                idempotency_key="concurrent",
            )
        self.assertEqual(applied.status_code, 409, applied.text)
        self.assertEqual(applied.json()["error"]["code"], "runtime_environment_conflict")
        self.assertEqual(self.store.list_runtime_environment_records(), (competing_record,))

    async def test_concurrent_profile_change_returns_conflict_without_writing_retirement(
        self,
    ) -> None:
        review = await _post_product_config_apply(
            self.app,
            self._payload(),
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(review.status_code, 202, review.text)
        original_write = self.store.write_product_authority_bundle

        def concurrent_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_product_profile_record(
                _profile().model_copy(update={"display_name": "Changed profile"})
            )
            original_write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=concurrent_write
        ):
            applied = await _post_product_config_apply(
                self.app,
                self._payload(mode="apply"),
                authorization="Bearer local-operator-token",
                idempotency_key="profile-race",
            )
        self.assertEqual(applied.status_code, 409, applied.text)
        self.assertEqual(applied.json()["error"]["code"], "product_profile_conflict")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_flat_values_are_preserved_with_retirement_and_conflicts_are_rejected(
        self,
    ) -> None:
        payload = self._payload()
        payload["runtime_env"] = {
            "APP_MODE": "new-private-mode",
            "retired_provider_keys": ["LEGACY_PASSWORD"],
        }
        review = await _post_product_config_apply(
            self.app,
            payload,
            authorization="Bearer local-operator-token",
        )
        self.assertEqual(review.status_code, 202, review.text)
        self.assertIn("APP_MODE", review.json()["result"]["runtime_environment"]["changed_keys"])
        payload["mode"] = "apply"
        applied = await _post_product_config_apply(
            self.app,
            payload,
            authorization="Bearer local-operator-token",
            idempotency_key="flat-retire",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(
            self.store.list_runtime_environment_records()[0].env["APP_MODE"], "new-private-mode"
        )
        self.assertNotIn("new-private-mode", review.text + applied.text)
        conflict = self._payload()
        conflict["runtime_env"] = {
            "LEGACY_PASSWORD": "conflicting-value",
            "retired_provider_keys": ["LEGACY_PASSWORD"],
        }
        response = await _post_product_config_apply(
            self.app, conflict, authorization="Bearer local-operator-token"
        )
        self.assertEqual(response.status_code, 400, response.text)


class ProviderKeyRetirementLiveSyncTests(unittest.TestCase):
    def test_existing_record_maintenance_preserves_retirement(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = _sqlite_database_url(Path(temporary_directory) / "records.sqlite3")
            with closing(PostgresRecordStore(database_url=database_url)) as store:
                store.ensure_schema()
                store.write_runtime_environment_record(
                    _runtime_record(retired=("LEGACY_PASSWORD",))
                )
                for command, arguments in (
                    ("put", ["--set", "APP_EXTRA=temporary"]),
                    ("unset", ["--key", "APP_EXTRA"]),
                    ("relabel", ["--source-label", "operator-maintenance"]),
                ):
                    with self.subTest(command=command):
                        result = CliRunner().invoke(
                            CLI_MAIN,
                            [
                                "environments",
                                command,
                                "--database-url",
                                database_url,
                                "--scope",
                                "instance",
                                "--context",
                                "example-site",
                                "--instance",
                                "testing",
                                "--allow-direct-db-mutation",
                                *arguments,
                            ],
                        )
                        self.assertEqual(result.exit_code, 0, result.output)
                        self.assertEqual(
                            store.list_runtime_environment_records()[0].retired_provider_keys,
                            ("LEGACY_PASSWORD",),
                        )
                existing = store.list_runtime_environment_records()
            manifest = ProductOnboardingManifest.model_validate(
                {
                    "product": "example-site",
                    "display_name": "Example",
                    "repository": "example/site",
                    "image_repository": "ghcr.io/example/site",
                    "lanes": [{"context": "example-site", "instance": "testing"}],
                    "runtime_environments": [
                        {
                            "scope": "instance",
                            "context": "example-site",
                            "instance": "testing",
                            "env": {"APP_MODE": "updated"},
                        }
                    ],
                }
            )
            records = build_runtime_environment_records(
                manifest=manifest, updated_at="2026-09-27T01:00:00Z", existing_records=existing
            )
            self.assertEqual(records[0].retired_provider_keys, ("LEGACY_PASSWORD",))
            self.assertEqual(records[0].env["APP_MODE"], "updated")

    def test_changed_authority_or_failed_removal_prevents_deployment(self) -> None:
        for failure in (
            "retirement_changed",
            "application_changed",
            "declaration_changed",
            "override_changed",
            "provider_retained_key",
        ):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                database_url = _sqlite_database_url(root / "records.sqlite3")
                store = PostgresRecordStore(database_url=database_url)
                store.ensure_schema()
                self.addCleanup(store.close)
                store.write_product_profile_record(_profile())
                retired_key = (
                    "ODOO_OVERRIDE_SECRET__ADDON__FIXTURE__TOKEN"
                    if failure == "override_changed"
                    else "LEGACY_PASSWORD"
                )
                store.write_runtime_environment_record(_runtime_record(retired=(retired_key,)))
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="example-site",
                    instance="testing",
                    target_id="test-application",
                    target_type="application",
                    target_name="test-app",
                )
                updates: list[dict[str, object]] = []
                fetches = 0

                def fetch(**_kwargs: object) -> dict[str, object]:
                    nonlocal fetches
                    fetches += 1
                    if fetches == 1:
                        if failure == "retirement_changed":
                            store.write_runtime_environment_record(_runtime_record())
                        elif failure == "application_changed":
                            store.write_runtime_environment_record(
                                RuntimeEnvironmentRecord(
                                    scope="context",
                                    context="example-site",
                                    env={"LEGACY_PASSWORD": "now-an-app-setting"},
                                    updated_at="2026-09-27T00:00:00Z",
                                    source_label="test",
                                )
                            )
                        elif failure == "override_changed":
                            from control_plane.contracts.odoo_instance_override_record import (
                                OdooAddonSettingOverride,
                                OdooInstanceOverrideRecord,
                                OdooOverrideValue,
                            )

                            store.write_odoo_instance_override_record(
                                OdooInstanceOverrideRecord(
                                    context="example-site",
                                    instance="testing",
                                    apply_on=("restore",),
                                    updated_at=_runtime_record().updated_at,
                                    addon_settings=(
                                        OdooAddonSettingOverride(
                                            addon="fixture",
                                            setting="token",
                                            value=OdooOverrideValue(
                                                source="secret_binding",
                                                secret_binding_id="fixture-binding",
                                            ),
                                        ),
                                    ),
                                )
                            )
                        elif failure == "declaration_changed":
                            profile = _profile().model_dump(mode="json")
                            profile["expected_config"]["runtime_environment_keys"].append(
                                {
                                    "key": "LEGACY_PASSWORD",
                                    "context": "example-site",
                                    "instance": "testing",
                                }
                            )
                            store.write_product_profile_record(
                                LaunchplaneProductProfileRecord.model_validate(profile)
                            )
                    return {
                        "name": "test-app",
                        "env": f"APP_MODE=private-mode-value\n{retired_key}=private-old-secret",
                    }

                deploy = Mock()
                with (
                    patch(
                        "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                        return_value=("host", "token"),
                    ),
                    patch(
                        "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                        side_effect=fetch,
                    ),
                    patch(
                        "control_plane.live_target_runtime.dokploy_api.update_dokploy_target_env",
                        side_effect=lambda **kwargs: updates.append(kwargs),
                    ),
                    self.assertRaises(LiveTargetRuntimeError) as refusal,
                ):
                    apply_live_target_runtime_environment(
                        control_plane_root=root,
                        database_url=database_url,
                        product_name="example-site",
                        context_name="example-site",
                        instance_name="testing",
                        apply_changes=True,
                        deploy=True,
                        no_cache=False,
                        deploy_timeout_seconds=None,
                        deploy_trigger=deploy,
                    )
                expected_code = {
                    "retirement_changed": "runtime_retirement_changed",
                    "application_changed": "runtime_retirement_conflict",
                    "declaration_changed": "runtime_retirement_conflict",
                    "override_changed": "runtime_retirement_conflict",
                    "provider_retained_key": "dokploy_target_verification_failed",
                }[failure]
                self.assertEqual(refusal.exception.code, expected_code)
                self.assertNotIn("private-old-secret", str(refusal.exception))
                if failure == "provider_retained_key":
                    self.assertEqual(len(updates), 1)
                    updated_text = updates[0]["env_text"]
                    assert isinstance(updated_text, str)
                    self.assertNotIn("LEGACY_PASSWORD", updated_text)
                else:
                    self.assertEqual(updates, [])
                deploy.assert_not_called()

    def test_a_key_only_a_managed_secret_supplies_can_be_retired(self) -> None:
        from itertools import product

        pointer = "ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN"
        for key, plain_setting_too in product(("ALERT_TOKEN", pointer), (False, True)):
            with (
                self.subTest(key=key, plain_setting_too=plain_setting_too),
                TemporaryDirectory() as temporary_directory,
                patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"
                    },
                ),
            ):
                root = Path(temporary_directory)
                database_url = _sqlite_database_url(root / "records.sqlite3")
                store = PostgresRecordStore(database_url=database_url)
                store.ensure_schema()
                self.addCleanup(store.close)
                store.write_product_profile_record(_profile())
                store.write_runtime_environment_record(_runtime_record(retired=(key,)))
                if plain_setting_too:
                    store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            scope="context",
                            context="example-site",
                            env={key: "private-plain-value"},
                            updated_at="2026-09-27T00:00:00Z",
                            source_label="test",
                        )
                    )
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="alert-token",
                    plaintext_value="private-lane-secret",
                    binding_key=key,
                    context_name="example-site",
                    instance_name="testing",
                    actor="test",
                    source_label="test",
                )
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="example-site",
                    instance="testing",
                    target_id="test-application",
                    target_type="application",
                    target_name="test-app",
                )
                provider_env = f"APP_MODE=private-mode-value\n{key}=private-lane-secret"
                updates: list[dict[str, object]] = []

                def update(**kwargs: object) -> None:
                    nonlocal provider_env
                    updates.append(kwargs)
                    provider_env = str(kwargs["env_text"])

                with (
                    patch(
                        "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                        return_value=("host", "token"),
                    ),
                    patch(
                        "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                        side_effect=lambda **_kwargs: {"name": "test-app", "env": provider_env},
                    ),
                    patch(
                        "control_plane.live_target_runtime.dokploy_api.update_dokploy_target_env",
                        side_effect=update,
                    ),
                ):
                    if plain_setting_too:
                        with self.assertRaises(LiveTargetRuntimeError) as refusal:
                            apply_live_target_runtime_environment(
                                control_plane_root=root,
                                database_url=database_url,
                                product_name="example-site",
                                context_name="example-site",
                                instance_name="testing",
                                apply_changes=True,
                                deploy=False,
                                no_cache=False,
                                deploy_timeout_seconds=None,
                                deploy_trigger=Mock(),
                            )
                        self.assertEqual(refusal.exception.code, "runtime_retirement_conflict")
                        self.assertEqual(updates, [])
                        continue
                    apply_live_target_runtime_environment(
                        control_plane_root=root,
                        database_url=database_url,
                        product_name="example-site",
                        context_name="example-site",
                        instance_name="testing",
                        apply_changes=True,
                        deploy=False,
                        no_cache=False,
                        deploy_timeout_seconds=None,
                        deploy_trigger=Mock(),
                    )
                self.assertEqual(len(updates), 1)
                self.assertNotIn(key, provider_env)
                self.assertIn("APP_MODE=private-mode-value", provider_env)
