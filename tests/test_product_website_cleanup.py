import unittest
from typing import Any, cast
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_website_cleanup import (
    ProductWebsiteCleanupRequest,
    build_website_cleanup,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import (
    DbOnlyMutationPreflightResult,
    PostgresRecordStore,
    ProductProfileCompareWriteResult,
)
from tests.http_app_test_support import (
    _RejectingVerifier,
    _AsgiResponse,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
)
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url


def profile() -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload("private-tool")
    payload["production_use"] = "live"
    lane = cast(list[dict[str, Any]], payload["lanes"])[0]
    lane["health_monitoring"] = {
        "monitoring_intent": "private",
        "checks": [
            {
                "name": "private-health",
                "kind": "private_http",
                "private_endpoint_key": "private-prod",
                "enabled": True,
                "require_runtime_identity": True,
            },
            {
                "name": "public-ingress",
                "kind": "public_http",
                "enabled": True,
                "url": "https://stale.example.test/health",
            },
            {
                "name": "provider-ready",
                "kind": "provider",
                "provider": "example",
                "provider_check": "ready",
                "enabled": True,
            },
        ],
    }
    second = {**lane, "instance": "prod", "base_url": "", "health_url": ""}
    payload["lanes"] = [lane, second]
    return LaunchplaneProductProfileRecord.model_validate(payload)


class WebsiteCleanupTests(unittest.TestCase):
    def test_exact_diff_preserves_private_checks_and_unrelated_fields(self) -> None:
        original = profile()
        plan, after = build_website_cleanup(
            original, ProductWebsiteCleanupRequest(reason="No website")
        )
        expected = original.model_dump(mode="json")
        expected["public_website"] = "none"
        for lane in expected["lanes"]:
            lane["base_url"] = lane["health_url"] = ""
            check = lane["health_monitoring"]["checks"][1]
            check["enabled"] = False
            check["url"] = ""
        self.assertEqual(after.model_dump(mode="json"), expected)
        self.assertTrue(plan.changed)
        self.assertEqual(original.public_website, "required")
        self.assertTrue(any(change.path.endswith("base_url") for change in plan.changes))
        self.assertFalse(any("private-health" in change.path for change in plan.changes))
        no_change, _ = build_website_cleanup(
            after, ProductWebsiteCleanupRequest(reason="Already done")
        )
        self.assertFalse(no_change.changed)

    def test_public_and_unknown_intent_are_not_exempted(self) -> None:
        for intent in ("public", "prelaunch"):
            payload = profile().model_dump(mode="json")
            payload["lanes"][0]["health_monitoring"]["monitoring_intent"] = intent
            with self.assertRaises(ValueError):
                build_website_cleanup(
                    LaunchplaneProductProfileRecord.model_validate(payload),
                    ProductWebsiteCleanupRequest(reason="No website"),
                )


class WebsiteCleanupHttpTests(unittest.IsolatedAsyncioTestCase):
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
                product="private-tool",
                context="launchplane",
            ),
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )

    async def post(self, payload: dict[str, object], key: str = "") -> _AsgiResponse:
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/private-tool/website-cleanup",
            headers={
                **_browser_mutation_headers(self.sessions, self.session),
                "Idempotency-Key": key,
            },
            payload=payload,
        )

    async def test_dry_run_atomic_apply_replay_conflict_and_scoped_receipt(self) -> None:
        before = self.store.read_product_profile_record("private-tool")
        dry = await self.post({"reason": "No public website"})
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.store.read_product_profile_record("private-tool"), before)
        body = {
            "reason": "No public website",
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        missing_key = await self.post(body)
        self.assertEqual(missing_key.status_code, 400)
        applied = await self.post(body, "cleanup")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(
            self.store.read_product_profile_record("private-tool").public_website, "none"
        )
        replay = await self.post(body, "cleanup")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        # Simulate the original commit arriving after the retry's first preflight.
        original_preflight = self.store.prepare_db_only_mutation
        calls = 0

        def late_commit(**kwargs: Any) -> DbOnlyMutationPreflightResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return DbOnlyMutationPreflightResult(status="missing")
            return original_preflight(**kwargs)

        with patch.object(self.store, "prepare_db_only_mutation", side_effect=late_commit):
            late_replay = await self.post(body, "cleanup")
        self.assertEqual(late_replay.status_code, 202, late_replay.text)
        self.assertTrue(late_replay.json()["replayed"])
        self.assertEqual(late_replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflict = await self.post({**body, "reason": "Different request"}, "cleanup")
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")
        for field, key, expected in (
            ("website-cleanup", "cleanup", "completed"),
            ("website-cleanup", "missing", "unresolved"),
            ("owner", "cleanup", "unresolved"),
        ):
            receipt = await _asgi_request(
                self.app,
                "GET",
                f"/v1/product-profiles/private-tool/mutation-receipts/{field}?operation_key={key}",
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
            self.assertEqual(receipt.status_code, 200, receipt.text)
            self.assertEqual(receipt.json()["state"], expected)

    async def test_stale_review_and_concurrent_profile_edit_refuse_without_partial_cleanup(
        self,
    ) -> None:
        dry = await self.post({"reason": "No public website"})
        body = {
            "reason": "No public website",
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        before = self.store.read_product_profile_record("private-tool")
        raced = before.model_copy(update={"display_name": "New display name"})
        original_write = self.store.compare_and_write_product_profile_record

        def race(**kwargs: Any) -> ProductProfileCompareWriteResult:
            self.store.write_product_profile_record(raced)
            return original_write(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=race):
            result = await self.post(body, "raced")
        self.assertEqual(result.json()["error"]["code"], "stale")
        self.assertEqual(self.store.read_product_profile_record("private-tool"), raced)
        result = await self.post(body, "stale")
        self.assertEqual(result.json()["error"]["code"], "stale")
        self.assertEqual(self.store.read_product_profile_record("private-tool"), raced)

    async def test_non_admin_missing_csrf_and_extra_fields_are_refused(self) -> None:
        before = self.store.read_product_profile_record("private-tool")
        bad = await self.post({"reason": "No public website", "production_use": "prelaunch"})
        self.assertEqual(bad.status_code, 400)
        no_csrf = await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/private-tool/website-cleanup",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            payload={"reason": "No website"},
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.session = self.sessions.issue(_github_human_identity(role="read_only"))
        denied = await self.post({"reason": "No public website"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.store.read_product_profile_record("private-tool"), before)
