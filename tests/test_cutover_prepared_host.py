import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.dokploy.prepared_host import (
    PreparedSiteDirectory,
    prepared_directory_app,
    prepared_service_app,
)
from control_plane.prepared_public_site import prepare_public_site, snapshot_digest
from tests.support.http import lifespan_client, request
from tests.support.prepared_public_fixture import WebsiteFixture


class PreparedHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        fixture = WebsiteFixture(root)
        self.addCleanup(fixture.engine.dispose)
        self.site = prepare_public_site(fixture.plan, fixture)
        self.directory = root / "prepared"
        self.directory.mkdir()

    async def test_restart_serves_persisted_bytes_and_keeps_submissions_paused(self) -> None:
        publisher = PreparedSiteDirectory(self.directory)
        publisher.publish(self.site, "original-operation")
        restarted = PreparedSiteDirectory(self.directory)
        self.assertEqual(restarted.load("original-operation"), self.site)
        async with lifespan_client(prepared_directory_app(self.directory)) as client:
            for resource in self.site.resources:
                response = await request(client, "GET", resource.path)
                self.assertEqual(response.status_code, resource.status)
                self.assertEqual(response.content, base64.b64decode(resource.body_base64))
                self.assertEqual(
                    response.headers["x-launchplane-public-copy"], self.site.content_digest
                )
            response = await request(client, "POST", "/contact", raw_body=b"inert submission")
            self.assertEqual(response.status_code, 423)

    async def test_lost_publication_reply_replays_exact_bundle_and_corruption_fails_closed(
        self,
    ) -> None:
        publisher = PreparedSiteDirectory(self.directory)
        publisher.publish(self.site, "original-operation")
        publisher.publish(self.site, "original-operation")
        foreign = self.site.model_copy(update={"content_digest": "unverified"})
        with self.assertRaises(ValueError):
            publisher.publish(foreign, "original-operation")
        self.assertEqual(publisher.load("original-operation"), self.site)
        publisher.path("original-operation").write_text("{}")
        async with lifespan_client(prepared_directory_app(self.directory)) as client:
            response = await request(client, "GET", "/")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("x-launchplane-public-copy", response.headers)

    async def test_catalog_selects_exact_http_host_across_private_origin_ports(self) -> None:
        site = self.site.model_copy(
            update={
                "plan": self.site.plan.model_copy(update={"origin": "http://fixture.invalid:8080"}),
                "content_digest": "",
            }
        )
        site = site.model_copy(update={"content_digest": snapshot_digest(site)})
        lane = self.directory / "lane"
        lane.mkdir()
        PreparedSiteDirectory(lane).publish(site, "original-operation")
        with patch.dict("os.environ", {"LAUNCHPLANE_PREPARED_PUBLIC_ROOT": str(self.directory)}):
            app = prepared_service_app()
        async with lifespan_client(app) as client:
            response = await request(client, "GET", "/", headers={"Host": "FIXTURE.INVALID"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-launchplane-pause-id"], "original-operation")
            foreign = await request(client, "GET", "/", headers={"Host": "foreign.invalid"})
            self.assertEqual(foreign.status_code, 503)
            self.assertNotIn("x-launchplane-public-copy", foreign.headers)
