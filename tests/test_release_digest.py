from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.contracts.promotion_record import DeploymentEvidence, HealthcheckEvidence
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.release_review import ReleaseReviewStatus
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.release_digest import ReleaseDigestScheduler, waiting_release_list
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import PublicIngressNotificationDriverSet
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class ReleaseDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "store.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.now = datetime.now(timezone.utc)
        self.current, self.head = "a" * 40, "b" * 40
        self.profile = _profile().model_copy(
            update={
                "production_use": "live",
                "release_on_acceptance": "promote",
            }
        )
        self.prod = self.profile.lanes[0]
        self.testing = ProductLaneProfile(context="example-testing", instance="testing")
        self.profile = self.profile.model_copy(update={"lanes": (self.prod, self.testing)})
        self.store.write_product_profile_record(self.profile)
        self.inventory(self.prod, self.current)
        self.inventory(self.testing, self.head)
        self.policy = PublicIngressNotificationPolicyRecord(
            policy_id="director-digest",
            product=self.profile.product,
            release_digest_enabled=True,
            created_at=self.now.isoformat(),
            updated_at=self.now.isoformat(),
            destinations=(
                PublicIngressNotificationDestination(
                    destination_id="director",
                    kind="discord",
                    discord_webhook_secret="existing-director-destination",
                ),
            ),
        )
        self.store.write_public_ingress_notification_policy_record(self.policy)
        self.transport = Mock()
        self.transport.get_json.side_effect = self.read
        factory = patch(
            "control_plane.release_digest.resolve_build_provenance_transport",
            return_value=self.transport,
        )
        factory.start()
        self.addCleanup(factory.stop)
        review = patch(
            "control_plane.release_digest.current_release_review",
            return_value=ReleaseReviewStatus(),
        )
        self.review = review.start()
        self.addCleanup(review.stop)
        self.sender = Mock()
        self.drivers = PublicIngressNotificationDriverSet(
            discord_sender=self.sender,
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/fixture/opaque",
        )

    def inventory(self, lane: ProductLaneProfile, source: str) -> None:
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=lane.context,
                instance=lane.instance,
                source_git_ref=source,
                deployment_record_id="deployment-" + lane.instance,
                updated_at=self.now.isoformat(),
                deploy=DeploymentEvidence(
                    deploy_mode="fixture",
                    target_name="fixture",
                    target_type="application",
                    status="pass",
                ),
                destination_health=HealthcheckEvidence(status="pass"),
                runtime_identity=RuntimeIdentity(
                    product=self.profile.product,
                    context=lane.context,
                    instance=lane.instance,
                    deployment_record_id="deployment-" + lane.instance,
                    artifact_id="fixture-" + source,
                    source_git_ref=source,
                ),
            )
        )

    def read(self, path: str) -> object:
        if "/commits/" in path:
            return {"sha": self.head}
        return {
            "status": "ahead",
            "total_commits": 2,
            "commits": [
                {
                    "sha": "c" * 40,
                    "commit": {
                        "message": "Improve checkout",
                        "committer": {"date": (self.now - timedelta(days=3)).isoformat()},
                    },
                },
                {
                    "sha": self.head,
                    "commit": {
                        "message": "Fix receipt",
                        "committer": {"date": self.now.isoformat()},
                    },
                },
            ],
        }

    def scheduler(self) -> ReleaseDigestScheduler:
        return ReleaseDigestScheduler(
            self.store,
            control_plane_root=self.root,
            public_url="https://launchplane.example.test",
            clock=lambda: self.now.timestamp(),
        )

    def worker(self) -> str:
        return run_outbox_worker_once(
            record_store=self.store,
            control_plane_root=self.root,
            lease_owner="fixture-worker",
            notification_drivers=self.drivers,
        ).status

    def test_daily_list_has_changes_age_blocker_and_invitation_without_incident(self) -> None:
        self.assertTrue(self.scheduler().run_once())
        self.assertFalse(self.scheduler().run_once())
        self.assertEqual(self.worker(), "delivered")
        self.assertEqual(self.worker(), "idle")
        payload = self.sender.call_args.args[1]
        body = payload["content"]
        self.assertIn("Improve checkout", body)
        self.assertIn("Fix receipt", body)
        self.assertIn(str(3 * 24) + " hours", body)
        self.assertIn("Client acceptance pending", body)
        self.assertIn("/ui/owner-review?product=" + self.profile.product, body)
        self.assertNotIn("color", payload)
        self.assertEqual(self.store.list_public_ingress_incident_records(), ())
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )
        self.now += timedelta(days=1)
        self.assertTrue(self.scheduler().run_once())
        self.assertEqual(len(self.store.list_outbox_delivery_records(kind="release_digest")), 2)

    def test_own_product_links_promotion_and_lists_testing_and_review_blockers(self) -> None:
        self.profile = self.profile.model_copy(
            update={"release_on_acceptance": "director_standing"}
        )
        self.inventory(self.testing, self.current)
        self.review.return_value = ReleaseReviewStatus(unavailable_reason="github_read_failed")
        body = waiting_release_list(
            self.store,
            self.profile,
            read=self.read,
            now=self.now,
            public_url="https://launchplane.example.test",
            control_plane_root=self.root,
        )
        assert body is not None
        self.assertIn("/environments/prod/actions", body.content)
        self.assertIn("not in testing", body.content)
        self.assertIn("Release review unavailable", body.content)
        self.assertNotIn("Send the Client", body.content)

    def test_up_to_date_product_is_quiet(self) -> None:
        self.head = self.current
        self.assertTrue(self.scheduler().run_once())
        self.assertEqual(self.worker(), "idle")
        self.sender.assert_not_called()
        self.review.assert_not_called()

    def test_existing_alert_policy_does_not_opt_in_recipients(self) -> None:
        self.store.write_public_ingress_notification_policy_record(
            self.policy.model_copy(update={"release_digest_enabled": False})
        )
        self.scheduler().run_once()
        self.transport.get_json.assert_not_called()
        self.assertEqual(self.worker(), "idle")

    def test_disable_after_enqueue_suppresses_delivery(self) -> None:
        self.scheduler().run_once()
        self.store.write_public_ingress_notification_policy_record(
            self.policy.model_copy(update={"release_digest_enabled": False})
        )
        self.assertEqual(self.worker(), "delivered")
        self.sender.assert_not_called()

    def test_source_failure_records_partial_slot_and_never_invents_changes(self) -> None:
        self.transport.get_json.side_effect = RuntimeError("provider offline")
        with self.assertLogs("control_plane.release_digest", level="ERROR"):
            self.scheduler().run_once()
        self.assertEqual(self.worker(), "idle")
        receipt = self.store.read_idempotency_record(
            scope=self.scheduler().scope,
            route_path=self.scheduler().route,
            idempotency_key=str(self.scheduler().slot()),
        )
        assert receipt is not None
        self.assertEqual(receipt.response_payload["status"], "partial")

    def test_ambiguous_discord_send_preserves_marker_and_does_not_repeat(self) -> None:
        self.scheduler().run_once()
        self.sender.side_effect = TimeoutError("response lost")
        self.assertEqual(self.worker(), "failed")
        self.assertEqual(self.worker(), "idle")
        delivery = self.store.list_outbox_delivery_records(kind="release_digest")[0]
        self.assertTrue(delivery.provider_operation_key)
        self.assertEqual(self.sender.call_count, 1)

    def test_diverged_source_is_unknown_not_a_waiting_release(self) -> None:
        self.transport.get_json.side_effect = lambda path: (
            {"sha": self.head} if "/commits/" in path else {"status": "diverged"}
        )
        with self.assertLogs("control_plane.release_digest", level="ERROR"):
            self.scheduler().run_once()
        self.assertEqual(self.worker(), "idle")

    def test_restarted_scheduler_deduplicates_queue_after_interrupted_pass(self) -> None:
        self.scheduler()._run_sweep()
        self.scheduler().run_once()
        self.assertEqual(len(self.store.list_outbox_delivery_records(kind="release_digest")), 1)

    def test_release_between_queue_and_send_drops_stale_list(self) -> None:
        self.scheduler().run_once()
        self.inventory(self.prod, self.head)
        self.assertEqual(self.worker(), "delivered")
        self.sender.assert_not_called()
