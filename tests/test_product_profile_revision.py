from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _product_profile_write_policy,
    _local_operator_bearer_config,
    _RejectingVerifier,
)
from tests.support.auth import StubVerifier, identity, scoped_agent_transport
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_release_review import seed, profile, github_read
from control_plane.release_review import build_release_review
from control_plane.release_request import record_release_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from urllib.parse import urlsplit
from control_plane.testing_address_storage import testing_health_url_correction_allowed


class ProductProfileRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_profile_updates_preserve_and_cannot_forge_release_requests(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "test.db"))
        self.addCleanup(store.close)
        store.ensure_schema()
        seed(store)
        saved = record_release_request(
            store=store,
            profile=profile(),
            review=build_release_review(store=store, profile=profile(), read=github_read),
            source="director_request",
            reason="Release the agreed work.",
            decision_reference="",
            actor="director",
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=_product_profile_write_policy(product=saved.product),
            record_store_factory=lambda: store,
        )
        payload = saved.model_dump(mode="json", exclude={"release_request"})
        payload["display_name"] = "Updated product name"
        edited = await request(
            app,
            "POST",
            "/v1/product-profiles",
            headers={"Authorization": "Bearer valid-token"},
            payload=payload,
        )
        self.assertEqual(edited.status_code, 202, edited.text)
        current = store.read_product_profile_record(saved.product)
        self.assertEqual(current.display_name, payload["display_name"])
        self.assertEqual(current.release_request, saved.release_request)
        assert saved.release_request is not None
        for replacement in (None, saved.release_request.model_copy(update={"actor": "forged"})):
            with self.subTest(replacement=replacement):
                changed = current.model_copy(update={"release_request": replacement})
                changed_payload = changed.model_dump(mode="json")
                if replacement is None:
                    changed_payload["release_request"] = None
                refused = await request(
                    app,
                    "POST",
                    "/v1/product-profiles",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=changed_payload,
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(store.read_product_profile_record(saved.product), current)

    async def test_stale_profile_cannot_overwrite_a_concurrent_change(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "test.db"))
        self.addCleanup(store.close)
        store.ensure_schema()
        before = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        current = before.model_copy(update={"display_name": "Concurrent product name"})
        store.write_product_profile_record(current)
        policy = _product_profile_write_policy(product=before.product)
        policy = policy.model_copy(
            update={
                "github_actions": tuple(
                    rule.model_copy(
                        update={"actions": ("product_profile.read", "product_profile.write")}
                    )
                    for rule in policy.github_actions
                )
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=policy,
            record_store_factory=lambda: store,
        )
        response = await request(
            app,
            "POST",
            "/v1/product-profiles",
            headers={
                "Authorization": "Bearer valid-token",
                "If-Match": f'"{product_profile_record_sha256(before)}"',
            },
            payload=before.model_dump(mode="json"),
        )
        self.assertEqual(response.status_code, 412)
        self.assertEqual(store.read_product_profile_record(before.product), current)
        read = await request(
            app,
            "GET",
            f"/v1/product-profiles/{before.product}",
            headers={"Authorization": "Bearer valid-token"},
        )
        self.assertEqual(read.headers["etag"], f'"{product_profile_record_sha256(current)}"')
        after = current.model_copy(
            update={
                "preview": current.preview.model_copy(
                    update={"copied_env_keys": ("ODOO_PROXY_MODE",)}
                )
            }
        )
        headers = {
            "Authorization": "Bearer valid-token",
            "If-Match": read.headers["etag"],
            "Idempotency-Key": "profile-revision-fixture",
        }
        applied = await request(
            app,
            "POST",
            "/v1/product-profiles",
            headers=headers,
            payload=after.model_dump(mode="json"),
        )
        self.assertEqual(applied.status_code, 202)
        replay = await request(
            app,
            "POST",
            "/v1/product-profiles",
            headers=headers,
            payload=after.model_dump(mode="json"),
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(store.read_product_profile_record(before.product), after)


class TestingHealthUrlCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "test.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        self.after = self.before.model_copy(deep=True)
        self.lane_index = next(
            index for index, lane in enumerate(self.before.lanes) if lane.instance == "testing"
        )
        lane = self.before.lanes[self.lane_index]
        lane.health_url = (
            urlsplit(lane.health_url)._replace(netloc="former-generation.example.invalid").geturl()
        )
        self.store.write_product_profile_record(self.before)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {
            "Authorization": "Bearer local-operator-token",
            "If-Match": f'"{product_profile_record_sha256(self.before)}"',
            "Idempotency-Key": "health-fallback-correction",
        }

    async def test_director_dry_run_apply_and_replay_preserve_every_other_field(self) -> None:
        reviewed = await request(
            self.app,
            "POST",
            "/v1/product-profiles?dry_run=true",
            headers=self.headers,
            payload=self.after.model_dump(mode="json"),
        )
        self.assertEqual(reviewed.status_code, 202, reviewed.text)
        self.assertTrue(reviewed.json()["result"]["dry_run"])
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)
        for _ in range(2):
            applied = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers=self.headers,
                payload=self.after.model_dump(mode="json"),
            )
            self.assertEqual(applied.status_code, 202, applied.text)
            self.assertEqual(
                self.store.read_product_profile_record(self.before.product), self.after
            )

    async def test_other_profile_changes_and_scoped_callers_still_refuse(self) -> None:
        malformed = self.before.model_copy(deep=True)
        malformed.lanes[self.lane_index].health_url = "https://[malformed"
        self.assertFalse(testing_health_url_correction_allowed(malformed, self.after))
        for changed in (
            self.after.model_copy(update={"display_name": "Unreviewed name"}),
            self.after.model_copy(update={"lanes": tuple(reversed(self.after.lanes))}),
            self.before.model_copy(deep=True),
        ):
            for dry_run in (True, False):
                with self.subTest(changed=changed, dry_run=dry_run):
                    response = await request(
                        self.app,
                        "POST",
                        f"/v1/product-profiles?dry_run={str(dry_run).lower()}",
                        headers=self.headers,
                        payload=changed.model_dump(mode="json"),
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        self.store.read_product_profile_record(self.before.product), self.before
                    )
        with scoped_agent_transport():
            response = await request(
                self.app,
                "POST",
                "/v1/product-profiles?dry_run=true",
                headers=self.headers,
                payload=self.after.model_dump(mode="json"),
            )
        self.assertEqual(response.status_code, 403, response.text)

    async def test_production_url_and_stale_profile_cannot_use_testing_correction(self) -> None:
        changed = self.before.model_copy(deep=True)
        production = next(lane for lane in changed.lanes if lane.instance == "prod")
        production.health_url = (
            urlsplit(production.health_url)
            ._replace(netloc="another-production.example.invalid")
            .geturl()
        )
        refused = await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers=self.headers,
            payload=changed.model_dump(mode="json"),
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        concurrent = self.before.model_copy(update={"display_name": "Concurrent edit"})
        self.store.write_product_profile_record(concurrent)
        refused = await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers=self.headers,
            payload=self.after.model_dump(mode="json"),
        )
        self.assertEqual(refused.status_code, 412, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), concurrent)
