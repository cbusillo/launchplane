"""The environment settings form records a site's own settings and retires provider keys."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import product_config_service
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_config_http import product_environment_config_confirmation
from control_plane.provider_key_adoption import LaneProviderEnv
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.http_app_test_support import (
    _AsgiResponse,
    _post_product_environment_config_apply,
    _post_product_config_apply,
    _RejectingVerifier,
)
from tests.support.auth import _local_operator_policy, scoped_agent_test
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url

_TOKEN = "Bearer local-operator-token"


def _profile(*, production_use: str = "unknown") -> LaunchplaneProductProfileRecord:
    payload = _generic_site_profile_payload()
    payload["production_use"] = production_use
    payload["expected_config"] = {
        "runtime_environment_keys": [
            {"key": "APP_MODE", "context": "example-site", "instance": "testing"},
            {"key": "APP_THEME", "context": "example-site", "instance": "testing"},
        ],
        "managed_secret_bindings": [
            {"binding_key": "MAIL_RELAY", "context": "example-site", "instance": "testing"}
        ],
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _runtime_record() -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        schema_version=2,
        scope="instance",
        context="example-site",
        instance="testing",
        env={"APP_MODE": "private-mode-value"},
        retired_provider_keys=("OLD_RETIRED_KEY",),
        updated_at="2026-10-02T00:00:00Z",
        source_label="test",
    )


class EnvironmentSettingsFormSiteSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary_directory.name) / "records.sqlite3")
        )
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

    async def _submit(
        self, payload: dict[str, object], *, idempotency_key: str = "", environment: str = "testing"
    ) -> _AsgiResponse:
        return await _post_product_environment_config_apply(
            self.app,
            {"reason": "Record the site's own settings.", **payload},
            environment=environment,
            authorization=_TOKEN,
            idempotency_key=idempotency_key,
        )

    async def _submit_generic(
        self,
        *,
        mode: str,
        settings: dict[str, str],
        idempotency_key: str = "",
        instance: str = "testing",
    ) -> _AsgiResponse:
        return await _post_product_config_apply(
            self.app,
            {
                "product": "example-site",
                "context": "example-site",
                "instance": instance,
                "mode": mode,
                "reason": "Check shared validation.",
                "runtime_env": {"env": settings},
            },
            authorization=_TOKEN,
            idempotency_key=idempotency_key,
        )

    async def test_live_product_testing_settings_and_retirements_stay_on_testing(self) -> None:
        profile = _profile(production_use="live")
        self.store.write_product_profile_record(profile)
        production = _runtime_record().model_copy(
            update={"instance": "prod", "env": {"SITE_MODE": "production"}}
        )
        self.store.write_runtime_environment_record(production)
        for route in ("generic", "environment"):
            with self.subTest(route=route):
                self.store.write_runtime_environment_record(_runtime_record())
                change: dict[str, object] = {
                    "reason": "Isolate testing outbound settings.",
                    "confirmation": product_environment_config_confirmation(
                        product=profile.product, environment="testing"
                    ),
                }
                if route == "generic":
                    change.update(
                        schema_version=2,
                        product=profile.product,
                        context="example-site",
                        instance="testing",
                        runtime_env={
                            "env": {"SITE_MODE": "log"},
                            "retired_provider_keys": [
                                *_runtime_record().retired_provider_keys,
                                "LEGACY_MAIL_TOKEN",
                            ],
                        },
                    )
                else:
                    change.update(
                        runtime_settings={"SITE_MODE": "log"},
                        retired_provider_keys=["LEGACY_MAIL_TOKEN"],
                    )

                async def submit(mode: str, key: str = "") -> _AsgiResponse:
                    payload = {**change, "mode": mode}
                    if route == "generic":
                        return await _post_product_config_apply(
                            self.app, payload, authorization=_TOKEN, idempotency_key=key
                        )
                    return await self._submit(payload, idempotency_key=key)

                with patch.object(
                    product_config_service,
                    "read_lane_provider_env",
                    side_effect=AssertionError("Settings must not contact the provider"),
                ):
                    unreviewed = await submit("apply", f"unreviewed-{route}")
                    self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
                    self.assertEqual(
                        unreviewed.json()["error"]["code"], "matching_dry_run_required"
                    )
                    review = await submit("dry-run")
                    self.assertEqual(review.status_code, 202, review.text)
                    self.assertEqual(
                        self.store.list_runtime_environment_records(
                            scope="instance", context_name="example-site", instance_name="testing"
                        )[0],
                        _runtime_record(),
                    )
                    applied = await submit("apply", f"testing-settings-{route}")
                    self.assertEqual(applied.status_code, 202, applied.text)
                testing = self.store.list_runtime_environment_records(
                    scope="instance", context_name="example-site", instance_name="testing"
                )[0]
                self.assertEqual(testing.env, {**_runtime_record().env, "SITE_MODE": "log"})
                self.assertEqual(
                    set(testing.retired_provider_keys),
                    {*_runtime_record().retired_provider_keys, "LEGACY_MAIL_TOKEN"},
                )
                self.assertEqual(
                    self.store.list_runtime_environment_records(
                        scope="instance", context_name="example-site", instance_name="prod"
                    )[0],
                    production,
                )
                self.assertEqual(self.store.read_product_profile_record(profile.product), profile)
                self.assertEqual(self.store.list_secret_records(), ())
                self.assertEqual(self.store.list_secret_bindings(), ())

    @scoped_agent_test
    async def test_scoped_agent_testing_settings_keep_existing_write_authority(self) -> None:
        await self.test_live_product_testing_settings_and_retirements_stay_on_testing()

    async def test_testing_settings_still_require_product_config_write_authority(self) -> None:
        self.store.write_product_profile_record(_profile(production_use="live"))
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        settings = {"SITE_MODE": "log"}
        review = await self._submit_generic(mode="dry-run", settings=settings)
        self.assertEqual(review.status_code, 202, review.text)
        denied = await self._submit_generic(
            mode="apply", settings=settings, idempotency_key="ungranted-testing-settings"
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_both_routes_refuse_credential_values_without_persistence(self) -> None:
        for key, value in (
            ("SERVICE_URL", "postgres://user:inert-password@db.invalid/site"),
            ("SERVICE_DSN", "host=db.invalid dbname=site password=inert-password"),
            ("APP_THEME", "https://site.invalid/?api_key=inert-password"),
        ):
            for route in ("generic", "environment"):
                with self.subTest(key=key, route=route):
                    response = (
                        await self._submit_generic(mode="dry-run", settings={key: value})
                        if route == "generic"
                        else await self._submit(
                            {"mode": "dry-run", "runtime_settings": {key: value}}
                        )
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["code"], "runtime_setting_refused")
                    self.assertNotIn("inert-password", response.text)
                    self.assertEqual(
                        self.store.list_runtime_environment_records(), (_runtime_record(),)
                    )
                    self.assertEqual(self.store.list_secret_records(), ())

    async def test_ordinary_declared_names_and_urls_remain_supported_on_both_routes(self) -> None:
        profile = _profile()
        requirement = profile.expected_config.runtime_environment_keys[0]
        profile = profile.model_copy(
            update={
                "expected_config": profile.expected_config.model_copy(
                    update={
                        "runtime_environment_keys": (
                            requirement,
                            requirement.model_copy(update={"key": "KEYCLOAK_URL"}),
                        )
                    }
                )
            }
        )
        self.store.write_product_profile_record(profile)
        settings = {"KEYCLOAK_URL": "https://login.example.invalid"}
        for route in ("generic", "environment"):
            review = (
                await self._submit_generic(mode="dry-run", settings=settings)
                if route == "generic"
                else await self._submit({"mode": "dry-run", "runtime_settings": settings})
            )
            self.assertEqual(review.status_code, 202, review.text)
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_both_routes_preserve_platform_host_refusal(self) -> None:
        for key in ("DOKPLOY_HOST", "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST"):
            settings = {key: "https://dokploy.example.invalid"}
            for route in ("generic", "environment"):
                response = (
                    await self._submit_generic(mode="dry-run", settings=settings)
                    if route == "generic"
                    else await self._submit({"mode": "dry-run", "runtime_settings": settings})
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "runtime_setting_refused")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_director_plans_live_settings_without_write_authority_or_effects(self) -> None:
        profile = _profile(production_use="live")
        self.store.write_product_profile_record(profile)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        with patch.object(
            self.store,
            "write_product_authority_bundle",
            wraps=self.store.write_product_authority_bundle,
        ) as write_bundle:
            for instance in ("testing", "prod", ""):
                routes = ("generic", "environment") if instance else ("generic",)
                for route in routes:
                    with self.subTest(instance=instance, route=route):
                        payload: dict[str, object] = {
                            "mode": "dry-run",
                            "reason": "Inspect a non-secret setting without applying it.",
                        }
                        if route == "generic":
                            payload.update(
                                product="example-site",
                                context="example-site",
                                instance=instance,
                                runtime_env={"env": {"SITE_MODE": "private-plan-value"}},
                            )
                            response = await _post_product_config_apply(
                                app, payload, authorization=_TOKEN
                            )
                        else:
                            payload["runtime_settings"] = {"SITE_MODE": "private-plan-value"}
                            response = await _post_product_environment_config_apply(
                                app, payload, environment=instance, authorization=_TOKEN
                            )
                        self.assertEqual(response.status_code, 202, response.text)
                        self.assertIn(
                            "SITE_MODE",
                            response.json()["result"]["runtime_environment"]["changed_keys"],
                        )
                        self.assertNotIn("private-plan-value", response.text)
            write_bundle.assert_not_called()
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        self.assertEqual(self.store.read_product_profile_record("example-site"), profile)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_director_live_provider_adoption_plan_is_read_only_and_redacted(self) -> None:
        profile = _profile(production_use="live")
        self.store.write_product_profile_record(profile)
        provider = LaneProviderEnv(
            env={"SITE_MODE": "private-provider-value", "SERVICE_PASSWORD": "private-password"},
            template_defaults={},
        )
        for instance in ("prod",):
            with (
                self.subTest(instance=instance),
                patch.object(
                    product_config_service, "read_lane_provider_env", return_value=provider
                ) as read_provider,
                patch.object(
                    self.store,
                    "write_product_authority_bundle",
                    wraps=self.store.write_product_authority_bundle,
                ) as write_bundle,
            ):
                payload: dict[str, object] = {
                    "product": "example-site",
                    "context": "example-site",
                    "instance": instance,
                    "reason": "Inspect provider-only keys without adopting their values.",
                    "schema_version": 2,
                    "runtime_env": {"adopt_provider_keys": ["SITE_MODE", "SERVICE_PASSWORD"]},
                }
                review = await _post_product_config_apply(
                    self.app, {**payload, "mode": "dry-run"}, authorization=_TOKEN
                )
                self.assertEqual(review.status_code, 202, review.text)
                self.assertEqual(read_provider.call_args.kwargs["instance_name"], instance)
                self.assertEqual(
                    {
                        item["key"]: item["disposition"]
                        for item in review.json()["result"]["provider_key_adoption"]
                    },
                    {"SERVICE_PASSWORD": "refused_credential", "SITE_MODE": "adopted"},
                )
                self.assertNotIn("private-provider-value", review.text)
                self.assertNotIn("private-password", review.text)
                applied = await _post_product_config_apply(
                    self.app,
                    {**payload, "mode": "apply"},
                    authorization=_TOKEN,
                    idempotency_key=f"live-adoption-plan-cannot-apply-{instance}",
                )
                self.assertEqual(applied.status_code, 403, applied.text)
                self.assertEqual(applied.json()["error"]["code"], "live_product_requires_operator")
                read_provider.assert_called_once()
                write_bundle.assert_not_called()
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        self.assertEqual(self.store.read_product_profile_record("example-site"), profile)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_director_live_setting_plan_does_not_authorize_apply(self) -> None:
        profile = _profile(production_use="live")
        self.store.write_product_profile_record(profile)
        for instance in ("prod", ""):
            with self.subTest(instance=instance):
                payload: dict[str, object] = {
                    "product": "example-site",
                    "context": "example-site",
                    "instance": instance,
                    "reason": "Inspect an undeclared live-product setting.",
                    "runtime_env": {"env": {"SITE_MODE": "public"}},
                }
                review = await _post_product_config_apply(
                    self.app, {**payload, "mode": "dry-run"}, authorization=_TOKEN
                )
                self.assertEqual(review.status_code, 202, review.text)
                applied = await _post_product_config_apply(
                    self.app,
                    {**payload, "mode": "apply"},
                    authorization=_TOKEN,
                    idempotency_key=f"live-plan-cannot-apply-{instance}",
                )
                self.assertEqual(applied.status_code, 403, applied.text)
                self.assertEqual(applied.json()["error"]["code"], "live_product_requires_operator")
                if instance:
                    form_payload = {
                        "reason": "Inspect the same setting through the environment form.",
                        "runtime_settings": {"SITE_MODE": "public"},
                    }
                    review = await _post_product_environment_config_apply(
                        self.app,
                        {**form_payload, "mode": "dry-run"},
                        environment=instance,
                        authorization=_TOKEN,
                    )
                    self.assertEqual(review.status_code, 202, review.text)
                    applied = await _post_product_environment_config_apply(
                        self.app,
                        {
                            **form_payload,
                            "mode": "apply",
                            "confirmation": product_environment_config_confirmation(
                                product="example-site", environment=instance
                            ),
                        },
                        environment=instance,
                        authorization=_TOKEN,
                        idempotency_key=f"live-form-plan-cannot-apply-{instance}",
                    )
                    self.assertEqual(applied.status_code, 403, applied.text)
                    self.assertEqual(
                        applied.json()["error"]["code"], "live_product_requires_operator"
                    )
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        self.assertEqual(self.store.read_product_profile_record("example-site"), profile)
        self.assertEqual(self.store.list_secret_records(), ())

    @scoped_agent_test
    async def test_live_context_wide_declared_settings_keep_their_supported_scope(self) -> None:
        profile = _profile(production_use="live")
        requirement = profile.expected_config.runtime_environment_keys[0]
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "expected_config": profile.expected_config.model_copy(
                        update={
                            "runtime_environment_keys": (
                                requirement.model_copy(update={"instance": ""}),
                            )
                        }
                    )
                }
            )
        )
        for key, status in (("APP_MODE", 202), ("SITE_MODE", 403)):
            response = await _post_product_config_apply(
                self.app,
                {
                    "product": "example-site",
                    "context": "example-site",
                    "mode": "dry-run",
                    "runtime_env": {"env": {key: "public"}},
                    "reason": "Check context scope.",
                },
                authorization=_TOKEN,
            )
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    @scoped_agent_test
    async def test_generic_route_live_guard_and_supported_declared_setting(self) -> None:
        self.store.write_product_profile_record(_profile(production_use="live"))
        for mode in ("dry-run", "apply"):
            response = await self._submit_generic(
                mode=mode,
                settings={"SITE_MODE": "public"},
                idempotency_key=f"live-undeclared-{mode}",
                instance="prod",
            )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "live_product_requires_operator")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        for production_use in ("live", "prelaunch"):
            self.store.write_product_profile_record(_profile(production_use=production_use))
            settings = {"APP_MODE" if production_use == "live" else "SITE_MODE": "public"}
            review = await self._submit_generic(mode="dry-run", settings=settings)
            self.assertEqual(review.status_code, 202, review.text)
            applied = await self._submit_generic(
                mode="apply", settings=settings, idempotency_key=f"supported-{production_use}"
            )
            self.assertEqual(applied.status_code, 202, applied.text)

    @scoped_agent_test
    async def test_generic_route_live_guard_refuses_undeclared_provider_adoption(self) -> None:
        self.store.write_product_profile_record(_profile(production_use="live"))
        for alias in ("runtime_env", "runtime_environment"):
            for mode in ("dry-run", "apply"):
                response = await _post_product_config_apply(
                    self.app,
                    {
                        "schema_version": 2,
                        "product": "example-site",
                        "context": "example-site",
                        "instance": "prod",
                        "mode": mode,
                        "confirmation": "APPLY example-site/testing",
                        "reason": "Adopt a provider setting.",
                        alias: {"env": {}, "adopt_provider_keys": ["SITE_MODE"]},
                    },
                    authorization=_TOKEN,
                    idempotency_key=f"adopt-{alias}-{mode}",
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "live_product_requires_operator")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    @scoped_agent_test
    async def test_live_guard_checks_the_canonical_flat_and_extra_runtime_values(self) -> None:
        self.store.write_product_profile_record(_profile(production_use="live"))
        for alias in ("runtime_env", "runtime_environment"):
            for runtime_input in (
                {"scope": None, "SITE_MODE": "public"},
                {"adopt_provider_keys": ["APP_MODE"], "SITE_MODE": "public"},
            ):
                for mode in ("dry-run", "apply"):
                    response = await _post_product_config_apply(
                        self.app,
                        {
                            "schema_version": 2,
                            "product": "example-site",
                            "context": "example-site",
                            "instance": "prod",
                            "mode": mode,
                            "reason": "Check canonical settings.",
                            "confirmation": "APPLY example-site/testing",
                            alias: runtime_input,
                        },
                        authorization=_TOKEN,
                        idempotency_key=f"extras-{alias}-{mode}",
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "live_product_requires_operator"
                    )
            review = await _post_product_config_apply(
                self.app,
                {
                    "product": "example-site",
                    "context": "example-site",
                    "instance": "testing",
                    "mode": "dry-run",
                    "reason": "Check supported flat targeting.",
                    alias: {"context": "example-site", "instance": "testing", "APP_MODE": "public"},
                },
                authorization=_TOKEN,
            )
            self.assertEqual(review.status_code, 202, review.text)
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_generic_route_profile_change_at_commit_preserves_concurrent_state(self) -> None:
        settings = {"SITE_MODE": "public"}
        review = await self._submit_generic(mode="dry-run", settings=settings)
        self.assertEqual(review.status_code, 202, review.text)
        original_write = self.store.write_product_authority_bundle

        def change_then_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_product_profile_record(_profile(production_use="live"))
            original_write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=change_then_write
        ):
            response = await self._submit_generic(
                mode="apply", settings=settings, idempotency_key="generic-profile-at-commit"
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "product_profile_conflict")
        self.assertEqual(
            self.store.read_product_profile_record("example-site").production_use, "live"
        )
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_records_an_undeclared_setting_and_retires_a_provider_key(self) -> None:
        change: dict[str, object] = {
            "runtime_settings": {"SITE_BASE_URL": "https://site.example.invalid"},
            "retired_provider_keys": ["LEGACY_TUNING"],
        }
        review = await self._submit({"mode": "dry-run", **change})
        self.assertEqual(review.status_code, 202, review.text)
        summary = review.json()["result"]["runtime_environment"]
        self.assertEqual(summary["changed_keys"], ["LEGACY_TUNING", "SITE_BASE_URL"])
        self.assertEqual(
            summary["retired_provider_keys_after"], ["LEGACY_TUNING", "OLD_RETIRED_KEY"]
        )
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

        applied = await self._submit(
            {"mode": "apply", "confirmation": "APPLY example-site/testing", **change},
            idempotency_key="site-settings-apply",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        (record,) = self.store.list_runtime_environment_records()
        self.assertEqual(
            record.env,
            {"APP_MODE": "private-mode-value", "SITE_BASE_URL": "https://site.example.invalid"},
        )
        self.assertEqual(record.retired_provider_keys, ("LEGACY_TUNING", "OLD_RETIRED_KEY"))
        self.assertNotIn("site.example.invalid", review.text + applied.text)

    async def test_a_completed_retirement_replays_after_a_later_retirement(self) -> None:
        async def retire(key: str, idempotency_key: str) -> _AsgiResponse:
            change: dict[str, object] = {"retired_provider_keys": [key]}
            review = await self._submit({"mode": "dry-run", **change})
            self.assertEqual(review.status_code, 202, review.text)
            return await self._submit(
                {"mode": "apply", "confirmation": "APPLY example-site/testing", **change},
                idempotency_key=idempotency_key,
            )

        first = await retire("LEGACY_A", "retire-a")
        self.assertEqual(first.status_code, 202, first.text)
        second = await retire("LEGACY_B", "retire-b")
        self.assertEqual(second.status_code, 202, second.text)
        replay = await self._submit(
            {
                "mode": "apply",
                "confirmation": "APPLY example-site/testing",
                "retired_provider_keys": ["LEGACY_A"],
            },
            idempotency_key="retire-a",
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        (record,) = self.store.list_runtime_environment_records()
        self.assertEqual(record.retired_provider_keys, ("LEGACY_A", "LEGACY_B", "OLD_RETIRED_KEY"))

    async def test_refuses_a_credential_or_a_declared_secret_as_a_plain_setting(self) -> None:
        for settings in (
            {"PAYMENT_API_KEY": "plain-looking"},
            {"SITE_DATABASE_URL": "postgres://user:hunter2@db.invalid/site"},
            {"SITE_DSN": "host=db.invalid dbname=site user=site password=hunter2"},
            {"MAIL_RELAY": "smtp.example.invalid"},
            {"not a key": "value"},
        ):
            with self.subTest(settings=list(settings)):
                response = await self._submit({"mode": "dry-run", "runtime_settings": settings})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "runtime_setting_refused")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))

    async def test_retirement_replay_refuses_a_foreign_duplicate_lane_claim(self) -> None:
        change: dict[str, object] = {"retired_provider_keys": ["LEGACY_TUNING"]}
        review = await self._submit({"mode": "dry-run", **change})
        self.assertEqual(review.status_code, 202, review.text)
        payload = {"mode": "apply", "confirmation": "APPLY example-site/testing", **change}
        applied = await self._submit(payload, idempotency_key="retirement-ownership")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.store.write_product_profile_record(
            _profile().model_copy(update={"product": "other-site"})
        )
        before = self.store.list_runtime_environment_records()
        with patch.object(self.store, "write_product_authority_bundle") as writer:
            replay = await self._submit(payload, idempotency_key="retirement-ownership")
        self.assertEqual(replay.status_code, 403, replay.text)
        self.assertEqual(replay.json()["error"]["code"], "product_config_lane_not_owned")
        writer.assert_not_called()
        self.assertEqual(self.store.list_runtime_environment_records(), before)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_plain_settings_pin_the_profile_used_to_construct_the_request(self) -> None:
        change: dict[str, object] = {"runtime_settings": {"SITE_BASE_URL": "https://site.invalid"}}
        review = await self._submit({"mode": "dry-run", **change})
        self.assertEqual(review.status_code, 202, review.text)
        original_read = self.store.read_product_profile_record
        for mode in ("dry-run", "apply"):
            with self.subTest(mode=mode):
                self.store.write_product_profile_record(_profile())
                read_count = 0

                def change_before_second_read(product: str) -> LaunchplaneProductProfileRecord:
                    nonlocal read_count
                    read_count += 1
                    if read_count == 2:
                        self.store.write_product_profile_record(_profile(production_use="live"))
                    return original_read(product)

                with (
                    patch.object(
                        self.store,
                        "read_product_profile_record",
                        side_effect=change_before_second_read,
                    ),
                    patch.object(self.store, "write_product_authority_bundle") as writer,
                ):
                    response = await self._submit(
                        {"mode": mode, "confirmation": "APPLY example-site/testing", **change},
                        idempotency_key=f"profile-snapshot-{mode}",
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "product_profile_conflict")
                writer.assert_not_called()
                self.assertEqual(
                    self.store.list_runtime_environment_records(), (_runtime_record(),)
                )
                self.assertEqual(self.store.list_secret_records(), ())

    async def test_plain_settings_profile_change_at_commit_refuses_atomically(self) -> None:
        change: dict[str, object] = {"runtime_settings": {"SITE_BASE_URL": "https://site.invalid"}}
        review = await self._submit({"mode": "dry-run", **change})
        self.assertEqual(review.status_code, 202, review.text)
        original_write = self.store.write_product_authority_bundle

        def change_then_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_product_profile_record(_profile(production_use="live"))
            original_write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=change_then_write
        ):
            response = await self._submit(
                {"mode": "apply", "confirmation": "APPLY example-site/testing", **change},
                idempotency_key="profile-at-commit",
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "product_profile_conflict")
        self.assertEqual(self.store.list_runtime_environment_records(), (_runtime_record(),))
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_retirement_keeps_the_product_config_guards(self) -> None:
        declared = await self._submit({"mode": "dry-run", "retired_provider_keys": ["APP_THEME"]})
        self.assertEqual(declared.status_code, 400, declared.text)
        self.assertEqual(declared.json()["error"]["code"], "runtime_retirement_conflict")
        driver = await self._submit(
            {"mode": "dry-run", "retired_provider_keys": ["ODOO_DB_PASSWORD"]}
        )
        self.assertEqual(driver.status_code, 400, driver.text)
        self.assertEqual(driver.json()["error"]["code"], "runtime_retirement_conflict")

    async def test_retirement_uses_the_checked_profile_during_a_declaration_edit(self) -> None:
        original_read = self.store.read_product_profile_record
        for key, declared in (("APP_THEME", False), ("LEGACY_TUNING", True)):
            for mode in ("dry-run", "apply") if declared else ("dry-run",):
                with self.subTest(key=key, mode=mode):
                    profile = _profile()
                    self.store.write_product_profile_record(profile)
                    declarations = profile.expected_config.runtime_environment_keys
                    edited_config = profile.expected_config.model_copy(
                        update={
                            "runtime_environment_keys": (
                                (*declarations, declarations[0].model_copy(update={"key": key}))
                                if declared
                                else tuple(item for item in declarations if item.key != key)
                            )
                        }
                    )
                    if mode == "apply":
                        review = await self._submit(
                            {"mode": "dry-run", "retired_provider_keys": [key]}
                        )
                        self.assertEqual(review.status_code, 202, review.text)

                    read_count = 0

                    def edit_after_checked_profile(product: str) -> LaunchplaneProductProfileRecord:
                        nonlocal read_count
                        read_count += 1
                        snapshot = original_read(product)
                        if read_count == 3:
                            self.store.write_product_profile_record(
                                profile.model_copy(update={"expected_config": edited_config})
                            )
                        return snapshot

                    with patch.object(
                        self.store,
                        "read_product_profile_record",
                        side_effect=edit_after_checked_profile,
                    ):
                        response = await self._submit(
                            {
                                "mode": mode,
                                "confirmation": "APPLY example-site/testing",
                                "retired_provider_keys": [key],
                            },
                            idempotency_key=f"retirement-declaration-{key}-{mode}",
                        )
                    if not declared:
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertEqual(
                            response.json()["error"]["code"], "runtime_retirement_conflict"
                        )
                    elif mode == "dry-run":
                        self.assertEqual(response.status_code, 202, response.text)
                    else:
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertEqual(
                            response.json()["error"]["code"], "product_profile_conflict"
                        )
                    self.assertNotEqual(
                        self.store.read_product_profile_record("example-site"), profile
                    )
                    self.assertEqual(
                        self.store.list_runtime_environment_records(), (_runtime_record(),)
                    )
                    self.assertEqual(self.store.list_secret_records(), ())

    @scoped_agent_test
    async def test_local_operator_cannot_record_undeclared_settings_on_a_live_product(
        self,
    ) -> None:
        self.store.write_product_profile_record(_profile(production_use="live"))
        undeclared = await self._submit(
            {"mode": "dry-run", "runtime_settings": {"SITE_BASE_URL": "https://site.invalid"}},
            environment="prod",
        )
        self.assertEqual(undeclared.status_code, 403, undeclared.text)
        self.assertEqual(undeclared.json()["error"]["code"], "live_product_requires_operator")
        declared = await self._submit(
            {"mode": "dry-run", "runtime_settings": {"APP_MODE": "another-mode"}}
        )
        self.assertEqual(declared.status_code, 202, declared.text)


if __name__ == "__main__":
    unittest.main()
