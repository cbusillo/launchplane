from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


class NativeProductOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_reviews_and_applies_records_without_caller_grants(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory) / "records.sqlite3")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            policy = _github_human_product_config_policy(
                actions=("generic_web_onboarding.plan", "product_onboarding.apply"),
                product="launchplane",
                context="launchplane",
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = session_manager.issue(_github_human_identity())
            reader = session_manager.issue(_github_human_identity(role="read_only"))
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                human_session_manager=session_manager,
                record_store_factory=lambda: store,
            )
            intent = {
                "product": "new-site",
                "display_name": "New site",
                "repository": "example/new-site",
                "repository_id": "321",
                "repository_owner_id": "654",
                "image_repository": "ghcr.io/example/new-site",
                "runtime_port": 8080,
                "health_path": "/healthz",
                "preview_base_url": "https://preview.example.invalid",
            }
            review = {"product": "launchplane", "mode": "dry_run", "generic_web": intent}
            original_policy = policy.model_dump(mode="json")
            async with lifespan_client(app) as client:
                for headers in (
                    {"Cookie": session_manager.session_cookie_header(session)},
                    _browser_mutation_headers(session_manager, reader),
                ):
                    refused = await client.post(
                        "/v1/product-onboarding/apply", json=review, headers=headers
                    )
                    self.assertEqual(refused.status_code, 403, refused.text)
                planned = await client.post(
                    "/v1/product-onboarding/apply",
                    json=review,
                    headers=_browser_mutation_headers(session_manager, session),
                )
                self.assertEqual(planned.status_code, 202, planned.text)
                self.assertFalse(store.list_product_profile_records())
                applied_request = {
                    **review,
                    "mode": "apply",
                    "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                    "resolved_target_id": "allocated-application",
                }
                headers = {
                    **_browser_mutation_headers(session_manager, session),
                    "Idempotency-Key": "native-onboarding-records",
                }
                mismatch = await client.post(
                    "/v1/product-onboarding/apply",
                    json={**applied_request, "generic_web": {**intent, "runtime_port": 9090}},
                    headers=headers,
                )
                self.assertEqual(mismatch.status_code, 409, mismatch.text)
                headers.update(_browser_mutation_headers(session_manager, session))
                applied = await client.post(
                    "/v1/product-onboarding/apply", json=applied_request, headers=headers
                )
                self.assertEqual(applied.status_code, 202, applied.text)
                headers.update(_browser_mutation_headers(session_manager, session))
                replayed = await client.post(
                    "/v1/product-onboarding/apply", json=applied_request, headers=headers
                )
                self.assertEqual(replayed.status_code, 202, replayed.text)
                self.assertTrue(replayed.json()["replayed"])
            profile = store.read_product_profile_record(str(intent["product"]))
            self.assertEqual(profile.repository_id, intent["repository_id"])
            self.assertEqual(profile.lanes[0].instance, "testing")
            self.assertEqual(profile.preview.context, "new-site-preview")
            self.assertEqual(
                store.read_dokploy_target_id_record(
                    context_name=profile.lanes[0].context, instance_name="testing"
                ).target_id,
                applied_request["resolved_target_id"],
            )
            self.assertEqual(policy.model_dump(mode="json"), original_policy)
            self.assertFalse(store.list_authz_policy_records())
