import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_display_name import ProductDisplayNameRequest, build_display_name_plan
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore, ProductProfileCompareWriteResult
from tests.http_app_test_support import (
    _AsgiResponse,
    _RejectingVerifier,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import sqlite_database_url


def profile() -> LaunchplaneProductProfileRecord:
    payload = _odoo_profile_payload_with_prod_lane("example-site")
    payload["production_use"] = "live"
    payload["historical_contexts"] = ["old-site"]
    return LaunchplaneProductProfileRecord.model_validate(payload)


class DisplayNameTests(unittest.TestCase):
    def test_only_display_name_changes_and_review_binds_the_whole_profile(self) -> None:
        before = profile()
        request = ProductDisplayNameRequest(
            display_name="  Example Website  ", reason="Clean sidebar"
        )
        plan, after = build_display_name_plan(before, request)
        expected = before.model_dump(mode="json")
        expected["display_name"] = "Example Website"
        self.assertEqual(after.model_dump(mode="json"), expected)
        self.assertTrue(plan.changed)
        changed_profile = before.model_copy(update={"source": "another-edit"})
        changed_plan, _ = build_display_name_plan(changed_profile, request)
        self.assertNotEqual(plan.plan_sha256, changed_plan.plan_sha256)
        unchanged, _ = build_display_name_plan(after, request)
        self.assertFalse(unchanged.changed)

    def test_empty_multiline_and_unreviewed_apply_are_rejected(self) -> None:
        for name in (" ", "Name\nOther", "Name\tOther"):
            with self.assertRaises(ValueError):
                ProductDisplayNameRequest(display_name=name, reason="Rename")
        with self.assertRaises(ValueError):
            ProductDisplayNameRequest(display_name="Name", reason="Rename", mode="apply")


class DisplayNameHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "db.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(profile())
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=("product_profile.write", "product_profile.read"),
                product="example-site",
                context="launchplane",
            ),
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )

    async def post(self, payload: dict[str, object], key: str = "") -> _AsgiResponse:
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/example-site/display-name",
            headers={
                **_browser_mutation_headers(self.sessions, self.session),
                "Idempotency-Key": key,
            },
            payload=payload,
        )

    async def test_review_apply_readback_replay_and_receipt_preserve_authority(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        draft: dict[str, object] = {"display_name": "Example Website", "reason": "Clean sidebar"}
        dry = await self.post(draft)
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), before)
        body = {
            **draft,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        self.assertEqual((await self.post(body)).status_code, 400)
        applied = await self.post(body, "rename")
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record("example-site")
        expected = before.model_dump(mode="json")
        expected.update(
            display_name="Example Website", updated_at=after.updated_at, source=after.source
        )
        self.assertEqual(after.model_dump(mode="json"), expected)
        read = await _asgi_request(
            self.app,
            "GET",
            "/v1/product-profiles/example-site",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
        )
        self.assertEqual(read.json()["profile"]["display_name"], "Example Website")
        replay = await self.post(body, "rename")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflict = await self.post({**body, "display_name": "Other Name"}, "rename")
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")
        receipt = await _asgi_request(
            self.app,
            "GET",
            "/v1/product-profiles/example-site/mutation-receipts/display-name?operation_key=rename",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
        )
        self.assertEqual(receipt.json()["state"], "completed")

    async def test_stale_review_and_racing_profile_write_are_refused(self) -> None:
        draft: dict[str, object] = {"display_name": "Example Website", "reason": "Clean sidebar"}
        dry = await self.post(draft)
        body = {
            **draft,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        before = self.store.read_product_profile_record("example-site")
        raced = before.model_copy(update={"source": "concurrent-edit"})
        original_write = self.store.compare_and_write_product_profile_record

        def race(**kwargs: Any) -> ProductProfileCompareWriteResult:
            self.store.write_product_profile_record(raced)
            return original_write(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=race):
            result = await self.post(body, "raced")
        self.assertEqual(result.json()["error"]["code"], "stale")
        self.assertEqual(self.store.read_product_profile_record("example-site"), raced)
        result = await self.post(body, "stale")
        self.assertEqual(result.json()["error"]["code"], "stale")

    async def test_non_admin_missing_csrf_and_unrelated_changes_are_refused(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        draft: dict[str, object] = {"display_name": "Example Website", "reason": "Rename"}
        self.assertEqual((await self.post({**draft, "lanes": []})).status_code, 400)
        no_csrf = await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/example-site/display-name",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            payload=draft,
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.session = self.sessions.issue(_github_human_identity(role="read_only"))
        self.assertEqual((await self.post(draft)).status_code, 403)
        self.assertEqual(self.store.read_product_profile_record("example-site"), before)
