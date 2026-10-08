import copy
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import ClassVar, cast
from fastapi import FastAPI
from httpx2 import Response
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.support.auth import identity, StubVerifier
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url


PRODUCT = "odoo-tenant-cm-website"
CONTEXT = "cm_website"
HOSTS = ["cellmechanic.com", "www.cellmechanic.com"]


class StubProvider:
    def __init__(self) -> None:
        self.routes: list[api.JsonObject] = [
            {
                "domainId": "internal",
                "composeId": "cm-prod-compose",
                "host": "cm-website-prod",
                "serviceName": "web",
                "port": 8069,
                "path": "/",
                "internalPath": "/",
                "https": True,
                "certificateType": "none",
                "stripPath": False,
            }
        ]
        self.writes: list[str] = []
        self.fail_host = ""
        self.drop_writes = False

    def call(self, **kwargs: object) -> api.JsonValue:
        path = kwargs["path"]
        if path == "/api/domain.byComposeId":
            return cast(api.JsonValue, copy.deepcopy(self.routes))
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        self.writes.append(str(path))
        if payload.get("host") == self.fail_host:
            raise RuntimeError("provider disconnected")
        if path == "/api/domain.create":
            route = {"domainId": f"public-{len(self.routes)}", **payload}
            if not self.drop_writes:
                self.routes.append(route)
            return route
        if path == "/api/domain.update":
            for route in self.routes:
                if route["domainId"] == payload["domainId"] and not self.drop_writes:
                    route.update(payload)
            return payload
        if path == "/api/domain.delete":
            self.routes = [
                route for route in self.routes if route["domainId"] != payload["domainId"]
            ]
            return {}
        raise AssertionError(path)


