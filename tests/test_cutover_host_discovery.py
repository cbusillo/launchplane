import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_host import BoundOdooCutoverHost, DokployCutoverHost
from control_plane.dokploy.traefik_cutover import TraefikRouteTarget, route_payload
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class CutoverHostDiscoveryTests(unittest.TestCase):
    def test_installed_provider_discovery_has_no_guarded_effect_before_admission(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = WebsiteFixture(root)
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            route_file = root / "route.yaml"
            target = TraefikRouteTarget(
                route_id=plan.route_id,
                host=plan.host,
                route_file=route_file,
                api_url="http://proxy.invalid",
                entrypoints=("web",),
                tls=False,
            )
            route_file.write_text(json.dumps(route_payload(target, plan.old.private_url)))
            runtime = SimpleNamespace(
                public_origin="http://" + plan.host, private_network="private", data_volume="data"
            )
            payloads = {
                slot.slot_id: {
                    "appName": slot.slot_id,
                    "serverId": "host",
                    "autoDeploy": False,
                    "env": "ODOO_DB_HOST=database\nODOO_DB_USER=inert\nODOO_DB_PASSWORD=inert\n",
                }
                for slot in (plan.old, plan.new)
            }
            transport = Mock(
                side_effect=AssertionError("discovery cannot dispatch an admitted effect")
            )
            transport.server_id = "host"
            native = Mock()
            bound: Any = SimpleNamespace(
                plan=plan,
                lane=SimpleNamespace(
                    prepared_origin="http://prepared.invalid", route_id=plan.route_id
                ),
                runtime=runtime,
                state=SimpleNamespace(verify_execution=Mock()),
                prepared_server=Mock(),
                payload=lambda slot: payloads[slot.slot_id],
                payloads=payloads,
                transport=transport,
                store=SimpleNamespace(
                    read_product_profile_record=lambda product: SimpleNamespace(
                        production_use="prelaunch"
                    )
                ),
                route=SimpleNamespace(target=target, observed_target=lambda: plan.old.private_url),
                record=lambda: None,
                writers=SimpleNamespace(
                    targets=tuple(
                        SimpleNamespace(slot_id=plan.old.slot_id, writer=writer)
                        for writer in plan.writers
                    )
                ),
                containers=lambda slot, service: ("native",),
                target=lambda slot, writer: SimpleNamespace(container="native"),
                inspect=lambda slot, service, container: {
                    "NetworkSettings": {"Networks": {"private": {"IPAddress": "192.0.2.1"}}},
                    "Mounts": [{"Type": "volume", "Name": "data", "Destination": "/volumes/data"}],
                },
                observer=SimpleNamespace(
                    database=lambda: {"prepared_transactions": 0, "sessions": []}
                ),
                startup_capability=Mock(),
                native_writer_capability=native,
                startup_reader=Mock(),
                capabilities=Mock(return_value="bound-capabilities"),
            )
            bound.preflight = lambda current: BoundOdooCutoverHost.preflight(bound, current)
            with (
                patch(
                    "control_plane.dokploy.cutover_host.BoundOdooCutoverHost", return_value=bound
                ),
                patch("control_plane.dokploy.cutover_host.verify_shared_watched_directory"),
            ):
                result = DokployCutoverHost().odoo(
                    plan,
                    cast(
                        Any,
                        SimpleNamespace(
                            prepared_origin="http://prepared.invalid", route_id=plan.route_id
                        ),
                    ),
                    object(),
                    root,
                )
            self.assertEqual(result, "bound-capabilities")
            transport.assert_not_called()
            bound.startup_reader.preflight_candidate.assert_called_once()
            bound.guard = Mock()
            bound.runner = lambda slot: "runner"
            runtime.restore_image = "postgres@sha256:" + "a" * 64
            transport.read.return_value = "pg_dump (PostgreSQL) 17.1"
            transport.side_effect = RuntimeError("native restore tools unavailable")
            with patch("control_plane.dokploy.cutover_host.prepare_dokploy_odoo_slot") as prepare:
                with self.assertRaisesRegex(RuntimeError, "restore tools unavailable"):
                    BoundOdooCutoverHost.prepare_candidate(bound, plan)
            bound.guard.assert_called_once()
            prepare.assert_not_called()
