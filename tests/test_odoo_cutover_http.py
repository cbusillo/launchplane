"""The native fixture preserves HTTP header semantics and strict public evidence."""

from contextlib import nullcontext
from http.client import HTTPMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from control_plane.prepared_public_site import prepare_public_site
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan
from tests import test_odoo_cutover_rehearsal as rehearsal


class CutoverHTTPTests(unittest.TestCase):
    def test_header_case_preserves_capture_mode_and_public_copy_evidence(self) -> None:
        for transform in (str.lower, str.title, str.upper):
            with self.subTest(transform=transform), TemporaryDirectory() as directory:
                website = WebsiteFixture(Path(directory))
                self.addCleanup(website.engine.dispose)
                fixture = rehearsal.OdooCutoverRehearsalTests()
                fixture.plan = release_plan(website)
                fixture.old_origin = fixture.public = "http://fixture.invalid"
                fixture.record_id = "pause"
                fixture.stop = threading.Event()
                fixture.samples, fixture.errors = [], []
                headers = HTTPMessage()
                for key, value in (
                    ("content-type", "text/html; charset=utf-8"),
                    ("x-launchplane-served-mode", "prepared-public"),
                    ("x-launchplane-public-copy", "pause"),
                ):
                    headers[transform(key)] = value
                response = SimpleNamespace(
                    status=200,
                    read=lambda: b"<html><body>Public page</body></html>",
                    headers=headers,
                )
                opener = SimpleNamespace(open=lambda *args, **kwargs: nullcontext(response))
                with patch.object(rehearsal, "build_opener", return_value=opener):
                    self.assertEqual(fixture.capture("/").content_type, "text/html")
                    self.assertEqual(fixture.observe_prepared("pause"), "pause")
                    with patch.object(
                        fixture.stop, "wait", side_effect=lambda timeout: fixture.stop.set()
                    ):
                        fixture.read_loop()
                self.assertEqual(
                    fixture.samples, ["prepared-public:<html><body>Public page</body></html>"]
                )
                self.assertEqual(fixture.errors, [])

    def test_missing_or_non_html_content_type_still_refuses_public_page(self) -> None:
        for content_type in (None, "application/json"):
            with self.subTest(content_type=content_type), TemporaryDirectory() as directory:
                website = WebsiteFixture(Path(directory))
                self.addCleanup(website.engine.dispose)
                fixture = rehearsal.OdooCutoverRehearsalTests()
                fixture.plan = release_plan(website)
                fixture.old_origin = "http://fixture.invalid"
                headers = HTTPMessage()
                if content_type is not None:
                    headers["content-type"] = content_type
                response = SimpleNamespace(
                    status=200, read=lambda: b"<html>Public page</html>", headers=headers
                )
                opener = SimpleNamespace(open=lambda *args, **kwargs: nullcontext(response))
                plan = website.plan.model_copy(
                    update={"public_routes": ("/",), "retained_assets": ()}
                )
                with (
                    patch.object(rehearsal, "build_opener", return_value=opener),
                    self.assertRaisesRegex(ValueError, "must be HTML"),
                ):
                    prepare_public_site(plan, fixture)
