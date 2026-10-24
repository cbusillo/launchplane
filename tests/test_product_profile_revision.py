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


class ProductProfileRevisionTests(unittest.IsolatedAsyncioTestCase):
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
                    update={"override_env": {"ODOO_PROXY_MODE": "True"}}
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
