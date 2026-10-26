from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _product_profile_write_policy
from tests.support.auth import StubVerifier, identity
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_release_review import seed, profile, github_read
from control_plane.release_review import build_release_review
from control_plane.release_request import record_release_request


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
