from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import os

from control_plane.service_deploy_drain import confirm_startup, prepare
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.test_outbox_worker import _workflow_delivery


class OutboxDeployDrainTests(unittest.TestCase):
    def database_url(self) -> str:
        directory = self.enterContext(TemporaryDirectory())
        return _sqlite_database_url(Path(directory) / "records.sqlite3")

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "DOCKER_IMAGE_REFERENCE": "example.invalid/old-image",
                    "LAUNCHPLANE_DEPLOYMENT_MARKER": "previous-image",
                },
            )
        )
        self.replacement = {
            "DOCKER_IMAGE_REFERENCE": "example.invalid/fixture@sha256:" + "a" * 64,
            "LAUNCHPLANE_DEPLOYMENT_MARKER": "outbox-replacement",
        }
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        now = datetime.fromisoformat(
            _workflow_delivery().created_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        self.clock = self.enterContext(
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=now.isoformat().replace("+00:00", "Z"),
            )
        )

    def test_expired_unmarked_claim_cannot_hold_replacement_or_start_a_provider(self) -> None:
        workflow = _workflow_delivery()
        self.store.write_outbox_delivery_record(workflow)
        claimed = self.store.claim_next_outbox_delivery_record(
            lease_owner="lost-worker", lease_seconds=10
        )
        assert claimed.record is not None
        after_expiry = datetime.fromisoformat(
            claimed.record.lease_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        self.clock.return_value = after_expiry.isoformat().replace("+00:00", "Z")
        _, blockers, dispatch = self.prepare()
        self.assertTrue(dispatch)
        self.assertFalse(blockers)
        started = self.store.mark_outbox_delivery_provider_started(
            record=claimed.record,
            lease_owner="lost-worker",
            provider_operation_key="too-late",
        )
        self.assertEqual(started.status, "lease_expired")

    def prepare(self) -> tuple[object, tuple[str, ...], bool]:
        return prepare(
            self.store,
            request_fingerprint="outbox-replacement",
            target_type="compose",
            target_id="fixture-control-plane",
            image_reference=self.replacement["DOCKER_IMAGE_REFERENCE"],
            deployment_marker=self.replacement["LAUNCHPLANE_DEPLOYMENT_MARKER"],
        )

    def test_replacement_fences_new_dispatches_but_notifications_can_progress(self) -> None:
        workflow = _workflow_delivery()
        self.store.write_outbox_delivery_record(workflow)
        self.prepare()
        claimed = self.store.claim_next_outbox_delivery_record(lease_owner="old-image")
        self.assertEqual(claimed.status, "empty")
        self.assertEqual(self.store.read_outbox_delivery_record(workflow.delivery_id), workflow)
        notification = workflow.model_copy(
            update={
                "delivery_id": "notification",
                "kind": "operator_authorization_recovery_alert",
                "dedupe_key": "notification",
                "payload": {},
            }
        )
        self.store.write_outbox_delivery_record(notification)
        claimed = self.store.claim_next_outbox_delivery_record(lease_owner="old-image")
        self.assertIsNotNone(claimed.record)
        assert claimed.record is not None
        self.assertEqual(claimed.record.delivery_id, notification.delivery_id)
        with patch.dict(os.environ, self.replacement):
            confirm_startup(self.store)
        self.assertEqual(
            self.store.claim_next_outbox_delivery_record(lease_owner="old-image").status, "empty"
        )
        with patch.dict(os.environ, self.replacement):
            resumed = self.store.claim_next_outbox_delivery_record(lease_owner="new-image")
        assert resumed.record is not None
        self.assertEqual(resumed.record.delivery_id, workflow.delivery_id)

    def test_replacement_waits_for_admitted_dispatch_to_finish(self) -> None:
        workflow = _workflow_delivery()
        self.store.write_outbox_delivery_record(workflow)
        claimed = self.store.claim_next_outbox_delivery_record(lease_owner="worker")
        assert claimed.record is not None
        _, blockers, dispatch = self.prepare()
        self.assertFalse(dispatch)
        self.assertIn(workflow.delivery_id, blockers)
        self.store.complete_outbox_delivery_record(
            record=claimed.record.model_copy(update={"state": "delivered", "action": "dispatched"}),
            lease_owner="worker",
        )
        _, blockers, dispatch = self.prepare()
        self.assertTrue(dispatch)
        self.assertFalse(blockers)

    def test_marked_dispatch_can_reconcile_while_new_dispatches_are_fenced(self) -> None:
        workflow = _workflow_delivery().model_copy(
            update={
                "state": "reconcile_required",
                "provider_operation_key": "already-started",
            }
        )
        self.store.write_outbox_delivery_record(workflow)
        self.prepare()
        claimed = self.store.claim_next_outbox_delivery_record(lease_owner="reconciler")
        assert claimed.record is not None
        self.assertEqual(claimed.record.provider_operation_key, workflow.provider_operation_key)

    def test_expired_marked_dispatch_holds_replacement_until_reconciliation_finishes(self) -> None:
        workflow = _workflow_delivery()
        self.store.write_outbox_delivery_record(workflow)
        claimed = self.store.claim_next_outbox_delivery_record(
            lease_owner="worker", lease_seconds=10
        )
        assert claimed.record is not None
        self.store.mark_outbox_delivery_provider_started(
            record=claimed.record,
            lease_owner="worker",
            provider_operation_key="already-started",
        )
        after_expiry = datetime.fromisoformat(
            claimed.record.lease_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        self.clock.return_value = after_expiry.isoformat().replace("+00:00", "Z")
        _, blockers, dispatch = self.prepare()
        self.assertFalse(dispatch)
        self.assertIn(workflow.delivery_id, blockers)
        reconciled = self.store.claim_next_outbox_delivery_record(lease_owner="reconciler")
        assert reconciled.record is not None
        self.store.complete_outbox_delivery_record(
            record=reconciled.record.model_copy(
                update={"state": "delivered", "action": "reconciled"}
            ),
            lease_owner="reconciler",
        )
        _, blockers, dispatch = self.prepare()
        self.assertTrue(dispatch)
        self.assertFalse(blockers)
