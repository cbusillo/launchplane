import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click import ClickException

from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetRecord,
    DokployTargetShopifyPolicy,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretStatus
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_addon_settings_override import (
    ODOO_ADDON_SETTINGS_APPLY_ROUTE,
    OdooAddonSettingsApplyRequest,
    OdooAddonSettingsRefusal,
    OdooAddonSettingsStale,
    apply_odoo_addon_settings_plan,
    build_odoo_addon_settings_plan,
)
from control_plane.odoo_instance_overrides import (
    _resolve_shopify_payload_settings,
    addon_setting_secret_env_key,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from fastapi import FastAPI

from tests.http_app_test_support import _AsgiResponse, _asgi_request
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import _odoo_profile_payload_with_prod_lane

_PRODUCT = "odoo-tenant-cm"
_CONTEXT = "cm"
_TIMESTAMP = "2026-09-28T12:00:00Z"
_PROTECTED_KEY = "cm-real-store"
_DEV_STORE_KEY = "cm-dev-store"


def _binding_id(instance: str, setting: str) -> str:
    return f"secret-shopify-{setting}-{_CONTEXT}-{instance}-binding"


def _seed_lane(
    store: FilesystemRecordStore,
    *,
    instance: str = "testing",
    protected_keys: tuple[str, ...] = (_PROTECTED_KEY,),
    binding_key_override: dict[str, str] | None = None,
    binding_status: SecretStatus = "configured",
    write_target: bool = True,
) -> None:
    if write_target:
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context=_CONTEXT,
                instance=instance,
                policies=DokployTargetPolicies(
                    shopify=DokployTargetShopifyPolicy(protected_store_keys=protected_keys)
                ),
                updated_at=_TIMESTAMP,
            )
        )
    for setting in ("api_token", "webhook_key"):
        secret_id = f"secret-shopify-{setting}-{_CONTEXT}-{instance}"
        store.write_secret_record(
            SecretRecord(
                secret_id=secret_id,
                scope="context_instance",
                integration="shopify",
                name=setting,
                context=_CONTEXT,
                instance=instance,
                current_version_id=f"{secret_id}-version-1",
                created_at=_TIMESTAMP,
                updated_at=_TIMESTAMP,
            )
        )
        binding_key = (binding_key_override or {}).get(
            setting,
            addon_setting_secret_env_key(addon_name="shopify", setting_name=setting),
        )
        store.write_secret_binding(
            SecretBinding(
                binding_id=_binding_id(instance, setting),
                secret_id=secret_id,
                integration="shopify",
                binding_key=binding_key,
                context=_CONTEXT,
                instance=instance,
                status=binding_status,
                created_at=_TIMESTAMP,
                updated_at=_TIMESTAMP,
            )
        )


def _existing_record(instance: str = "testing") -> OdooInstanceOverrideRecord:
    return OdooInstanceOverrideRecord(
        context=_CONTEXT,
        instance=instance,
        apply_on=("deploy", "promotion"),
        config_parameters=(
            OdooConfigParameterOverride(
                key="web.base.url",
                value=OdooOverrideValue(source="literal", value="https://cm-testing.example"),
            ),
        ),
        updated_at=_TIMESTAMP,
    )


def _request_payload(
    *,
    instance: str = "testing",
    mode: str = "dry-run",
    shop_url_key: str = _DEV_STORE_KEY,
    test_store: bool = True,
    reviewed_plan_sha256: str = "",
    api_token_binding: str | None = None,
) -> dict[str, object]:
    return {
        "product": _PRODUCT,
        "context": _CONTEXT,
        "instance": instance,
        "mode": mode,
        "reason": "Load the dev-store settings for testing restores.",
        "reviewed_plan_sha256": reviewed_plan_sha256,
        "shopify": {
            "shop_url_key": shop_url_key,
            "api_version": "2025-07",
            "api_token_secret_binding_id": api_token_binding
            if api_token_binding is not None
            else _binding_id(instance, "api_token"),
            "webhook_key_secret_binding_id": _binding_id(instance, "webhook_key"),
            "test_store": test_store,
        },
    }


def _request(**kwargs: object) -> OdooAddonSettingsApplyRequest:
    return OdooAddonSettingsApplyRequest.model_validate(_request_payload(**kwargs))  # type: ignore[arg-type]


