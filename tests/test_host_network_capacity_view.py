"""Host capacity is opt-in, scoped to the authorized lane, and never inferred."""

from pathlib import Path
from tempfile import TemporaryDirectory
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
