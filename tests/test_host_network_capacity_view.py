"""Host capacity is opt-in, scoped to the authorized lane, and never inferred."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch
import unittest

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.host_network_capacity import BridgeNetworkCapacity
from control_plane.dokploy.network_capacity import BRIDGE_NETWORK_CAPACITY_MARGIN
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier
from tests.support.http import get
from tests.test_http_app_product_operational_readiness import (
    _identity_for_readiness,
    _policy,
    _profile,
)


class HostCapacityViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_recipient_read_routes_preserve_typed_capacity_and_provider_denials(self) -> None:
        from unittest.mock import AsyncMock
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        from control_plane.dokploy.network_capacity import BridgeNetworkCapacityRefused
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
        from control_plane.http_routes.production_recipient import (
            register_production_recipient_routes,
        )
        from control_plane.http_routes.testing_activation import register_testing_activation_routes
        from control_plane.service_auth import LocalOperatorIdentity
        from tests.support.http import request

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            identity = LocalOperatorIdentity("fixture", "fixture", director_agent=True)

            def http_error(**kwargs: object) -> HTTPException:
                return HTTPException(
                    status_code=int(str(kwargs["status_code"])), detail={"code": kwargs["code"]}
                )

            dependencies = ReadRouteDependencies(
                read_identity=lambda: identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: "fixture",
                authorization_allows=lambda **kwargs: True,
                http_error=http_error,
                error_response_model=BaseModel,
            )
            app = FastAPI()
            mutate = AsyncMock()
            register_production_recipient_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=dependencies,
                mutation_identity=lambda: identity,
                actor=lambda _: "fixture",
                root=Path(directory),
                run_provider_mutation=mutate,
            )
            register_testing_activation_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=dependencies,
                mutation_identity=lambda: identity,
                actor=lambda _: "fixture",
                root=Path(directory),
                run_provider_mutation=mutate,
            )
            common = {
                "context": "fixture",
                "mode": "readback",
                "source_commit": "a" * 40,
                "volume_keys": {"database": "DB_VOLUME", "config": "CONFIG_VOLUME"},
                "reason": "Read original custody",
            }
            production = {
                **common,
                "destination_server_id": "host",
                "ports": {"PORT": 9000},
                "application_service": "web",
                "database_service": "database",
                "service_limits": {
                    service: {"cpus": 1, "memory_bytes": 1024**3} for service in ("web", "database")
                },
            }
            testing = {
                **common,
                "preparation_key": "original",
                "interlock_record_sha256": "b" * 64,
                "port_keys": ["PORT"],
            }
            from control_plane.production_recipient import ROUTE as production_route
            from control_plane.testing_activation import ROUTE as testing_route

            for route, adapter, body in (
                (
                    production_route,
                    "control_plane.production_recipient.ProductionRecipientAdapter.current",
                    production,
                ),
                (
                    testing_route,
                    "control_plane.testing_activation.TestingActivationAdapter.current",
                    testing,
                ),
            ):
                for error, status, code in (
                    (
                        BridgeNetworkCapacityRefused("host_bridge_network_capacity_short"),
                        409,
                        "host_bridge_network_capacity_short",
                    ),
                    (
                        BridgeNetworkCapacityRefused("host_bridge_network_capacity_unavailable"),
                        409,
                        "host_bridge_network_capacity_unavailable",
                    ),
                    (
                        NativeSourceAuthorizationDenied("private provider detail"),
                        403,
                        "authorization_denied",
                    ),
                ):
                    with self.subTest(route=route, code=code), patch(adapter, side_effect=error):
                        response = await request(
                            app, "POST", route.format(product="fixture"), payload=body
                        )
                        self.assertEqual(response.status_code, status, response.text)
                        self.assertEqual(response.json()["detail"]["code"], code)
                        self.assertNotIn("private provider detail", response.text)
            mutate.assert_not_called()

    async def test_capacity_read_uses_lane_host_and_authorization_precedes_provider(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = _profile()
            store.write_product_profile_record(profile)
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context=profile.product,
                    instance="testing",
                    provider_id="dokploy",
                    target_category="compose",
                    provider_target_type="compose",
                    target_id="lane-target",
                    display_name="Fixture lane",
                    updated_at="2026-10-20T12:00:00Z",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=Path(directory),
            )
            route = f"/v1/products/{profile.product}/environments/testing"
            with (
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://provider.invalid", "fixture-token"),
                ),
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={"composeId": "lane-target", "serverId": "lane-host"},
                ) as target,
                patch(
                    "control_plane.dokploy.network_capacity.read_bridge_network_capacity",
                    return_value=BridgeNetworkCapacity(
                        total_slots=16, free_slots=BRIDGE_NETWORK_CAPACITY_MARGIN
                    ),
                ) as reader,
            ):
                plain = await get(app, route, headers={"Authorization": "Bearer valid-token"})
                self.assertEqual(plain.status_code, 200)
                reader.assert_not_called()
                target.assert_not_called()
                observed = await get(
                    app,
                    route + "?include_host_network_capacity=true",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(observed.status_code, 200)
                observation = observed.json()["host_network_capacity"]
                self.assertEqual(observation["server_id"], "lane-host")
                self.assertEqual(
                    observation["capacity"]["free_slots"], BRIDGE_NETWORK_CAPACITY_MARGIN
                )
                self.assertEqual(observation["status"], "short")
                self.assertTrue(observation["observed_at"])
                self.assertEqual(reader.call_args.kwargs["server_id"], "lane-host")
                from control_plane.dokploy.network_capacity import BridgeNetworkCapacityRefused
                from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

                for failure, status in (
                    (
                        BridgeNetworkCapacityRefused("host_bridge_network_capacity_unavailable"),
                        "unavailable",
                    ),
                    (NativeSourceAuthorizationDenied("private fixture detail"), "denied"),
                ):
                    reader.side_effect = failure
                    missing = await get(
                        app,
                        route + "?include_host_network_capacity=true",
                        headers={"Authorization": "Bearer valid-token"},
                    )
                    evidence = missing.json()["host_network_capacity"]
                    self.assertEqual(evidence["status"], status)
                    self.assertIsNone(evidence["capacity"])
                    self.assertNotIn("private fixture detail", str(evidence))
                reader.side_effect = None
                target.side_effect = [
                    {"composeId": "lane-target", "serverId": "lane-host"},
                    {"composeId": "lane-target", "serverId": "moved-host"},
                ]
                moved = await get(
                    app,
                    route + "?include_host_network_capacity=true",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(moved.json()["host_network_capacity"]["status"], "unavailable")
                self.assertIsNone(moved.json()["host_network_capacity"]["capacity"])
                reader.reset_mock()
                target.reset_mock()
                refused = await get(
                    app,
                    route.replace("testing", "prod") + "?include_host_network_capacity=true",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertIn(refused.status_code, (403, 404))
                reader.assert_not_called()
                target.assert_not_called()
