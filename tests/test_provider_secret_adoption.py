import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from httpx2 import Response

from control_plane import product_config as control_plane_product_config
from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.provider_key_adoption import LaneProviderEnv
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _RejectingVerifier
from tests.support.auth import _local_operator_policy
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import (
    _seed_tracked_target_records,
    _sqlite_database_url,
    _write_runtime_key_safety_policy,
)

PROVIDER_TOKEN = "provider-only-test-token"
PROVIDER_STORE_NAME = "provider-only-store-name"
SHARING_REASON = {
    "kind": "read_only_source",
    "reason": "Read-only API access",
    "evidence": "Client verified view permissions on 2026-10-02",
}


def _adopted_secret(binding_key: str, **overrides: object) -> dict[str, object]:
    return {
        "binding_key": binding_key,
        "adopt_from_provider": True,
        "secret_class": "shared_safe",
        "sharing_reason": SHARING_REASON,
        **overrides,
    }


class ProviderSecretAdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_url = _sqlite_database_url(Path(directory.name) / "records.sqlite3")
        _write_runtime_key_safety_policy(database_url=self.database_url)
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(self.store.close)
        environment = patch.dict(
            os.environ,
            {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        )
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="example-site",
            instance="prod",
            target_id="compose-example-site-prod",
            target_type="compose",
            target_name="example-site-prod",
            env={"TRACKED_TOKEN": "tracked-value"},
        )
        self.provider_env = (
            f"REPAIRSHOPR_TOKEN={PROVIDER_TOKEN}\n"
            f"REPAIRSHOPR_URL_STORE_NAME={PROVIDER_STORE_NAME}\n"
            "TRACKED_TOKEN=provider-copy\n"
            "BLANK_TOKEN=\n"
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=(
                    "product_config.plan",
                    "product_config.apply",
                    "product_profile.read",
                    "secret.list",
                    "secret.read",
                )
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    @staticmethod
    def _payload(
        mode: str, secrets_input: list[dict[str, object]], instance: str = "prod"
    ) -> dict[str, object]:
        return {
            "product": "example-site",
            "context": "example-site",
            "instance": instance,
            "mode": mode,
            "reason": "Move production's provider-only credentials into Launchplane.",
            "secrets": secrets_input,
        }

    async def _post(self, payload: dict[str, object], key: str = "") -> Response:
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
            return await _asgi_request(
                self.app,
                "POST",
                "/v1/product-config/apply",
                headers={
                    "Authorization": "Bearer test-operator-token",
                    **({"Idempotency-Key": key} if key else {}),
                },
                payload=payload,
            )

    def _lane_values(self, instance: str) -> dict[str, str]:
        return secrets.resolve_site_secret_values(
            context_name="example-site",
            instance_name=instance,
            database_url=self.database_url,
            include_site_shared=True,
        )

    async def test_reviewed_adoption_stores_provider_values_without_returning_them(self) -> None:
        adopted = [
            _adopted_secret("REPAIRSHOPR_TOKEN"),
            _adopted_secret("REPAIRSHOPR_URL_STORE_NAME"),
        ]
        dry_run = await self._post(self._payload("dry-run", adopted))
        self.assertEqual(dry_run.status_code, 202, dry_run.text)
        self.assertEqual(
            [item["action"] for item in dry_run.json()["result"]["secrets"]],
            ["created", "created"],
        )
        self.assertEqual(self._lane_values("prod"), {})

        applied = await self._post(self._payload("apply", adopted), key="adopt-once")
        self.assertEqual(applied.status_code, 202, applied.text)
        for response in (dry_run, applied):
            self.assertNotIn(PROVIDER_TOKEN, response.text)
            self.assertNotIn(PROVIDER_STORE_NAME, response.text)
        self.assertEqual(
            self._lane_values("prod"),
            {
                "REPAIRSHOPR_TOKEN": PROVIDER_TOKEN,
                "REPAIRSHOPR_URL_STORE_NAME": PROVIDER_STORE_NAME,
            },
        )
        bindings = self.store.list_secret_bindings(limit=None)
        self.assertEqual({binding.instance for binding in bindings}, {"prod"})
        for binding in bindings:
            self.assertEqual(binding.declared_secret_class, "shared_safe")
            assert binding.sharing_reason is not None
            self.assertEqual(binding.sharing_reason.kind, "read_only_source")
        records = self.store.list_secret_records(integration="runtime_environment")
        self.assertEqual({record.scope for record in records}, {"context_instance"})
        events = [
            event
            for record in records
            for event in self.store.list_secret_audit_events(secret_id=record.secret_id)
        ]
        self.assertEqual({event.metadata.get("value_source") for event in events}, {"provider_env"})
        self.assertNotIn(PROVIDER_TOKEN, repr(events))

        # The adopted secret is a copy source for testing by reference.
        token_record = next(record for record in records if record.name == "REPAIRSHOPR_TOKEN")
        copy: list[dict[str, object]] = [
            {
                "binding_key": "REPAIRSHOPR_TOKEN",
                "copy_from": {
                    "context": "example-site",
                    "instance": "prod",
                    "version_id": token_record.current_version_id,
                },
                "secret_class": "shared_safe",
                "sharing_reason": SHARING_REASON,
            }
        ]
        self.assertEqual(
            (await self._post(self._payload("dry-run", copy, "testing"))).status_code, 202
        )
        copied = await self._post(self._payload("apply", copy, "testing"), key="copy-once")
        self.assertEqual(copied.status_code, 202, copied.text)
        self.assertEqual(self._lane_values("testing")["REPAIRSHOPR_TOKEN"], PROVIDER_TOKEN)

    async def test_apply_requires_a_matching_dry_run(self) -> None:
        response = await self._post(
            self._payload("apply", [_adopted_secret("REPAIRSHOPR_TOKEN")]), key="no-review"
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")
        self.assertEqual(self.store.list_secret_records(integration="runtime_environment"), ())

    async def test_refuses_keys_missing_on_the_provider_or_already_recorded(self) -> None:
        await self._post(self._payload("dry-run", [_adopted_secret("REPAIRSHOPR_TOKEN")]), key="")
        await self._post(
            self._payload("apply", [_adopted_secret("REPAIRSHOPR_TOKEN")]), key="first"
        )
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="example-site",
                instance="prod",
                env={"RECORDED_SETTING": "plain"},
                updated_at="2026-10-03T00:00:00Z",
            )
        )
        self.provider_env += "RECORDED_SETTING=provider-copy\n"
        for binding_key, code in (
            ("ABSENT_TOKEN", "provider_secret_missing"),
            ("BLANK_TOKEN", "provider_secret_missing"),
            ("TRACKED_TOKEN", "provider_secret_already_recorded"),
            ("RECORDED_SETTING", "provider_secret_already_recorded"),
            ("REPAIRSHOPR_TOKEN", "provider_secret_already_recorded"),
        ):
            with self.subTest(binding_key):
                response = await self._post(
                    self._payload("dry-run", [_adopted_secret(binding_key)])
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(self._lane_values("prod"), {"REPAIRSHOPR_TOKEN": PROVIDER_TOKEN})

    async def test_refuses_unsafe_request_shapes(self) -> None:
        version = {"context": "example-site", "instance": "prod", "version_id": "v1"}
        for name, secret in (
            ("also a value", _adopted_secret("REPAIRSHOPR_TOKEN", value="typed")),
            ("also a copy", _adopted_secret("REPAIRSHOPR_TOKEN", copy_from=version)),
            ("false", _adopted_secret("REPAIRSHOPR_TOKEN", adopt_from_provider=False)),
            ("no class", {"binding_key": "REPAIRSHOPR_TOKEN", "adopt_from_provider": True}),
            ("context-wide", _adopted_secret("REPAIRSHOPR_TOKEN", scope="context")),
            ("other store", _adopted_secret("REPAIRSHOPR_TOKEN", integration="worker")),
        ):
            with self.subTest(name):
                response = await self._post(self._payload("dry-run", [secret]))
                self.assertIn(response.status_code, {400, 403, 422}, response.text)
                self.assertNotIn(PROVIDER_TOKEN, response.text)
        self.assertEqual(self.store.list_secret_records(integration="runtime_environment"), ())


class ProviderSecretAdoptionPlanTests(unittest.TestCase):
    def test_plain_requests_keep_their_canonical_payload(self) -> None:
        normalized: dict[str, Any] = control_plane_product_config.normalize_product_config_payload(
            {
                "product": "example-site",
                "context": "example-site",
                "instance": "prod",
                "secrets": [{"binding_key": "REPAIRSHOPR_TOKEN", "value": "typed"}],
            }
        )
        self.assertNotIn("adopt_from_provider", normalized["secrets"][0])

    def test_adoption_without_the_service_provider_read_is_refused(self) -> None:
        with self.assertRaises(control_plane_product_config.ProductConfigError) as raised:
            control_plane_product_config._resolve_provider_secret_adoptions(
                secrets=({"binding_key": "REPAIRSHOPR_TOKEN", "adopt_from_provider": True},),
                recorded_keys=frozenset(),
                lane_provider_env_reader=None,
            )
        self.assertEqual(raised.exception.code, "provider_env_unavailable")

    def test_only_adopted_entries_read_the_provider(self) -> None:
        def unexpected_read() -> LaneProviderEnv:
            raise AssertionError("a plain request must not read the provider")

        self.assertEqual(
            control_plane_product_config._resolve_provider_secret_adoptions(
                secrets=({"binding_key": "REPAIRSHOPR_TOKEN", "value": "typed"},),
                recorded_keys=frozenset(),
                lane_provider_env_reader=unexpected_read,
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
