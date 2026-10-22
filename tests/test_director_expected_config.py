from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from httpx2 import Response

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerSecretInput,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import local_operator_policy, scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import product_profile_payload


class DirectorExpectedConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(temporary.name) / 'records.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            {
                **product_profile_payload("example-site"),
                "production_use": "live",
                "lanes": [
                    {"context": "example-site", "instance": lane} for lane in ("testing", "prod")
                ],
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
            context="example-site",
            instance="prod",
            env={"APP_MODE": "existing-mode"},
            updated_at=self.profile.updated_at,
            source_label="fixture",
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.payload: dict[str, object] = {
            "schema_version": 1,
            "product": self.profile.product,
            "mode": "apply",
            "reason": "Declare the application's independently verified requirements.",
            "runtime_environment_keys": [
                {"key": "APP_MODE", "context": "example-site", "instance": lane}
                for lane in ("testing", "prod")
            ],
            "managed_secret_bindings": [
                {"binding_key": "API_TOKEN", "context": "example-site", "instance": lane}
                for lane in ("testing", "prod")
            ],
        }

    async def post(self, payload: dict[str, object], key: str = "") -> Response:
        return await request(
            self.app,
            "POST",
            "/v1/product-profiles/expected-config/apply",
            payload=payload,
            headers={
                "Authorization": "Bearer local-operator-token",
                **({"Idempotency-Key": key} if key else {}),
            },
        )

    async def test_director_declares_live_lane_requirements_without_runtime_changes(self) -> None:
        dry_run = await self.post({**self.payload, "mode": "dry-run"})
        self.assertEqual(dry_run.status_code, 202, dry_run.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        applied = await self.post(self.payload, "declarations")
        self.assertEqual(applied.status_code, 202, applied.text)
        stored = self.store.read_product_profile_record(self.profile.product)
        self.assertEqual(
            [item.model_dump() for item in stored.expected_config.runtime_environment_keys],
            self.payload["runtime_environment_keys"],
        )
        self.assertEqual(
            [
                (item.binding_key, item.context, item.instance)
                for item in stored.expected_config.managed_secret_bindings
            ],
            [("API_TOKEN", lane.context, lane.instance) for lane in self.profile.lanes],
        )
        excluded = {"expected_config", "updated_at", "source"}
        self.assertEqual(
            stored.model_dump(exclude=excluded), self.profile.model_dump(exclude=excluded)
        )
        self.assertEqual(
            self.store.list_runtime_environment_records(),
            (self.runtime,),
        )
        self.assertEqual(self.store.list_secret_bindings(integration="runtime_environment"), ())
        replay = await self.post(self.payload, "declarations")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])

    async def test_scoped_agent_does_not_inherit_director_declaration_authority(self) -> None:
        with scoped_agent_transport():
            response = await self.post(self.payload, "scoped")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_external_purpose_updates_existing_requirement_without_clearing_other_metadata(
        self,
    ) -> None:
        requirement = ProductSecretConfigRequirement(
            binding_key="API_TOKEN",
            context=self.profile.product,
            instance="testing",
            owner_input=ProductOwnerSecretInput(label="Existing provider input"),
        )
        self.profile.expected_config.managed_secret_bindings = (requirement,)
        self.store.write_product_profile_record(self.profile)
        payload = {
            "product": self.profile.product,
            "reason": "Identify the external provider without changing the credential.",
            "managed_secret_bindings": [
                {
                    "binding_key": requirement.binding_key,
                    "context": requirement.context,
                    "instance": requirement.instance,
                    "external_provider": "discord_bot",
                }
            ],
        }
        planned = await self.post({**payload, "mode": "dry-run"})
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertTrue(planned.json()["result"]["changed"])
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        applied = await self.post({**payload, "mode": "apply"}, "external-purpose")
        self.assertEqual(applied.status_code, 202, applied.text)
        stored = self.store.read_product_profile_record(self.profile.product)
        expected = requirement.model_copy(update={"external_provider": "discord_bot"})
        self.assertEqual(stored.expected_config.managed_secret_bindings, (expected,))
        omitted = await self.post(
            {
                **payload,
                "mode": "apply",
                "managed_secret_bindings": [
                    requirement.model_dump(
                        mode="json", include={"binding_key", "integration", "context", "instance"}
                    )
                ],
            },
            "purpose-omitted",
        )
        self.assertEqual(omitted.status_code, 202, omitted.text)
        self.assertFalse(omitted.json()["result"]["changed"])
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), stored)
        self.assertEqual(self.store.list_runtime_environment_records(), (self.runtime,))
        self.assertEqual(self.store.list_secret_bindings(integration="runtime_environment"), ())

    async def test_external_declaration_requires_explicit_retirement_of_existing_local_generation(
        self,
    ) -> None:
        requirement = ProductSecretConfigRequirement(
            binding_key="API_TOKEN",
            context=self.profile.product,
            instance="testing",
            testing_local_credential=True,
        )
        self.profile.expected_config.managed_secret_bindings = (requirement,)
        self.store.write_product_profile_record(self.profile)
        declaration = {
            "binding_key": requirement.binding_key,
            "context": requirement.context,
            "instance": requirement.instance,
            "external_provider": "discord_bot",
        }
        payload: dict[str, object] = {
            "product": self.profile.product,
            "mode": "apply",
            "reason": "Correct the declared credential purpose.",
            "managed_secret_bindings": [declaration],
        }
        refused = await self.post(payload, "local-conflict")
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        payload["managed_secret_bindings"] = [{**declaration, "testing_local_credential": False}]
        applied = await self.post(payload, "retire-local-intent")
        self.assertEqual(applied.status_code, 202, applied.text)
        requirement_after = self.store.read_product_profile_record(
            self.profile.product
        ).expected_config.managed_secret_bindings[0]
        self.assertEqual(requirement_after.external_provider, "discord_bot")
        self.assertFalse(requirement_after.testing_local_credential)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_duplicate_external_purpose_updates_cannot_hide_an_intermediate_mutation(
        self,
    ) -> None:
        declaration = {
            "binding_key": "API_TOKEN",
            "context": self.profile.product,
            "instance": "testing",
            "external_provider": "discord_bot",
        }
        response = await self.post(
            {
                "product": self.profile.product,
                "mode": "apply",
                "reason": "Ambiguous purpose update.",
                "managed_secret_bindings": [
                    declaration,
                    {**declaration, "external_provider": None},
                ],
            },
            "duplicate-purpose",
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_removals_do_not_inherit_addition_authority(self) -> None:
        response = await self.post(
            {
                **self.payload,
                "remove_runtime_environment_keys": [{"key": "OTHER_KEY"}],
            },
            "remove",
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_client_secret_requests_do_not_inherit_declaration_authority(self) -> None:
        response = await self.post(
            {
                **self.payload,
                "managed_secret_bindings": [
                    {
                        "binding_key": "API_TOKEN",
                        "context": "example-site",
                        "instance": "prod",
                        "owner_input": {
                            "label": "Service credential",
                            "instructions": "Supply the service credential.",
                        },
                    }
                ],
            },
            "client-request",
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_explicit_grant_does_not_bypass_live_removal_or_client_request_guard(
        self,
    ) -> None:
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=local_operator_policy(
                actions=("product_profile.expected_config.apply",),
                products=(self.profile.product,),
                contexts=("launchplane",),
                token_label="local-owner-read",
            ),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        for restricted in (
            {"remove_runtime_environment_keys": [{"key": "OTHER_KEY"}]},
            {
                "managed_secret_bindings": [
                    {
                        "binding_key": "API_TOKEN",
                        "context": "example-site",
                        "owner_input": {"label": "Service credential"},
                    }
                ]
            },
        ):
            with self.subTest(restricted=restricted):
                response = await self.post({**self.payload, **restricted}, "restricted")
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "live_product_requires_operator")
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_runtime_values_and_identity_assertions_are_not_declarations(self) -> None:
        for extra in ({"env": {"APP_MODE": "replacement"}}, {"director_agent": True}):
            with self.subTest(extra=extra):
                response = await self.post({**self.payload, **extra}, "invalid")
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
