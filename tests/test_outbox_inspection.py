from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.outbox_inspection import inspect_outbox
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import get
from tests.support.stores import _sqlite_database_url
from tests.http_app_test_support import _record_read_policy
from tests.support.auth import _identity, _StubVerifier
from tests.test_outbox_worker import _workflow_delivery
from tests import test_public_ingress_direct_outbox as direct_outbox
from tests.test_public_ingress_monitor import _notification_policy, _profile


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
            workflow = next(
                row
                for row in inspection["deliveries"]
                if row["delivery_id"] == delivery.delivery_id
            )
            self.assertEqual(
                workflow["routing"]["credential_context"], delivery.payload["credential_context"]
            )
            self.assertEqual(workflow["routing"]["inputs"], delivery.payload["inputs"])
            projected = inspection["notification_policies"][0]["destinations"][0]
            self.assertEqual(projected["email_to"], list(destination.email_to))
            self.assertNotIn(destination.smtp_host, response.text)
            self.assertNotIn(destination.smtp_password_secret, response.text)
            self.assertEqual(store.read_outbox_delivery_record(delivery.delivery_id), delivery)

    async def test_notification_census_binds_reused_destinations_and_incident_state(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            destination = direct_outbox.DirectNotificationOutboxTests._destination("email")
            first_policy = _notification_policy(destination)
            second_policy = first_policy.model_copy(
                update={
                    "policy_id": "second-policy",
                    "destinations": (
                        destination.model_copy(update={"email_to": ("other@example.com",)}),
                    ),
                }
            )
            for policy in (first_policy, second_policy):
                store.write_public_ingress_notification_policy_record(policy)
            result = direct_outbox.DirectNotificationOutboxTests._probe(
                store, 503, "2026-05-29T12:00:00Z"
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="operations.read", context="launchplane"),
                record_store_factory=lambda: store,
            )
            for status in ("open", "resolved"):
                if status == "resolved":
                    direct_outbox.DirectNotificationOutboxTests._probe(
                        store, 200, "2026-05-29T12:01:00Z"
                    )
                response = await get(
                    app, "/v1/outbox/inspection", headers={"Authorization": "Bearer valid-token"}
                )
                self.assertEqual(response.status_code, 200)
                inspection = response.json()["inspection"]
                self.assertTrue(inspection["complete"])
                rows = inspection["deliveries"]
                self.assertEqual(
                    {row["routing"]["policy_id"] for row in rows},
                    {first_policy.policy_id, second_policy.policy_id},
                )
                for row in rows:
                    queued = store.read_outbox_delivery_record(row["delivery_id"])
                    attempt = cast(dict[str, object], queued.payload["attempt"])
                    self.assertEqual(row["routing"]["incident_id"], result.incidents[0].incident_id)
                    self.assertEqual(row["routing"]["event"], attempt["event"])
                    self.assertEqual(row["routing"]["incident_status"], status)
                    self.assertEqual(
                        row["routing"]["existing_issue_url"], queued.payload["existing_issue_url"]
                    )
            orphan = store.read_outbox_delivery_record(rows[0]["delivery_id"]).model_copy(
                update={"aggregate_id": "missing-incident"}
            )
            store.write_outbox_delivery_record(orphan)
            response = await get(
                app, "/v1/outbox/inspection", headers={"Authorization": "Bearer valid-token"}
            )
            projected = next(
                row
                for row in response.json()["inspection"]["deliveries"]
                if row["delivery_id"] == orphan.delivery_id
            )
            self.assertEqual(projected["routing"]["incident_status"], "unknown")

    def test_failed_history_cannot_hide_active_work_or_newest_failure(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            delivery = _workflow_delivery()
            for index in range(3):
                store.write_outbox_delivery_record(
                    delivery.model_copy(
                        update={
                            "delivery_id": f"failed-{index}",
                            "dedupe_key": f"failed-{index}",
                            "state": "failed",
                            "error_code": "provider_error",
                            "created_at": f"2026-10-25T0{index}:00:00Z",
                        }
                    )
                )
            store.write_outbox_delivery_record(
                delivery.model_copy(update={"created_at": "2026-10-25T03:00:00Z"})
            )
            result = inspect_outbox(store, limit=1)
            self.assertEqual(
                {row["delivery_id"] for row in result.deliveries},
                {delivery.delivery_id, "failed-2"},
            )
            self.assertFalse(result.complete)
            self.assertEqual(result.counts[f"{delivery.kind}:pending"], 1)

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
