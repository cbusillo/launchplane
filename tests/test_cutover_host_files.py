import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.dokploy.cutover_host_files import ProductHostFileRoute, ProductHostPreparedSites
from control_plane.dokploy.prepared_host import (
    PreparedSiteDirectory,
    bundle_name,
    prepared_bundle_text,
    prepared_directory_app,
)
from control_plane.dokploy.traefik_cutover import TraefikRouteTarget, route_payload
from control_plane.prepared_public_site import prepare_public_site
from tests.support.fake_product_host import FakeProductHost
from tests.support.http import lifespan_client, request
from tests.support.prepared_public_fixture import WebsiteFixture

OLD = "http://old:8069"
NEW = "http://new:8069"


def document(target: TraefikRouteTarget, backend: str) -> object:
    return json.loads(json.dumps(route_payload(target, backend)))


class ProductHostRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = FakeProductHost(Path(temporary.name).resolve())
        self.target = TraefikRouteTarget(
            route_id="lane",
            host="fixture.invalid",
            entrypoints=("web",),
            route_file=self.host.route_file,
            api_url="http://127.0.0.1:8080",
            tls=False,
        )
        self.host.route_file.write_text(json.dumps(route_payload(self.target, OLD), sort_keys=True))
        self.guard = Mock()
        self.route = ProductHostFileRoute(
            self.target,
            reader=self.host.read,
            execute=self.host.execute,
            lock_directory=str(self.host.authority),
            guard=self.guard,
        )

    def test_switch_writes_the_product_host_file_and_returns_traefik_applied_backend(self) -> None:
        self.route.verify_watch()
        self.assertEqual(self.route.observed_target(), OLD)
        self.assertEqual(self.route.publish(NEW, expected=(OLD,)), NEW)
        self.assertEqual(json.loads(self.host.route_file.read_text()), document(self.target, NEW))
        self.assertEqual(self.route.observed_target(), NEW)
        self.assertEqual(len(self.host.effects), 1)
        self.assertEqual(list(self.host.dynamic.glob(".cutover-*")), [])

    def test_publication_rechecks_lane_guard_before_any_host_effect(self) -> None:
        self.guard.side_effect = ValueError("bound lane authority changed")
        with self.assertRaises(ValueError):
            self.route.publish(NEW, expected=(OLD,))
        self.assertEqual(self.host.effects, [])
        self.assertEqual(json.loads(self.host.route_file.read_text()), document(self.target, OLD))

    def test_host_write_refuses_a_predecessor_that_changed_after_admission(self) -> None:
        admitted = self.host.route_file.read_text()
        self.host.route_file.write_text(json.dumps(route_payload(self.target, "http://other:8069")))
        changed = self.host.route_file.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale traffic switch"):
            self.route._replace_owned(json.dumps(route_payload(self.target, NEW)), admitted)
        self.assertEqual(self.host.route_file.read_bytes(), changed)

    def test_delayed_switch_cannot_apply_after_the_route_returns_to_its_bytes(self) -> None:
        delayed = ProductHostFileRoute(
            self.target,
            reader=self.host.read,
            execute=self.host.execute,
            lock_directory=str(self.host.authority),
            guard=Mock(),
        )
        admitted = delayed._read_owned()
        self.route.publish(NEW, expected=(OLD,))
        self.route.publish(OLD, expected=(NEW,))
        self.assertEqual(self.host.route_file.read_text(), admitted)
        with self.assertRaisesRegex(ValueError, "stale traffic switch"):
            delayed._replace_owned(
                json.dumps(route_payload(self.target, "http://obsolete:8069")), admitted
            )
        self.assertEqual(self.route.observed_target(), OLD)

    def test_delayed_switch_cannot_apply_after_a_no_op_recovery_publication(self) -> None:
        delayed = ProductHostFileRoute(
            self.target,
            reader=self.host.read,
            execute=self.host.execute,
            lock_directory=str(self.host.authority),
            guard=Mock(),
        )
        admitted = delayed._read_owned()
        self.assertEqual(self.route.publish(OLD, expected=(NEW,)), OLD)
        with self.assertRaisesRegex(ValueError, "stale traffic switch"):
            delayed._replace_owned(
                json.dumps(route_payload(self.target, NEW), sort_keys=True), admitted
            )
        self.assertEqual(self.route.observed_target(), OLD)

    def test_watch_proof_refuses_a_directory_the_proxy_does_not_watch(self) -> None:
        proxy = self.host.state["containers"]["dokploy-traefik"]
        original_mounts = proxy["Mounts"]
        proxy["Mounts"] = [{**original_mounts[0], "Source": str(self.host.authority)}]
        self.host.save()
        with self.assertRaises(Exception):
            self.route.verify_watch()
        proxy["Mounts"] = original_mounts
        self.host.state["proxy_files"]["/etc/traefik/traefik.yml"] = json.dumps(
            {"providers": {"file": {"directory": "/etc/dokploy/traefik/dynamic", "watch": False}}}
        )
        self.host.save()
        with self.assertRaisesRegex(ValueError, "does not watch"):
            self.route.verify_watch()


class ProductHostPreparedSitesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        fixture = WebsiteFixture(root)
        self.addCleanup(fixture.engine.dispose)
        self.site = prepare_public_site(fixture.plan, fixture)
        self.host = FakeProductHost(root)

    def sites(self) -> ProductHostPreparedSites:
        return ProductHostPreparedSites(
            reader=self.host.read,
            execute=self.host.execute,
            volume=self.host.volume,
            site="lane",
            lock_directory=str(self.host.authority),
        )

    async def test_staged_bundle_lives_in_the_product_volume_the_retained_server_reads(
        self,
    ) -> None:
        self.sites().publish(self.site, "original-operation")
        replacement = self.sites()
        self.assertEqual(replacement.load("original-operation"), self.site)
        self.assertEqual(replacement.active_pause(), "original-operation")
        served = self.host.volume_data / "lane"
        self.assertEqual(PreparedSiteDirectory(served).load("original-operation"), self.site)
        async with lifespan_client(prepared_directory_app(served)) as client:
            for resource in self.site.resources:
                response = await request(client, "GET", resource.path)
                self.assertEqual(response.content, base64.b64decode(resource.body_base64))
                self.assertEqual(response.headers["x-launchplane-pause-id"], "original-operation")

    async def test_restaging_keeps_the_original_immutable_bundle(self) -> None:
        sites = self.sites()
        sites.publish(self.site, "original-operation")
        original = (self.host.volume_data / "lane").joinpath(
            PreparedSiteDirectory(self.host.volume_data / "lane").path("original-operation").name
        )
        before = original.read_bytes()
        sites.publish(self.site, "original-operation")
        foreign = self.site.model_copy(update={"content_digest": "unverified"})
        with self.assertRaises(ValueError):
            sites.publish(foreign, "original-operation")
        self.assertEqual(original.read_bytes(), before)

    async def test_delayed_activation_and_retention_cannot_change_a_newer_pause(self) -> None:
        sites = self.sites()
        sites.publish(self.site, "original-operation")
        admitted_generation = int(sites._read_frame("active.json")["generation"])
        sites.publish(self.site, "rollback-operation")
        with self.assertRaises(Exception):
            sites._run(
                sites.execute,
                "activate",
                name=bundle_name("original-operation"),
                content=json.dumps("original-operation"),
                expected_generation=admitted_generation,
            )
        with self.assertRaises(Exception):
            sites._run(
                sites.execute,
                "prune",
                remove=[],
                deactivate=None,
                expected_generation=admitted_generation,
            )
        self.assertEqual(sites.active_pause(), "rollback-operation")
        self.assertEqual(sites.load("original-operation"), self.site)

    async def test_delayed_activation_cannot_point_at_a_bundle_retention_removed(self) -> None:
        sites = self.sites()
        admitted_generation = int(sites._read_frame("active.json")["generation"])
        sites._run(
            sites.execute,
            "stage",
            name=bundle_name("aborted-operation"),
            content=prepared_bundle_text(self.site, "aborted-operation"),
        )
        sites.prune({"aborted-operation": self.site}, protected=set(), serving_prepared=False)
        with self.assertRaises(Exception):
            sites._run(
                sites.execute,
                "activate",
                name=bundle_name("aborted-operation"),
                content=json.dumps("aborted-operation"),
                expected_generation=admitted_generation,
            )
        with self.assertRaises(FileNotFoundError):
            sites.active_pause()

    async def test_volume_outside_the_product_daemon_root_is_refused(self) -> None:
        self.host.state["volumes"][self.host.volume]["Mountpoint"] = str(self.host.authority)
        self.host.save()
        with self.assertRaises(Exception):
            self.sites().publish(self.site, "original-operation")
        self.assertEqual(self.host.effects[1:], [])
        self.assertFalse((self.host.authority / "lane").exists())

    async def test_retention_requires_durable_reconstruction_and_preserves_held_publication(
        self,
    ) -> None:
        sites = self.sites()
        sites.publish(self.site, "old-terminal")
        sites.publish(self.site, "held")
        known = {"old-terminal": self.site, "held": self.site}
        sites.prune(known, protected={"held"}, serving_prepared=True)
        with self.assertRaises(FileNotFoundError):
            sites.load("old-terminal")
        self.assertEqual(sites.load("held"), self.site)
        self.assertEqual(sites.active_pause(), "held")
        corrupted = self.site.model_copy(update={"content_digest": "foreign"})
        with self.assertRaises(ValueError):
            sites.prune({"held": corrupted}, protected=set(), serving_prepared=False)
        self.assertEqual(sites.load("held"), self.site)
        sites.prune(known, protected=set(), serving_prepared=False)
        with self.assertRaises(FileNotFoundError):
            sites.load("held")
        with self.assertRaises(FileNotFoundError):
            sites.active_pause()