class ProductPublicHostsTests(unittest.IsolatedAsyncioTestCase):
    current_store: ClassVar[ContextVar[PostgresRecordStore]]
    app: ClassVar[FastAPI]

    @classmethod
    def setUpClass(cls) -> None:
        cls.current_store = ContextVar("public_hosts_store")
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [identity().workflow_ref],
                        "event_names": ["pull_request"],
                        "products": [PRODUCT],
                        "actions": ["product_config.plan", "product_config.apply"],
                    }
                ]
            }
        )
        cls.app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=policy,
            record_store_factory=lambda: cls.current_store.get(),
        )

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.temp.name) / "state.db")
        )
        self.store.ensure_schema()
        token = self.current_store.set(self.store)
        self.addCleanup(self.current_store.reset, token)
        payload = _odoo_preview_profile_payload()
        payload.update(
            product=PRODUCT,
            repository="example/cm-website",
            lanes=[{"context": CONTEXT, "instance": "prod"}],
        )
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.target = DokployTargetRecord(
            context=CONTEXT,
            instance="prod",
            target_type="compose",
            domains=("cm-website-prod",),
            updated_at="2026-10-08T22:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context=CONTEXT,
            instance="prod",
            target_id="cm-prod-compose",
            updated_at=self.target.updated_at,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        self.provider = StubProvider()
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.example", "private-token"),
            )
        )
        self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider.call)
        )

    async def config(
        self, hosts: object = HOSTS, *, mode: str = "dry-run", key: str = "", **overrides: object
    ) -> Response:
        payload = {
            "mode": mode,
            "product": PRODUCT,
            "context": CONTEXT,
            "instance": "prod",
            "public_hosts": hosts,
            **overrides,
        }
        return await request(
            self.app,
            "POST",
            "/v1/product-config/apply",
            payload=payload,
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
        )

    def recorded(self) -> DokployTargetRecord:
        return self.store.read_dokploy_target_record(context_name=CONTEXT, instance_name="prod")

    async def test_cm_dry_run_apply_readback_replay_and_noop(self) -> None:
        dry = await self.config()
        self.assertEqual(dry.status_code, 202, dry.text)
        diff = dry.json()["result"]["public_hosts"]
        self.assertEqual(diff["added"], HOSTS)
        self.assertEqual(diff["removed"], [])
        self.assertFalse(diff["verified"])
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)
        response = await self.config(mode="apply", key="first")
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]["public_hosts"]
        self.assertTrue(result["verified"])
        self.assertEqual(result["read_back_hosts"], HOSTS)
        self.assertEqual(self.recorded().public_hosts, tuple(HOSTS))
        self.assertEqual(set(self.recorded().domains), {"cm-website-prod", *HOSTS})
        self.assertEqual(self.provider.routes[0]["domainId"], "internal")
        for route in self.provider.routes[1:]:
            self.assertEqual(
                (route["serviceName"], route["port"], route["https"], route["certificateType"]),
                ("web", 8069, True, "none"),
            )
        writes = list(self.provider.writes)
        replay = await self.config(mode="apply", key="first")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.provider.writes, writes)
        await self.config()
        noop = await self.config(mode="apply", key="noop")
        self.assertEqual(noop.status_code, 202, noop.text)
        self.assertEqual(noop.json()["result"]["public_hosts"]["unchanged"], HOSTS)
        self.assertEqual(self.provider.writes, writes)

    async def test_drop_only_a_previously_managed_name_and_preserve_origin(self) -> None:
        await self.config()
        await self.config(mode="apply", key="add")
        dry = await self.config([HOSTS[0]])
        self.assertEqual(dry.json()["result"]["public_hosts"]["removed"], [HOSTS[1]])
        response = await self.config([HOSTS[0]], mode="apply", key="remove")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            {route["host"] for route in self.provider.routes}, {"cm-website-prod", HOSTS[0]}
        )
        await self.config([])
        response = await self.config([], mode="apply", key="clear")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.recorded().domains, ("cm-website-prod",))

    async def test_stale_provider_diff_requires_new_dry_run(self) -> None:
        await self.config()
        self.provider.routes[0]["port"] = 8080
        response = await self.config(mode="apply", key="stale")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])
        await self.config()
        response = await self.config(mode="apply", key="fresh")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(all(route["port"] == 8080 for route in self.provider.routes))

    async def test_partial_failure_keeps_records_and_success_receipt_uncommitted(self) -> None:
        await self.config()
        self.provider.fail_host = HOSTS[1]
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.recorded(), self.target)
        self.provider.fail_host = ""
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 409, response.text)
        await self.config()
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.provider.routes), 3)

    async def test_readback_mismatch_is_not_success(self) -> None:
        await self.config()
        self.provider.drop_writes = True
        response = await self.config(mode="apply", key="ignored")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.recorded(), self.target)

    async def test_binding_changed_at_commit_refuses_before_provider_write(self) -> None:
        await self.config()
        original = self.store.write_product_public_hosts_bundle

        def changed(
            bundle: ProductAuthorityBundle,
            *,
            expected_target: DokployTargetRecord,
            expected_target_id: DokployTargetIdRecord,
            expected_provider_target: ProviderTargetRecord,
            apply_provider: Callable[[], None],
        ) -> None:
            self.store.write_dokploy_target_id_record(
                self.target_id.model_copy(update={"target_id": "replacement-compose"})
            )
            original(
                bundle,
                expected_target=expected_target,
                expected_target_id=expected_target_id,
                expected_provider_target=expected_provider_target,
                apply_provider=apply_provider,
            )

        with patch.object(self.store, "write_product_public_hosts_bundle", side_effect=changed):
            response = await self.config(mode="apply", key="binding-changed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)

    async def test_target_shared_with_another_lane_refuses_before_provider_write(self) -> None:
        await self.config()
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "another-product"})
        )
        response = await self.config(mode="apply", key="shared")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])

    async def test_scope_validation_and_unreviewed_apply_never_write(self) -> None:
        for overrides in ({"instance": "testing"}, {"context": "foreign"}, {"context": ""}):
            with self.subTest(overrides=overrides):
                response = await self.config(**overrides)
                self.assertIn(response.status_code, (400, 403), response.text)
        for hosts in (
            ["https://cellmechanic.com"],
            ["*.cellmechanic.com"],
            ["x:80"],
            ["a.example", "A.example"],
            None,
        ):
            with self.subTest(hosts=hosts):
                response = await self.config(hosts)
                self.assertEqual(response.status_code, 400, response.text)
        response = await self.config(mode="apply", key="unreviewed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])

    async def test_internal_name_cannot_become_managed_or_remove_foreign_service(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"domains": ("origin.example",)})
        )
        self.provider.routes[0]["host"] = "origin.example"
        response = await self.config(["origin.example"])
        self.assertEqual(response.status_code, 400, response.text)
        self.provider.routes.append(
            {
                **self.provider.routes[0],
                "host": HOSTS[0],
                "domainId": "foreign",
                "serviceName": "admin",
            }
        )
        response = await self.config()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, [])
