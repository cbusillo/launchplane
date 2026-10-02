import unittest
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane import product_config as control_plane_product_config
from control_plane import product_config_service
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy.compose import odoo_compose_template_defaults
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.provider_key_adoption import LaneProviderEnv
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _AsgiResponse,
    _post_product_config_apply,
    _RejectingVerifier,
)
from tests.support.auth import _local_operator_policy
from tests.support.profiles import _generic_site_profile_payload, _odoo_preview_profile_payload
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url
from tests.test_runtime_environments import _FakeProductConfigStore

ADOPTED_VALUE = "private-adopted-value"
CREDENTIAL_URL = "https://operator:private-password@example.invalid"


def _plan(
    store: _FakeProductConfigStore,
    runtime_env: Mapping[str, object],
    *,
    provider_env: dict[str, str] | None = None,
    mode: str = "dry-run",
    schema_version: int = 2,
    instance: str = "testing",
    with_reader: bool = True,
) -> dict[str, object]:
    provider = LaneProviderEnv(
        env=provider_env or {},
        template_defaults={"APP_WORKERS": "6", "APP_ADDONS_PATH": "/default"},
        recorded_keys=frozenset({"TRACKED_SETTING"}),
        unretirable_keys=frozenset({"APP_ADDONS_PATH"}),
    )
    return control_plane_product_config.apply_product_config_bundle(
        record_store=store,
        payload={
            "schema_version": schema_version,
            "product": "sample",
            "context": "sample",
            "instance": instance,
            "runtime_env": dict(runtime_env),
        },
        mode=cast(control_plane_product_config.ProductConfigMode, mode),
        actor="operator",
        source_label="test",
        lane_provider_env_reader=(lambda: provider) if with_reader else None,
    )


class ProviderKeyAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeProductConfigStore()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="sample",
                env={"SITE_SETTING": "site-value"},
                updated_at="2026-10-02T00:00:00Z",
            )
        )

    def test_each_key_gets_a_disposition_and_no_value_leaves_the_plan(self) -> None:
        provider_env = {
            "APP_LABEL": ADOPTED_VALUE,
            "APP_WORKERS": "6",
            "APP_ADDONS_PATH": "/default",
            "SITE_SETTING": "provider-copy",
            "TRACKED_SETTING": "provider-copy",
            "APP_CALLBACK_URL": CREDENTIAL_URL,
            "APP_SIGNING_SECRET": "private-secret-value",
            "APP_BUILD_REF": "ghp_" + "a" * 36,
        }
        review = _plan(
            self.store,
            {
                "adopt_provider_keys": [
                    "APP_LABEL",
                    "APP_WORKERS",
                    "APP_ADDONS_PATH",
                    "SITE_SETTING",
                    "TRACKED_SETTING",
                    "APP_CALLBACK_URL",
                    "APP_SIGNING_SECRET",
                    "APP_BUILD_REF",
                    "APP_ABSENT",
                ]
            },
            provider_env=provider_env,
        )

        self.assertEqual(
            {
                item["key"]: item["disposition"]
                for item in cast(list[dict[str, str]], review["provider_key_adoption"])
            },
            {
                "APP_LABEL": "adopted",
                "APP_WORKERS": "template_default",
                "APP_ADDONS_PATH": "adopted",
                "SITE_SETTING": "already_recorded",
                "TRACKED_SETTING": "already_recorded",
                "APP_CALLBACK_URL": "refused_credential",
                "APP_SIGNING_SECRET": "refused_credential",
                "APP_BUILD_REF": "refused_credential",
                "APP_ABSENT": "missing",
            },
        )
        rendered = repr(review)
        for value in provider_env.values():
            self.assertNotIn(value, rendered)
        self.assertEqual(len(self.store.list_runtime_environment_records()), 1)

    def test_apply_refuses_while_any_key_is_missing_or_credential_shaped(self) -> None:
        for provider_env in ({}, {"APP_CALLBACK_URL": CREDENTIAL_URL}):
            with self.assertRaises(control_plane_product_config.ProductConfigError) as raised:
                _plan(
                    self.store,
                    {"adopt_provider_keys": ["APP_CALLBACK_URL"]},
                    provider_env=provider_env,
                    mode="apply",
                )
            self.assertEqual(raised.exception.code, "provider_key_adoption_refused")
        self.assertEqual(len(self.store.list_runtime_environment_records()), 1)

    def test_apply_records_adopted_values_and_retires_template_defaults(self) -> None:
        _plan(
            self.store,
            {
                "adopt_provider_keys": ["APP_LABEL", "APP_WORKERS", "SITE_SETTING"],
                "retired_provider_keys": ["OLD_BUILD_REF"],
                "env": {"APP_MODE": "set-by-request"},
            },
            provider_env={"APP_LABEL": ADOPTED_VALUE, "APP_WORKERS": "6", "SITE_SETTING": "x"},
            mode="apply",
        )

        lane = self.store.list_runtime_environment_records(
            context_name="sample", instance_name="testing"
        )
        lane_record = next(record for record in lane if record.scope == "instance")
        self.assertEqual(
            lane_record.env, {"APP_LABEL": ADOPTED_VALUE, "APP_MODE": "set-by-request"}
        )
        self.assertEqual(lane_record.retired_provider_keys, ("APP_WORKERS", "OLD_BUILD_REF"))

    def test_adoption_rejects_unsafe_request_shapes(self) -> None:
        adopt = {"adopt_provider_keys": ["APP_LABEL"]}
        for name, runtime_env, schema_version, instance in (
            ("old schema", adopt, 1, "testing"),
            ("not one lane", adopt, 2, ""),
            ("also set", {**adopt, "env": {"APP_LABEL": "x"}}, 2, "testing"),
            ("also retired", {**adopt, "retired_provider_keys": ["APP_LABEL"]}, 2, "testing"),
            ("not a key name", {"adopt_provider_keys": ["APP LABEL"]}, 2, "testing"),
            ("empty", {"adopt_provider_keys": []}, 2, "testing"),
        ):
            with self.subTest(name):
                with self.assertRaises(control_plane_product_config.ProductConfigError):
                    _plan(
                        self.store,
                        runtime_env,
                        provider_env={"APP_LABEL": ADOPTED_VALUE},
                        schema_version=schema_version,
                        instance=instance,
                    )
        with self.assertRaises(control_plane_product_config.ProductConfigError) as raised:
            _plan(self.store, {"adopt_provider_keys": ["APP_LABEL"]}, with_reader=False)
        self.assertEqual(raised.exception.code, "provider_env_unavailable")


class LaneProviderEnvReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_url = _sqlite_database_url(Path(temporary_directory.name) / "r.sqlite3")
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="cm",
            instance="testing",
            target_id="compose-cm-testing",
            target_type="compose",
            target_name="cm-testing",
            env={"TRACKED_SETTING": "tracked-value"},
        )
        self.store.write_secret_record(
            SecretRecord(
                secret_id="secret-cm-shared",
                scope="context",
                integration="runtime_environment",
                name="shared-service-password",
                context="cm",
                current_version_id="version-cm-shared",
                created_at="2026-10-02T00:00:00Z",
                updated_at="2026-10-02T00:00:00Z",
            )
        )
        # A binding whose secret record is gone delivers nothing, so it records nothing.
        for secret_id, binding_key in (
            ("secret-cm-shared", "SHARED_SERVICE_PASSWORD"),
            ("secret-cm-removed", "REMOVED_SERVICE_PASSWORD"),
        ):
            self.store.write_secret_binding(
                SecretBinding(
                    binding_id=f"binding-{secret_id}",
                    secret_id=secret_id,
                    integration="runtime_environment",
                    binding_key=binding_key,
                    context="cm",
                    created_at="2026-10-02T00:00:00Z",
                    updated_at="2026-10-02T00:00:00Z",
                )
            )

    def _read(self, *, product: str, instance: str = "testing") -> LaneProviderEnv:
        with (
            patch.object(
                dokploy_source,
                "read_dokploy_config",
                return_value=("https://dokploy.invalid", "provider-token"),
            ),
            patch.object(
                dokploy_api,
                "fetch_dokploy_target_payload",
                return_value={"env": f"APP_LABEL={ADOPTED_VALUE}\nODOO_ADDONS_PATH=/x\n"},
            ) as fetch,
        ):
            provider = product_config_service.read_lane_provider_env(
                record_store=self.store,
                control_plane_root=Path(self.database_url),
                product=product,
                context_name="cm",
                instance_name=instance,
            )
        self.assertEqual(fetch.call_args.kwargs["target_id"], "compose-cm-testing")
        return provider

    def test_reads_the_lanes_target_with_the_odoo_template_defaults(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        provider = self._read(product="odoo-tenant-cm")
        self.assertEqual(provider.env["APP_LABEL"], ADOPTED_VALUE)
        self.assertEqual(dict(provider.template_defaults), odoo_compose_template_defaults())
        self.assertEqual(
            provider.recorded_keys, frozenset({"TRACKED_SETTING", "SHARED_SERVICE_PASSWORD"})
        )
        self.assertEqual(provider.unretirable_keys, frozenset({"ODOO_ADDONS_PATH"}))

    def test_refuses_a_lane_the_product_does_not_own(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        with self.assertRaises(control_plane_product_config.ProductConfigError) as raised:
            product_config_service.read_lane_provider_env(
                record_store=self.store,
                control_plane_root=Path(self.database_url),
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="prod",
            )
        self.assertEqual(raised.exception.code, "provider_env_unavailable")

    def test_a_non_odoo_product_gets_no_template_defaults(self) -> None:
        payload = _generic_site_profile_payload("example-site")
        payload["lanes"] = ({"instance": "testing", "context": "cm"},)
        payload["preview"] = {"enabled": False}
        payload["expected_config"] = {}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.assertEqual(dict(self._read(product="example-site").template_defaults), {})


class ProviderKeyAdoptionApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_url = _sqlite_database_url(Path(temporary_directory.name) / "r.sqlite3")
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"ODOO_DB_NAME": "cm-testing"},
                updated_at="2026-10-02T00:00:00Z",
            )
        )
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="cm",
            instance="testing",
            target_id="compose-cm-testing",
            target_type="compose",
            target_name="cm-testing",
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan", "product_config.apply"),
                products=("odoo-tenant-cm",),
                contexts=("cm",),
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        workers_default = odoo_compose_template_defaults()["ODOO_WORKERS"]
        self.provider_env = (
            f"ODOO_WORKERS={workers_default}\n"
            f"WEB_BASE_URL={ADOPTED_VALUE}\n"
            f"APP_CALLBACK_URL={CREDENTIAL_URL}\n"
        )

    @staticmethod
    def _payload(mode: str, keys: list[str]) -> dict[str, object]:
        return {
            "schema_version": 2,
            "mode": mode,
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "reason": "Record CM testing's provider-only settings.",
            "runtime_env": {"adopt_provider_keys": keys},
        }

    async def _post(self, payload: dict[str, object], idempotency_key: str = "") -> _AsgiResponse:
        with (
            patch.object(
                dokploy_source,
                "read_dokploy_config",
                return_value=("https://dokploy.invalid", "provider-token"),
            ),
            patch.object(
                dokploy_api,
                "fetch_dokploy_target_payload",
                return_value={"env": self.provider_env},
            ),
        ):
            return await _post_product_config_apply(
                self.app,
                payload,
                authorization="Bearer local-operator-token",
                idempotency_key=idempotency_key,
            )

    async def test_dry_run_reports_dispositions_and_apply_records_values_server_side(
        self,
    ) -> None:
        refused_keys = ["APP_CALLBACK_URL", "ODOO_WORKERS", "WEB_BASE_URL"]
        refused_review = await self._post(self._payload("dry-run", refused_keys))
        self.assertEqual(refused_review.status_code, 202, refused_review.text)
        self.assertEqual(
            refused_review.json()["result"]["provider_key_adoption"],
            [
                {"key": "APP_CALLBACK_URL", "disposition": "refused_credential"},
                {"key": "ODOO_WORKERS", "disposition": "template_default"},
                {"key": "WEB_BASE_URL", "disposition": "adopted"},
            ],
        )
        refused_apply = await self._post(self._payload("apply", refused_keys), "refused")
        self.assertEqual(refused_apply.status_code, 400, refused_apply.text)
        self.assertEqual(refused_apply.json()["error"]["code"], "provider_key_adoption_refused")

        keys = ["ODOO_WORKERS", "WEB_BASE_URL"]
        review = await self._post(self._payload("dry-run", keys))
        applied = await self._post(self._payload("apply", keys), "reviewed")
        self.assertEqual(applied.status_code, 202, applied.text)
        for response in (refused_review, refused_apply, review, applied):
            self.assertNotIn(ADOPTED_VALUE, response.text)
            self.assertNotIn("private-password", response.text)
        with closing(PostgresRecordStore(database_url=self.database_url)) as independent_store:
            (record,) = independent_store.list_runtime_environment_records()
        self.assertEqual(record.env, {"ODOO_DB_NAME": "cm-testing", "WEB_BASE_URL": ADOPTED_VALUE})
        self.assertEqual(record.retired_provider_keys, ("ODOO_WORKERS",))


if __name__ == "__main__":
    unittest.main()
