import unittest
from unittest.mock import patch

import click

from control_plane.workflows.odoo_verification import (
    _extract_same_origin_logo_urls,
    verify_odoo_stable_readiness,
)


class OdooVerificationTests(unittest.TestCase):
    def test_verification_uses_logo_url_from_canonical_page(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            self.assertEqual(timeout_seconds, 5)
            calls.append(url)
            if url == "https://cm-testing.example.com":
                return (
                    200,
                    '<html><link rel="canonical" href="https://cm-testing.example.com">'
                    '<img src="/web/image/website/7/logo?unique=abc"></html>',
                    "text/html",
                )
            if url == "https://cm-testing.example.com/web/health":
                return 200, '{"status":"ok"}', "application/json"
            if url == "https://cm-testing.example.com/web/image/website/7/logo?unique=abc":
                return 200, "image-bytes", "image/png"
            return 404, "missing", "text/plain"

        with patch(
            "control_plane.workflows.odoo_verification._http_text",
            side_effect=fake_http_text,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com",
                timeout_seconds=5,
            )

        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.canonical_status, "pass")
        self.assertEqual(result.logo_status, "pass")
        self.assertEqual(
            calls,
            [
                "https://cm-testing.example.com/web/health",
                "https://cm-testing.example.com",
                "https://cm-testing.example.com",
                "https://cm-testing.example.com/web/image/website/7/logo?unique=abc",
            ],
        )
        self.assertEqual(
            result.evidence.logo_urls,
            ("https://cm-testing.example.com/web/image/website/7/logo?unique=abc",),
        )

    def test_verification_keeps_canonical_logo_fallback_after_bad_logo_asset(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            self.assertEqual(timeout_seconds, 5)
            calls.append(url)
            if url == "https://cm-testing.example.com":
                return (
                    200,
                    '<link rel="canonical" href="https://cm-testing.example.com">'
                    '<img src="/web/image/product.template/7/logo_badge">',
                    "text/html",
                )
            if url == "https://cm-testing.example.com/web/image/website/1/logo":
                return 200, "image-bytes", "image/png"
            if url == "https://cm-testing.example.com/web/image/product.template/7/logo_badge":
                return 200, "wrong-image", "image/png"
            return 404, "missing", "text/plain"

        with patch(
            "control_plane.workflows.odoo_verification._http_text",
            side_effect=fake_http_text,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com",
                verify_health=False,
                verify_canonical=False,
                timeout_seconds=5,
            )

        self.assertEqual(result.logo_status, "pass")
        self.assertEqual(
            result.evidence.logo_urls,
            ("https://cm-testing.example.com/web/image/website/1/logo",),
        )
        self.assertEqual(
            calls,
            [
                "https://cm-testing.example.com",
                "https://cm-testing.example.com/web/image/website/1/logo",
            ],
        )

    def test_verification_falls_back_to_legacy_website_one_logo_route(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            self.assertEqual(timeout_seconds, 5)
            calls.append(url)
            if url == "https://cm-testing.example.com":
                return 200, '<link rel="canonical" href="https://cm-testing.example.com">', "text/html"
            if url == "https://cm-testing.example.com/web/image/website/1/logo":
                return 200, "image-bytes", "image/png"
            return 404, "missing", "text/plain"

        with patch(
            "control_plane.workflows.odoo_verification._http_text",
            side_effect=fake_http_text,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com/",
                verify_health=False,
                verify_canonical=False,
                timeout_seconds=5,
            )

        self.assertEqual(result.logo_status, "pass")
        self.assertEqual(
            calls,
            [
                "https://cm-testing.example.com",
                "https://cm-testing.example.com/web/image/website/1/logo",
            ],
        )

    def test_verification_accepts_odoo_web_content_logo_url(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            self.assertEqual(timeout_seconds, 5)
            calls.append(url)
            if url == "https://cm-testing.example.com":
                return (
                    200,
                    '<meta property="og:image" content="/web/content?model=website&amp;id=1&amp;field=logo">',
                    "text/html",
                )
            if url == "https://cm-testing.example.com/web/content?model=website&id=1&field=logo":
                return 200, "image-bytes", "image/png"
            return 404, "missing", "text/plain"

        with patch(
            "control_plane.workflows.odoo_verification._http_text",
            side_effect=fake_http_text,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com",
                verify_health=False,
                verify_canonical=False,
                timeout_seconds=5,
            )

        self.assertEqual(result.logo_status, "pass")
        self.assertEqual(
            result.evidence.logo_urls,
            ("https://cm-testing.example.com/web/content?model=website&id=1&field=logo",),
        )

    def test_verification_fails_closed_when_logo_is_missing(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            self.assertEqual(timeout_seconds, 5)
            calls.append(url)
            if url == "https://cm-testing.example.com":
                return 200, '<link rel="canonical" href="https://cm-testing.example.com">', "text/html"
            if url == "https://cm-testing.example.com/web/image/website/1/logo":
                return 404, "missing", "text/plain"
            return 404, "missing", "text/plain"

        with patch(
            "control_plane.workflows.odoo_verification._http_text",
            side_effect=fake_http_text,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com/",
                verify_health=False,
                verify_canonical=False,
                timeout_seconds=5,
            )

        self.assertEqual(result.logo_status, "fail")
        self.assertEqual(
            result.error_message,
            "Logo check https://cm-testing.example.com/web/image/website/1/logo returned HTTP 404.",
        )
        self.assertEqual(
            result.evidence.logo_urls,
            ("https://cm-testing.example.com/web/image/website/1/logo",),
        )
        self.assertEqual(result.evidence.probes[-1].failure_class, "http")
        self.assertEqual(
            calls,
            [
                "https://cm-testing.example.com",
                "https://cm-testing.example.com/web/image/website/1/logo",
                "https://cm-testing.example.com",
                "https://cm-testing.example.com/web/image/website/1/logo",
            ],
        )

    def test_extract_logo_urls_ignores_cross_origin_assets(self) -> None:
        urls = _extract_same_origin_logo_urls(
            base_url="https://cm-testing.example.com",
            body=(
                '<img src="https://evil.example.com/web/image/website/9/logo">'
                '<img src="/web/image/product.template/7/logo_badge">'
                '<img src="/web/image/website/7/logo?unique=abc&amp;download=1">'
            ),
        )

        self.assertEqual(
            urls,
            ("https://cm-testing.example.com/web/image/website/7/logo?unique=abc&download=1",),
        )

    def test_extract_logo_urls_accepts_same_origin_web_content_logo_assets(self) -> None:
        urls = _extract_same_origin_logo_urls(
            base_url="https://cm-testing.example.com",
            body=(
                '<meta property="og:image" content="https://evil.example.com/web/content?model=website&amp;id=1&amp;field=logo">'
                '<meta name="twitter:image" content="/web/content/website/1/logo/Cell%20Mechanic.png">'
            ),
        )

        self.assertEqual(
            urls,
            ("https://cm-testing.example.com/web/content/website/1/logo/Cell%20Mechanic.png",),
        )

    def test_extract_logo_urls_accepts_unquoted_same_origin_logo_assets(self) -> None:
        urls = _extract_same_origin_logo_urls(
            base_url="https://cm-testing.example.com",
            body=(
                "<img src=/web/image/website/7/logo?unique=abc&amp;download=1>"
                "<meta property=og:image content=/web/content?model=website&amp;id=1&amp;field=logo>"
            ),
        )

        self.assertEqual(
            urls,
            (
                "https://cm-testing.example.com/web/image/website/7/logo?unique=abc&download=1",
                "https://cm-testing.example.com/web/content?model=website&id=1&field=logo",
            ),
        )

    def test_verification_retry_allows_transient_startup_failure(self) -> None:
        attempts = 0

        def fake_http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
            nonlocal attempts
            self.assertEqual(url, "https://cm-testing.example.com/web/health")
            self.assertEqual(timeout_seconds, 30)
            attempts += 1
            if attempts == 1:
                raise click.ClickException("temporary health 404")
            return 200, '{"status":"ok"}', "application/json"

        with (
            patch("control_plane.workflows.odoo_verification._http_text", side_effect=fake_http_text),
            patch(
                "control_plane.workflows.odoo_verification.time.sleep",
                side_effect=lambda _seconds: None,
            ) as sleep_mock,
        ):
            result = verify_odoo_stable_readiness(
                base_url="https://cm-testing.example.com",
                verify_canonical=False,
                verify_logo=False,
                timeout_seconds=30,
                retry_interval_seconds=5,
            )

        self.assertEqual(result.health_status, "pass")
        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
