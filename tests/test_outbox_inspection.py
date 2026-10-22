from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import get
from tests.support.stores import _sqlite_database_url
from tests.http_app_test_support import _record_read_policy
from tests.support.auth import _identity, _StubVerifier
from tests.test_outbox_worker import _workflow_delivery
from tests import test_public_ingress_direct_outbox as direct_outbox
from tests.test_public_ingress_monitor import _notification_policy


class OutboxInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_kinds_inventory_is_bounded_read_only_and_omits_message_content(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            delivery = _workflow_delivery()
            destination = direct_outbox.DirectNotificationOutboxTests._destination("email")
            store.write_public_ingress_notification_policy_record(_notification_policy(destination))
            store.write_outbox_delivery_record(delivery)
            store.write_outbox_delivery_record(
                delivery.model_copy(
                    update={
                        "delivery_id": "discussion-fixture",
                        "kind": "discussion_reminder",
                        "dedupe_key": "discussion-fixture",
                        "payload": {"body": "PRIVATE MESSAGE"},
                    }
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="operations.read", context="launchplane"),
                record_store_factory=lambda: store,
            )
            response = await get(
                app,
                "/v1/outbox/inspection?limit=1",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertFalse(response.json()["inspection"]["complete"])
            response = await get(
                app, "/v1/outbox/inspection", headers={"Authorization": "Bearer valid-token"}
            )
            inspection = response.json()["inspection"]
            self.assertTrue(inspection["complete"])
            self.assertEqual(
                set(inspection["counts"]),
                {"github_workflow_dispatch:pending", "discussion_reminder:pending"},
            )
            self.assertNotIn("PRIVATE MESSAGE", response.text)
            self.assertNotIn("credential_context", response.text)
            projected = inspection["notification_policies"][0]["destinations"][0]
            self.assertEqual(projected["email_to"], list(destination.email_to))
            self.assertNotIn(destination.smtp_host, response.text)
            self.assertNotIn(destination.smtp_password_secret, response.text)
            self.assertEqual(store.read_outbox_delivery_record(delivery.delivery_id), delivery)

    async def test_other_context_cannot_inspect_global_destinations(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="operations.read", context="other"),
            record_store_factory=lambda: object(),
        )
        response = await get(
            app, "/v1/outbox/inspection", headers={"Authorization": "Bearer valid-token"}
        )
        self.assertEqual(response.status_code, 403)
