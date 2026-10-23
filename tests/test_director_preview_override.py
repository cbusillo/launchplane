import base64
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import patch

from httpx2 import Response

from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class DirectorPreviewOverrideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'test.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        self.before.preview.override_env = {"EXISTING_SETTING": "preserved"}
        self.store.write_product_profile_record(self.before)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {
            "Authorization": "Bearer local-operator-token",
            "If-Match": f'"{product_profile_record_sha256(self.before)}"',
            "Idempotency-Key": "preview-setting-review",
        }
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["override_env"]["NEW_SETTING"] = "preview-only"

    async def post(self, payload: dict[str, Any] | None = None) -> Response:
        return await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers=self.headers,
            payload=payload if payload is not None else self.after,
        )

    async def test_director_saves_one_preview_key_and_preserves_every_other_field(self) -> None:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        expected = self.before.model_copy(deep=True)
        expected.preview.override_env["NEW_SETTING"] = "preview-only"
        self.assertEqual(self.store.read_product_profile_record(self.before.product), expected)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)

    async def test_addon_payload_saves_without_touching_template_or_production(self) -> None:
        payload = OdooPostDeployPayload.model_validate(
            {
                "context": self.before.preview.context,
                "instance": "preview",
                "addon_settings": [
                    {
                        "addon": "authentik_sso",
                        "setting": setting,
                        "value": {"source": "literal", "value": value},
                    }
                    for setting, value in (
                        ("client_id", "public-fixture-client"),
                        ("base_url", "https://identity.example.test"),
                        ("provider_name", "Authentik"),
                    )
                ],
            }
        )
        self.after["preview"]["override_env"].pop("NEW_SETTING")
        self.after["preview"]["override_env"][ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
            base64.b64encode(payload.to_wire_json_bytes()).decode()
        )
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        stored = self.store.read_product_profile_record(self.before.product)
        self.assertEqual(stored.lanes, self.before.lanes)
        self.assertEqual(stored.preview.override_env, self.after["preview"]["override_env"])

    async def test_unrelated_edits_deletion_and_multiple_keys_are_refused(self) -> None:
        variants = []
        for field, value in (("display_name", "Other"), ("repository", "example/other")):
            variants.append({**deepcopy(self.after), field: value})
        deletion = self.before.model_dump(mode="json")
        deletion["preview"]["override_env"] = {}
        variants.append(deletion)
        two_keys = deepcopy(self.after)
        two_keys["preview"]["override_env"]["ANOTHER_SETTING"] = "other"
        variants.append(two_keys)
        template = deepcopy(self.after)
        template["preview"]["template_instance"] = "prod"
        variants.append(template)
        lane = deepcopy(self.after)
        lane["lanes"][0]["base_url"] = "https://other.example.test"
        variants.append(lane)
        for payload in variants:
            with self.subTest(payload=payload):
                response = await self.post(payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )

    async def test_conditional_review_and_idempotency_are_required(self) -> None:
        for name in ("If-Match", "Idempotency-Key"):
            with self.subTest(header=name):
                old = self.headers.pop(name)
                response = await self.post()
                self.headers[name] = old
                self.assertEqual(response.status_code, 400, response.text)
        self.headers["If-Match"] = '"stale"'
        response = await self.post()
        self.assertEqual(response.status_code, 412, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_scoped_agent_still_gets_denial_before_revision_or_profile_state(self) -> None:
        self.headers["If-Match"] = '"stale"'
        with scoped_agent_transport():
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)

    async def test_plain_and_encoded_credentials_and_shopify_are_refused(self) -> None:
        for key, value in (
            ("API_TOKEN", "live-value"),
            ("GITHUB_TOKEN", "opaque"),
            ("CONNECTION_OPTIONS", "host=example password=fixture"),
        ):
            payload = self.before.model_dump(mode="json")
            payload["preview"]["override_env"][key] = value
            response = await self.post(payload)
            self.assertEqual(response.status_code, 403, response.text)
        for addon, setting, value in (
            ("shopify", "shop_url_key", "business-store"),
            ("example", "api_token", "live-value"),
            ("example", "connection_options", "host=example password=fixture"),
        ):
            settings = OdooPostDeployPayload.model_validate(
                {
                    "context": self.before.preview.context,
                    "instance": "preview",
                    "addon_settings": [
                        {
                            "addon": addon,
                            "setting": setting,
                            "value": {"source": "literal", "value": value},
                        }
                    ],
                }
            )
            payload = self.before.model_dump(mode="json")
            payload["preview"]["override_env"][ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
                base64.b64encode(settings.to_wire_json_bytes()).decode()
            )
            response = await self.post(payload)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_plain_override_transports_and_integration_keys_are_refused(self) -> None:
        for key in (
            config_parameter_runtime_key("web.base.url"),
            "ENV_OVERRIDE_SHOPIFY__SHOP_URL",
            "SMTP_HOST",
        ):
            payload = self.before.model_dump(mode="json")
            payload["preview"]["override_env"][key] = "https://business.example.test"
            response = await self.post(payload)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_active_policy_markers_cover_plain_and_encoded_settings(self) -> None:
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="fixture-key-policy",
                source="test",
                updated_at=self.before.updated_at,
                rules=(
                    RuntimeSecretSafetyRule(binding_key="OTHER_TOKEN", secret_class="shared_safe"),
                ),
                integration_key_markers=("EXAMPLE_CONNECTOR",),
            )
        )
        plain = self.before.model_dump(mode="json")
        plain["preview"]["override_env"]["EXAMPLE_CONNECTOR_BASE_URL"] = (
            "https://business.example.test"
        )
        encoded = self.before.model_dump(mode="json")
        encoded["preview"]["override_env"][ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
            base64.b64encode(
                OdooPostDeployPayload.model_validate(
                    {
                        "context": self.before.preview.context,
                        "instance": "preview",
                        "addon_settings": [
                            {
                                "addon": "example_connector",
                                "setting": "base_url",
                                "value": {
                                    "source": "literal",
                                    "value": "https://business.example.test",
                                },
                            }
                        ],
                    }
                ).to_wire_json_bytes()
            ).decode()
        )
        for payload in (plain, encoded):
            response = await self.post(payload)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_replacing_a_restricted_encoded_payload_is_refused(self) -> None:
        self.before.preview.override_env[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
            base64.b64encode(
                OdooPostDeployPayload.model_validate(
                    {
                        "context": self.before.preview.context,
                        "instance": "preview",
                        "addon_settings": [
                            {
                                "addon": "shopify",
                                "setting": "shop_url_key",
                                "value": {"source": "literal", "value": "protected-store"},
                            }
                        ],
                    }
                ).to_wire_json_bytes()
            ).decode()
        )
        self.store.write_product_profile_record(self.before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["override_env"][ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
            base64.b64encode(
                OdooPostDeployPayload.model_validate(
                    {
                        "context": self.before.preview.context,
                        "instance": "preview",
                        "addon_settings": [
                            {
                                "addon": "example",
                                "setting": "base_url",
                                "value": {
                                    "source": "literal",
                                    "value": "https://identity.example.test",
                                },
                            }
                        ],
                    }
                ).to_wire_json_bytes()
            ).decode()
        )
        response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_secret_binding_metadata_blocks_an_innocently_named_key(self) -> None:
        binding = SecretBinding(
            binding_id="fixture-binding",
            secret_id="fixture-secret",
            integration="runtime_environment",
            binding_key="NEW_SETTING",
            context=self.before.preview.context,
            instance=self.before.preview.template_instance,
            created_at=self.before.updated_at,
            updated_at=self.before.updated_at,
        )
        self.store.write_secret_binding(binding)
        response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_disabled_missing_and_other_driver_profiles_are_not_admitted(self) -> None:
        disabled = self.before.model_copy(deep=True)
        disabled.preview.enabled = False
        for current in (
            disabled,
            disabled.model_copy(update={"lifecycle_state": "retired"}),
            self.before.model_copy(update={"driver_id": "generic-web"}),
        ):
            self.store.write_product_profile_record(current)
            self.headers["If-Match"] = f'"{product_profile_record_sha256(current)}"'
            after = current.model_dump(mode="json")
            after["preview"]["override_env"]["NEW_SETTING"] = "preview-only"
            response = await self.post(after)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(self.store.read_product_profile_record(current.product), current)
        missing = deepcopy(self.after)
        missing["product"] = "absent-product"
        response = await self.post(missing)
        self.assertNotEqual(response.status_code, 202, response.text)

    async def test_replay_survives_a_later_unrelated_profile_change(self) -> None:
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        current = self.store.read_product_profile_record(self.before.product)
        changed = current.model_copy(update={"display_name": "Later name"})
        self.store.write_product_profile_record(changed)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), changed)

    async def test_cas_rejects_a_change_after_authorization(self) -> None:
        write = self.store.write_product_authority_bundle

        def concurrent_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_product_profile_record(
                self.before.model_copy(update={"display_name": "Concurrent"})
            )
            write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=concurrent_write
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        current = self.store.read_product_profile_record(self.before.product)
        self.assertEqual(current.preview, self.before.preview)
