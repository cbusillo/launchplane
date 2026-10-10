import base64
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.prepared_public_site import (
    CapturedPublicResponse,
    PublicPauseRecord,
    PublicSitePlan,
)
from control_plane.prepared_public_pause import PreparedPublicPause
from control_plane.prepared_public_site import prepare_public_site, prepared_public_app
from control_plane.prepared_public_storage import (
    FilePublicPauseStore,
    load_prepared_site,
    save_prepared_site,
)
from tests.support.http import lifespan_client, request
from tests.support.prepared_public_fixture import WebsiteFixture


class PreparedPublicSiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = WebsiteFixture(self.root)
        self.addCleanup(self.fixture.engine.dispose)
        self.site = prepare_public_site(self.fixture.plan, self.fixture)
        self.store = FilePublicPauseStore(self.root / "state")

    async def test_public_routes_assets_and_redirects_survive_database_update_and_gc(self) -> None:
        path = self.root / "state" / "copy.json"
        save_prepared_site(path, self.site)
        site = load_prepared_site(path)
        pause = PreparedPublicPause(site, self.store, self.fixture)
        pause.begin()
        self.fixture.update_database()
        self.fixture.engine.dispose()
        self.fixture.database.unlink()
        before = tuple(self.fixture.capture_paths)
        async with lifespan_client(prepared_public_app(site)) as client:
            for resource in site.resources:
                response = await request(client, "GET", resource.path)
                self.assertEqual(response.status_code, resource.status)
                self.assertEqual(response.content, base64.b64decode(resource.body_base64))
                self.assertNotIn("set-cookie", response.headers)
                self.assertNotIn("x-csrf-token", response.headers)
                self.assertEqual(
                    response.headers["x-launchplane-reduced-service"], "public-writes-paused"
                )
            page = await request(client, "GET", "/prices/repair")
            self.assertIn("Representative repair prices", page.text)
            self.assertIn(site.plan.notice, page.text)
            self.assertNotIn("secret-csrf", page.text)
            self.assertNotIn("<script", page.text)
            self.assertNotIn("<form", page.text)
            self.assertIn("disabled", page.text)
            redirect = await request(client, "GET", "/old-prices")
            self.assertEqual(redirect.headers["location"], "/prices/repair")
        self.assertEqual(tuple(self.fixture.capture_paths), before)

    async def test_direct_js_and_personal_requests_never_reach_authority(self) -> None:
        before = tuple(self.fixture.capture_paths)
        app = prepared_public_app(self.site)
        async with lifespan_client(app) as client:
            for method, path, headers in (
                ("POST", "/contact", {}),
                ("POST", "/metrics", {}),
                ("PUT", "/web/editor", {}),
                ("DELETE", "/assets/old.css", {}),
                ("GET", "/my", {}),
                ("GET", "/editor/page", {}),
                ("GET", "/my", {"Cookie": "session_id=personal"}),
                ("GET", "/editor/page", {"Authorization": "Bearer personal"}),
            ):
                response = await request(
                    client, method, path, headers=headers, raw_body=b"secret-csrf"
                )
                self.assertEqual(response.status_code, 423)
                self.assertIn("paused", response.text)
            response = await request(client, "GET", "/?editor=1")
            self.assertEqual(response.status_code, 404)
            anonymous = await request(client, "GET", "/")
            for headers in (
                {"Cookie": "session_id=personal"},
                {"Authorization": "Bearer personal"},
            ):
                public = await request(client, "GET", "/", headers=headers)
                self.assertEqual(public.status_code, 200)
                self.assertEqual(public.content, anonymous.content)
                self.assertNotIn("set-cookie", public.headers)
        self.assertEqual(tuple(self.fixture.capture_paths), before)

    def test_incomplete_coverage_fails_before_any_writer_pause(self) -> None:
        original = self.fixture.capture
        for fault in ("missing", "unlisted", "personal", "binding", "cycle", "external", "encoded"):
            with self.subTest(fault=fault):

                def capture(path: str) -> CapturedPublicResponse:
                    response = original(path)
                    if fault == "missing" and path == "/assets/bg.png":
                        return response.model_copy(update={"status": 404})
                    if fault == "personal":
                        return response.model_copy(update={"headers": {"Cache-Control": "private"}})
                    if fault == "binding":
                        return response.model_copy(
                            update={
                                "binding": response.binding.model_copy(update={"database": "wrong"})
                            }
                        )
                    if fault == "cycle" and path == "/old-prices":
                        return response.model_copy(update={"headers": {"Location": "/old-prices"}})
                    if path == "/" and fault in {"unlisted", "external", "encoded"}:
                        replacement = {
                            "unlisted": b'<a href="/not-in-sitemap">Missing price page</a>',
                            "external": b'<img src="https://remote.invalid/a.png">',
                            "encoded": b'<img src="/assets/%62g.png">',
                        }[fault]
                        return response.model_copy(
                            update={
                                "body": response.body.replace(b"</main>", replacement + b"</main>")
                            }
                        )
                    return response

                self.fixture.capture = capture  # type: ignore[method-assign]
                with self.assertRaises(ValueError):
                    prepare_public_site(self.fixture.plan, self.fixture)
                self.assertEqual(self.fixture.fenced, set())
                self.assertEqual(self.fixture.published, "")
                self.fixture.capture = original  # type: ignore[method-assign]

    def test_pause_requires_publication_and_every_writer_drained(self) -> None:
        for bad_writer in self.fixture.plan.writers:
            self.fixture.active[bad_writer.writer_id] = 1
            pause = PreparedPublicPause(self.site, self.store, self.fixture)
            with self.assertRaisesRegex(ValueError, "fenced and drained"):
                pause.begin()
            self.fixture.active[bad_writer.writer_id] = 0
        self.fixture.bad_publication = True
        with self.assertRaisesRegex(ValueError, "publication"):
            PreparedPublicPause(self.site, self.store, self.fixture).begin()
        record = self.store.load(self.fixture.published_pause_id)
        self.assertEqual(record.served_mode, "unverified")
        with self.assertRaisesRegex(ValueError, "observed"):
            PreparedPublicPause(self.site, self.store, self.fixture).drain(record.pause_id)

    def test_real_writers_pause_and_full_interval_survives_controller_restart(self) -> None:
        for writer in self.fixture.plan.writers:
            self.assertTrue(self.fixture.write(writer.writer_id))
        began = datetime.now(UTC)
        clock_values = iter((began, began + timedelta(seconds=17)))

        def clock() -> datetime:
            return next(clock_values)

        pause = PreparedPublicPause(self.site, self.store, self.fixture, clock=clock)
        record = pause.begin()
        for writer in self.fixture.plan.writers:
            self.assertFalse(self.fixture.write(writer.writer_id))
        self.assertEqual(self.fixture.committed_write_count(), len(self.fixture.plan.writers))
        self.fixture.update_database()
        restarted = PreparedPublicPause(
            self.site, FilePublicPauseStore(self.root / "state"), self.fixture, clock=clock
        )
        self.fixture.fail_resume = True
        with self.assertRaises(RuntimeError):
            restarted.finish(record.pause_id, expected=self.fixture.binding)
        self.assertIsNone(self.store.load(record.pause_id).ended_at)
        self.fixture.fail_resume = False
        complete = restarted.finish(record.pause_id, expected=self.fixture.binding)
        self.assertEqual(complete.duration_seconds, 17)
        self.assertEqual(complete.began_at, began)
        self.assertTrue(complete.reduced_service)
        self.assertEqual(restarted.finish(record.pause_id, expected=self.fixture.binding), complete)
        self.assertEqual(self.fixture.resume_count, 1)
        for writer in self.fixture.plan.writers:
            self.assertTrue(self.fixture.write(writer.writer_id))

    def test_recovery_rejects_duplicate_owners_and_target_changes(self) -> None:
        pause = PreparedPublicPause(self.site, self.store, self.fixture)
        record = pause.begin()
        self.fixture.owner_count = 2
        with self.assertRaisesRegex(ValueError, "read back"):
            pause.finish(record.pause_id, expected=self.fixture.binding)
        self.assertEqual(self.store.load(record.pause_id).state, "resuming")
        changed = self.fixture.binding.model_copy(update={"artifact_id": "other-artifact"})
        with self.assertRaisesRegex(ValueError, "uncertain recovery"):
            pause.finish(record.pause_id, expected=changed)

    def test_retained_copy_tampering_is_rejected(self) -> None:
        path = self.root / "state" / "copy.json"
        save_prepared_site(path, self.site)
        path.write_text(path.read_text().replace("fixture-artifact", "wrong-artifact"))
        with self.assertRaisesRegex(ValueError, "changed"):
            load_prepared_site(path)

    def test_css_transitive_assets_and_limits_fail_closed(self) -> None:
        self.fixture.pages["/assets/site.css"] = (
            "text/css",
            b"body {background:url('/assets/missing.png')}",
        )
        with self.assertRaisesRegex(ValueError, "coverage"):
            prepare_public_site(self.fixture.plan, self.fixture)
        with self.assertRaisesRegex(ValueError, "budget"):
            prepare_public_site(
                self.fixture.plan.model_copy(update={"max_resources": 1}), self.fixture
            )

    def test_public_coverage_never_retains_session_or_csrf_query_tokens(self) -> None:
        for path in (
            "/?csrf_token=private",
            "/assets/a.js?session_id=private",
            "/web/image?access_token=private",
        ):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "credential parameters"),
            ):
                PublicSitePlan.model_validate(
                    {
                        **self.fixture.plan.model_dump(),
                        "public_routes": (path,),
                    }
                )

    def test_unknown_completion_reobserves_recovery_without_resuming_twice(self) -> None:
        pause = PreparedPublicPause(self.site, self.store, self.fixture)
        record = pause.begin()
        original_save = self.store.save

        def fail_completion(updated: PublicPauseRecord) -> None:
            if updated.state == "complete":
                raise OSError("fixture completion commit failed")
            original_save(updated)

        with patch.object(self.store, "save", side_effect=fail_completion):
            with self.assertRaises(OSError):
                pause.finish(record.pause_id, expected=self.fixture.binding)
        self.assertEqual(self.fixture.resume_count, 1)
        self.assertEqual(self.store.load(record.pause_id).state, "resuming")
        self.assertIsNone(self.store.load(record.pause_id).ended_at)
        recovered = PreparedPublicPause(self.site, self.store, self.fixture).finish(
            record.pause_id, expected=self.fixture.binding
        )
        self.assertEqual(recovered.state, "complete")
        self.assertEqual(self.fixture.resume_count, 1)

    async def test_css_imports_escapes_and_svg_remain_usable_with_local_closure(self) -> None:
        self.fixture.pages["/assets/site.css"] = (
            "text/css",
            b'/*! generated bundle */ @import "more.css"; .icon:before{content:"\\f015"} .hero{background:u\\72l("/assets/bg.png")}',
        )
        self.fixture.pages["/assets/more.css"] = (
            "text/css",
            b'.logo{background-image:image-set("logo.svg" 1x)}',
        )
        self.fixture.pages["/assets/logo.svg"] = (
            "image/svg+xml",
            b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="blue"/></svg>',
        )
        site = prepare_public_site(self.fixture.plan, self.fixture)
        self.assertIn("/assets/more.css", self.fixture.capture_paths)
        self.assertIn("/assets/logo.svg", self.fixture.capture_paths)
        self.fixture.engine.dispose()
        self.fixture.database.unlink()
        async with lifespan_client(prepared_public_app(site)) as client:
            for path in ("/assets/site.css", "/assets/more.css", "/assets/logo.svg"):
                response = await request(client, "GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content)

    def test_css_import_remote_and_executable_svg_fail_before_pause(self) -> None:
        for content_type, body in (
            ("text/css", b'@import url("https://untrusted.invalid/x.css");'),
            (
                "image/svg+xml",
                b'<svg xmlns="http://www.w3.org/2000/svg"><script>send()</script></svg>',
            ),
        ):
            self.fixture.pages["/assets/old.css"] = (content_type, body)
            with self.assertRaises(ValueError):
                prepare_public_site(self.fixture.plan, self.fixture)
            self.assertEqual(self.fixture.fenced, set())

    async def test_review_markup_cases_preserve_images_mobile_metadata_and_content_after_forms(
        self,
    ) -> None:
        original = self.fixture.capture
        viewport = "width=device-width, initial-scale=1"
        data_image = (
            "data:image/png;base64,"
            + base64.b64encode(self.fixture.pages["/assets/bg.png"][1]).decode()
        )

        def capture(path: str) -> CapturedPublicResponse:
            response = original(path)
            if path in self.fixture.plan.public_routes and response.status == 200:
                body = (
                    response.body.replace(
                        b"</head>",
                        (
                            '<meta charset="utf-8"><meta name="viewport" content="'
                            + viewport
                            + '">'
                            '<meta name="csrf-token" content="private-meta-token">'
                            '<meta http-equiv="refresh" content="0;url=/metrics"></head>'
                        ).encode(),
                    )
                    .replace(
                        b"</form>",
                        b'<embed src="ignored"></form><p>Public content after the form</p>',
                    )
                    .replace(
                        b'src="/assets/bg.png"',
                        ('src="' + data_image + '"').encode(),
                    )
                    .replace(
                        b"</main>",
                        b'<a href="//outside.invalid/public">External resource</a></main>',
                    )
                )
                return response.model_copy(update={"body": body})
            return response

        self.fixture.capture = capture  # type: ignore[method-assign]
        site = prepare_public_site(self.fixture.plan, self.fixture)
        self.fixture.engine.dispose()
        self.fixture.database.unlink()
        served = await request(prepared_public_app(site), "GET", "/")
        self.assertEqual(served.status_code, 200)
        self.assertIn(viewport, served.text)
        self.assertIn(data_image, served.text)
        self.assertIn("Public content after the form", served.text)
        self.assertIn("//outside.invalid/public", served.text)
        self.assertNotIn("private-meta-token", served.text)
        self.assertNotIn("http-equiv", served.text)
        self.assertNotIn("<embed", served.text)

    async def test_form_display_fragments_and_icon_declarations_survive_pause(self) -> None:
        original = self.fixture.capture

        def capture(path: str) -> CapturedPublicResponse:
            response = original(path)
            if path in self.fixture.plan.public_routes and response.status == 200:
                body = response.body.replace(
                    b"</head>",
                    b'<link rel="Shortcut ICON" href="/assets/bg.png">'
                    b'<link rel="apple-touch-icon" href="/assets/bg.png"></head>',
                ).replace(
                    b'<input name="message">',
                    b"<section><h2>Repair enquiry details</h2>"
                    b'<a href="/prices/repair#details">Read details</a>'
                    b"<label>Message</label><textarea>private-field-value</textarea>"
                    b"<select><option>private-selection</option></select></section>",
                )
                return response.model_copy(update={"body": body})
            return response

        self.fixture.capture = capture  # type: ignore[method-assign]
        site = prepare_public_site(self.fixture.plan, self.fixture)
        PreparedPublicPause(site, self.store, self.fixture).begin()
        self.fixture.update_database()
        served = await request(prepared_public_app(site), "GET", "/contact")
        self.assertEqual(served.status_code, 200)
        self.assertIn('href="/prices/repair#details"', served.text)
        self.assertIn("Repair enquiry details", served.text)
        self.assertIn('rel="Shortcut ICON"', served.text)
        self.assertIn('rel="apple-touch-icon"', served.text)
        self.assertNotIn("private-field-value", served.text)
        self.assertNotIn("private-selection", served.text)
        self.assertNotIn("<form", served.text)
        self.assertNotIn("<input", served.text)
        self.assertIn("disabled", served.text)
        submission = await request(prepared_public_app(site), "POST", "/contact")
        self.assertEqual(submission.status_code, 423)

    def test_unsupported_inline_svg_fails_before_writer_fencing(self) -> None:
        original = self.fixture.capture

        def capture(path: str) -> CapturedPublicResponse:
            response = original(path)
            if path == "/":
                return response.model_copy(
                    update={
                        "body": response.body.replace(
                            b"</main>", b'<svg><path d="M0 0"/></svg></main>'
                        )
                    }
                )
            return response

        self.fixture.capture = capture  # type: ignore[method-assign]
        with self.assertRaises(ValueError):
            prepare_public_site(self.fixture.plan, self.fixture)
        self.assertEqual(self.fixture.fenced, set())
        self.assertEqual(self.fixture.published, "")
