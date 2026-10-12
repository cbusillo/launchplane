from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.test_http_app_product import (
    _product_health_monitoring_payload,
    _product_health_monitoring_profile,
    _sqlite_database_url,
)
from tests.support.http import request


class HealthMonitoringBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_reviews_applies_and_replays_with_csrf(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            store.ensure_schema()
            original = _product_health_monitoring_profile()
            store.write_product_profile_record(original)
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = manager.issue(_github_human_identity())
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [session.identity.github_id],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=manager,
            )
            path = "/v1/product-profiles/health-monitoring/apply"
            payload = _product_health_monitoring_payload()
            rejected = await request(
                app,
                "POST",
                path,
                payload=payload,
                headers={"Cookie": manager.session_cookie_header(session)},
            )
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(store.read_product_profile_record(original.product), original)
            plan = await request(
                app,
                "POST",
                path,
                payload=payload,
                headers=_browser_mutation_headers(manager, session),
            )
            self.assertEqual(plan.status_code, 202)
            self.assertEqual(store.read_product_profile_record(original.product), original)
            apply_payload = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
            }
            responses = []
            for _ in range(2):
                responses.append(
                    await request(
                        app,
                        "POST",
                        path,
                        payload=apply_payload,
                        headers={
                            **_browser_mutation_headers(manager, session),
                            "Idempotency-Key": "browser-monitoring",
                        },
                    )
                )
            self.assertTrue(all(response.status_code == 202 for response in responses))
            self.assertTrue(responses[0].json()["result"]["applied"])
            self.assertTrue(responses[1].json()["replayed"])
            saved = store.read_product_profile_record(original.product)
            self.assertTrue(saved.lanes[0].health_monitoring.checks[0].require_runtime_identity)
            self.assertEqual(saved.lanes[0].health_url, original.lanes[0].health_url)
            self.assertEqual(saved.lanes[1:], original.lanes[1:])
            store.close()

    async def test_scoped_human_plan_cannot_apply_or_target_another_instance(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            store.ensure_schema()
            original = _product_health_monitoring_profile()
            store.write_product_profile_record(original)
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = manager.issue(_github_human_identity())
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [session.identity.github_id],
                            "roles": ["admin"],
                            "products": [original.product],
                            "contexts": ["cm"],
                            "instances": ["testing"],
                            "actions": ["product_profile.health_monitoring.plan"],
                        }
                    ],
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=manager,
            )
            path = "/v1/product-profiles/health-monitoring/apply"
            payload = _product_health_monitoring_payload()
            for change in (
                {"instance": "prod"},
                {"mode": "apply", "reviewed_plan_sha256": "a" * 64},
            ):
                response = await request(
                    app,
                    "POST",
                    path,
                    payload={**payload, **change},
                    headers={
                        **_browser_mutation_headers(manager, session),
                        "Idempotency-Key": "denied-browser-monitoring",
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")
            self.assertEqual(store.read_product_profile_record(original.product), original)
            store.close()