class OdooAddonSettingsPlanTests(unittest.TestCase):
    def test_dry_run_returns_redacted_diff_and_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            store.write_odoo_instance_override_record(_existing_record())

            plan, existing, replacement = build_odoo_addon_settings_plan(
                record_store=store, request=_request()
            )

        self.assertIsNotNone(existing)
        self.assertTrue(plan.changed)
        self.assertFalse(plan.applied)
        self.assertRegex(plan.plan_sha256, r"^[0-9a-f]{64}$")
        changes = {change.setting: change for change in plan.changes}
        self.assertEqual(
            set(changes), {"shop_url_key", "api_token", "webhook_key", "api_version", "test_store"}
        )
        self.assertTrue(all(change.action == "add" for change in changes.values()))
        token_after = changes["api_token"].after
        assert token_after is not None
        self.assertEqual(token_after.source, "secret_binding")
        self.assertIsNone(token_after.value)
        self.assertTrue(token_after.secret_binding_present)
        shop_after = changes["shop_url_key"].after
        assert shop_after is not None
        self.assertEqual(shop_after.value, _DEV_STORE_KEY)
        # The replacement keeps unrelated overrides.
        self.assertEqual(replacement.config_parameters[0].key, "web.base.url")

    def test_digest_is_stable_and_binds_current_record(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            first, _, _ = build_odoo_addon_settings_plan(record_store=store, request=_request())
            second, _, _ = build_odoo_addon_settings_plan(record_store=store, request=_request())
            store.write_odoo_instance_override_record(_existing_record())
            third, _, _ = build_odoo_addon_settings_plan(record_store=store, request=_request())
            other_key, _, _ = build_odoo_addon_settings_plan(
                record_store=store, request=_request(shop_url_key="cm-other-dev")
            )

        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertNotEqual(first.plan_sha256, third.plan_sha256)
        self.assertNotEqual(third.plan_sha256, other_key.plan_sha256)

    def test_apply_requires_matching_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            with self.assertRaises(OdooAddonSettingsStale):
                apply_odoo_addon_settings_plan(
                    record_store=store,
                    request=_request(mode="apply", reviewed_plan_sha256="0" * 64),
                )
            with self.assertRaises(FileNotFoundError):
                store.read_odoo_instance_override_record(
                    context_name=_CONTEXT, instance_name="testing"
                )

    def test_apply_writes_record_and_reads_back_redacted(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            store.write_odoo_instance_override_record(_existing_record())
            dry_run, _, _ = build_odoo_addon_settings_plan(record_store=store, request=_request())

            applied = apply_odoo_addon_settings_plan(
                record_store=store,
                request=_request(mode="apply", reviewed_plan_sha256=dry_run.plan_sha256),
            )
            stored = store.read_odoo_instance_override_record(
                context_name=_CONTEXT, instance_name="testing"
            )
            replay_plan, _, _ = build_odoo_addon_settings_plan(
                record_store=store, request=_request()
            )

        self.assertTrue(applied.applied)
        self.assertTrue(applied.read_back_matches)
        self.assertNotEqual(applied.record_sha256_after, applied.record_sha256_before)
        read_back = {evidence.setting: evidence for evidence in applied.read_back}
        self.assertEqual(read_back["shop_url_key"].value, _DEV_STORE_KEY)
        self.assertEqual(read_back["test_store"].value, True)
        self.assertEqual(read_back["api_version"].value, "2025-07")
        self.assertIsNone(read_back["api_token"].value)
        self.assertTrue(read_back["api_token"].secret_binding_present)
        self.assertEqual(stored.config_parameters[0].key, "web.base.url")
        self.assertEqual(stored.source_label, "service:odoo-addon-settings")
        self.assertFalse(replay_plan.changed)
        # The stored record renders an apply action at post-deploy.
        rendered = _resolve_shopify_payload_settings(
            record=stored, protected_shopify_store_keys=(_PROTECTED_KEY,)
        )
        self.assertEqual(rendered[0].value.value, "apply")
        self.assertIn("test_store", {setting.setting for setting in rendered})

    def test_apply_replaces_existing_shopify_settings_only(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            record = _existing_record().model_copy(
                update={
                    "addon_settings": (
                        OdooAddonSettingOverride(
                            addon="shopify",
                            setting="api_token",
                            value=OdooOverrideValue(source="literal", value="legacy-plaintext"),
                        ),
                        OdooAddonSettingOverride(
                            addon="shopify",
                            setting="allow_production",
                            value=OdooOverrideValue(source="literal", value=True),
                        ),
                        OdooAddonSettingOverride(
                            addon="openai",
                            setting="api_key",
                            value=OdooOverrideValue(
                                source="secret_binding", secret_binding_id="secret-openai"
                            ),
                        ),
                    )
                }
            )
            store.write_odoo_instance_override_record(record)
            dry_run, _, _ = build_odoo_addon_settings_plan(record_store=store, request=_request())
            apply_odoo_addon_settings_plan(
                record_store=store,
                request=_request(mode="apply", reviewed_plan_sha256=dry_run.plan_sha256),
            )
            stored = store.read_odoo_instance_override_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        changes = {change.setting: change for change in dry_run.changes}
        self.assertEqual(changes["allow_production"].action, "remove")
        self.assertEqual(changes["api_token"].action, "update")
        token_before = changes["api_token"].before
        assert token_before is not None
        # A legacy literal credential is shown as present, never by value.
        self.assertIsNone(token_before.value)
        self.assertTrue(token_before.value_present)
        self.assertNotIn("legacy-plaintext", dry_run.model_dump_json())
        addons = {(item.addon, item.setting) for item in stored.addon_settings}
        self.assertIn(("openai", "api_key"), addons)
        self.assertNotIn(("shopify", "allow_production"), addons)

    def _assert_refused(self, code: str, **kwargs: object) -> None:
        seed = kwargs.pop("seed", {})
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store, **seed)  # type: ignore[arg-type]
            with self.assertRaises(OdooAddonSettingsRefusal) as raised:
                build_odoo_addon_settings_plan(record_store=store, request=_request(**kwargs))
        self.assertEqual(raised.exception.code, code)

    def test_refuses_protected_store_key(self) -> None:
        self._assert_refused("protected_store_key", shop_url_key=_PROTECTED_KEY.upper())

    def test_refuses_protected_store_as_myshopify_domain(self) -> None:
        self._assert_refused("protected_store_key", shop_url_key=f"{_PROTECTED_KEY}.myshopify.com")

    def test_refuses_production_like_store_key(self) -> None:
        self._assert_refused("production_like_store_key", shop_url_key="cm-production")

    def test_refuses_test_store_on_production_lane(self) -> None:
        self._assert_refused("production_test_store", instance="prod", seed={"instance": "prod"})

    def test_refuses_missing_secret_binding_without_echoing_it(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            with self.assertRaises(OdooAddonSettingsRefusal) as raised:
                build_odoo_addon_settings_plan(
                    record_store=store, request=_request(api_token_binding="not-a-binding-xyz")
                )
        self.assertEqual(raised.exception.code, "secret_binding_missing")
        self.assertNotIn("not-a-binding-xyz", str(raised.exception))

    def test_refuses_binding_from_another_lane(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_lane(store)
            _seed_lane(store, instance="prod", write_target=False)
            with self.assertRaises(OdooAddonSettingsRefusal) as raised:
                build_odoo_addon_settings_plan(
                    record_store=store,
                    request=_request(api_token_binding=_binding_id("prod", "api_token")),
                )
        self.assertEqual(raised.exception.code, "secret_binding_missing")

    def test_refuses_binding_with_wrong_environment_key(self) -> None:
        self._assert_refused(
            "secret_binding_invalid",
            seed={"binding_key_override": {"api_token": "SHOPIFY_API_TOKEN"}},
        )

    def test_refuses_disabled_binding(self) -> None:
        self._assert_refused("secret_binding_invalid", seed={"binding_status": "disabled"})

    def test_refuses_without_target_policy_record(self) -> None:
        self._assert_refused("target_policy_missing", seed={"write_target": False})

    def test_request_rejects_plaintext_secret_fields(self) -> None:
        payload = _request_payload()
        shopify = payload["shopify"]
        assert isinstance(shopify, dict)
        shopify["api_token"] = "plaintext"
        with self.assertRaises(ValueError):
            OdooAddonSettingsApplyRequest.model_validate(payload)

    def test_request_rejects_credential_shaped_binding_id(self) -> None:
        with self.assertRaises(ValueError):
            _request(api_token_binding="shpat_" + "example")

    def test_request_rejects_apply_without_digest(self) -> None:
        with self.assertRaises(ValueError):
            _request(mode="apply")
        with self.assertRaises(ValueError):
            _request(reviewed_plan_sha256="a" * 64)


class OdooShopifyResolverTests(unittest.TestCase):
    @staticmethod
    def _record(instance: str) -> OdooInstanceOverrideRecord:
        return _existing_record(instance)

    def test_non_production_lane_without_settings_emits_clear(self) -> None:
        for instance in ("testing", "dev", "unexpected-lane"):
            with self.subTest(instance=instance):
                rendered = _resolve_shopify_payload_settings(record=self._record(instance))
                self.assertEqual(len(rendered), 1)
                self.assertEqual(rendered[0].setting, "action")
                self.assertEqual(rendered[0].value.value, "clear")

    def test_production_lane_without_settings_is_unchanged(self) -> None:
        self.assertEqual(_resolve_shopify_payload_settings(record=self._record("prod")), [])

    def test_protected_store_domain_form_is_refused_at_render(self) -> None:
        record = self._record("testing").model_copy(
            update={
                "addon_settings": tuple(
                    OdooAddonSettingOverride(
                        addon="shopify",
                        setting=setting,
                        value=OdooOverrideValue(source="literal", value=value),
                    )
                    for setting, value in (
                        ("shop_url_key", f"https://{_PROTECTED_KEY}.myshopify.com/"),
                        ("api_token", "t"),
                        ("webhook_key", "w"),
                        ("api_version", "2025-07"),
                    )
                )
            }
        )
        with self.assertRaises(ClickException):
            _resolve_shopify_payload_settings(
                record=record, protected_shopify_store_keys=(_PROTECTED_KEY,)
            )

    def test_partial_settings_still_clear(self) -> None:
        record = self._record("testing").model_copy(
            update={
                "addon_settings": (
                    OdooAddonSettingOverride(
                        addon="shopify",
                        setting="shop_url_key",
                        value=OdooOverrideValue(source="literal", value=_DEV_STORE_KEY),
                    ),
                )
            }
        )
        rendered = _resolve_shopify_payload_settings(record=record)
        self.assertEqual([item.value.value for item in rendered], ["clear"])


class OdooAddonSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    _WORKFLOW_REF = "cbusillo/launchplane/.github/workflows/operator.yml@refs/heads/main"

    def _identity(self) -> GitHubActionsIdentity:
        return _identity(
            repository="cbusillo/launchplane",
            workflow_ref=self._WORKFLOW_REF,
            event_name="workflow_dispatch",
        )

    def _policy(self, *actions: str) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [self._WORKFLOW_REF],
                        "event_names": ["workflow_dispatch"],
                        "products": [_PRODUCT],
                        "contexts": [_CONTEXT],
                        "actions": list(actions),
                    }
                ]
            }
        )

    @staticmethod
    def _store(root: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=root / "state")
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
        )
        _seed_lane(store)
        store.write_odoo_instance_override_record(_existing_record())
        return store

    @staticmethod
    async def _post(
        app: FastAPI,
        payload: dict[str, object],
        *,
        idempotency_key: str = "",
    ) -> _AsgiResponse:
        headers = {"Authorization": "Bearer valid-token"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await _asgi_request(
            app,
            "POST",
            ODOO_ADDON_SETTINGS_APPLY_ROUTE,
            headers=headers,
            payload=payload,
        )

    def _app(self, store: FilesystemRecordStore, root: Path, *actions: str) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(*actions),
            record_store_factory=lambda: store,
            control_plane_root_path=root,
        )

    async def test_dry_run_then_apply_with_digest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan", "product_config.apply")

            dry_run = await self._post(app, _request_payload())
            self.assertEqual(dry_run.status_code, 202)
            plan = dry_run.json()["result"]
            unchanged = store.read_odoo_instance_override_record(
                context_name=_CONTEXT, instance_name="testing"
            )
            self.assertEqual(unchanged.addon_settings, ())

            missing_key = await self._post(
                app,
                _request_payload(mode="apply", reviewed_plan_sha256=plan["plan_sha256"]),
            )
            self.assertEqual(missing_key.status_code, 400)
            self.assertEqual(
                missing_key.json()["error"]["code"],
                "idempotency_key_required",
            )

            stale = await self._post(
                app,
                _request_payload(mode="apply", reviewed_plan_sha256="f" * 64),
                idempotency_key="cm-testing-shopify-stale",
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["error"]["code"], "stale")

            apply_payload = _request_payload(mode="apply", reviewed_plan_sha256=plan["plan_sha256"])
            applied = await self._post(
                app, apply_payload, idempotency_key="cm-testing-shopify-apply"
            )
            replayed = await self._post(
                app, apply_payload, idempotency_key="cm-testing-shopify-apply"
            )
            stored = store.read_odoo_instance_override_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        self.assertEqual(applied.status_code, 202)
        result = applied.json()["result"]
        self.assertTrue(result["applied"])
        self.assertTrue(result["read_back_matches"])
        self.assertEqual(result["plan_sha256"], plan["plan_sha256"])
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(
            {item.setting for item in stored.addon_settings},
            {"shop_url_key", "api_token", "webhook_key", "api_version", "test_store"},
        )

    async def test_dry_run_requires_product_config_plan(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "odoo_config_parameter_override.write")
            response = await self._post(app, _request_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["code"],
            "authorization_denied",
        )

    async def test_apply_requires_product_config_apply(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan")
            response = await self._post(
                app,
                _request_payload(mode="apply", reviewed_plan_sha256="a" * 64),
                idempotency_key="cm-testing-shopify-denied",
            )

        self.assertEqual(response.status_code, 403)

    async def test_refusal_is_conflict_with_code(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan")
            response = await self._post(app, _request_payload(shop_url_key=_PROTECTED_KEY))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "addon_settings_protected_store_key",
        )

    async def test_plaintext_secret_is_rejected_and_not_echoed(self) -> None:
        payload = _request_payload()
        shopify = payload["shopify"]
        assert isinstance(shopify, dict)
        shopify["api_token"] = "shpat_plaintext_value_should_not_echo"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan")
            response = await self._post(app, payload)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(
            "shpat_plaintext_value_should_not_echo",
            json.dumps(response.json()),
        )


if __name__ == "__main__":
    unittest.main()
