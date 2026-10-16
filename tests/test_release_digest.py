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
from control_plane.contracts.release_review import ReleaseReviewStatus, ReleaseVersion
from control_plane.contracts.artifact_identity import (
    ArtifactAddonSource,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.notifications import DiscordNotificationError, post_discord_webhook
from control_plane.outbound_http import PublicHttpResponse
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.release_digest import (
    ReleaseDigestScheduler,
    waiting_release_list,
    deliver_release_digest,
    DigestBlocker,
    DigestAction,
    WaitingReleaseList,
)
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
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
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
                    "parents": [{"sha": self.current}],
                    "commit": {
                        "message": "Improve checkout",
                        "committer": {"date": (self.now - timedelta(days=3)).isoformat()},
                    },
                },
                {
                    "sha": self.head,
                    "parents": [{"sha": "c" * 40}],
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

    def waiting(self) -> WaitingReleaseList | None:
        return waiting_release_list(
            self.store,
            self.profile,
            read=self.read,
            now=self.now,
            public_url="https://launchplane.example.test",
            control_plane_root=self.root,
        )

    def worker(self) -> str:
        with (
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: self.now.isoformat().replace("+00:00", "Z"),
            ),
            patch(
                "control_plane.release_digest.deliver_release_digest",
                side_effect=lambda **kwargs: deliver_release_digest(
                    **kwargs, clock=lambda: self.now.timestamp()
                ),
            ),
        ):
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
        waiting = self.waiting()
        assert waiting is not None
        self.assertEqual(waiting.waiting_hours, int(timedelta(days=3).total_seconds() // 3600))
        self.assertIn(DigestBlocker.ACCEPTANCE_PENDING, waiting.blockers)
        self.assertEqual(waiting.action, DigestAction.INVITATION)
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
        self.assertEqual(body.action, DigestAction.PROMOTION)
        self.assertIn(DigestBlocker.TESTING_BEHIND, body.blockers)
        self.assertIn(DigestBlocker.REVIEW_UNAVAILABLE, body.blockers)

    def test_up_to_date_product_is_quiet(self) -> None:
        self.head = self.current
        self.inventory(self.testing, self.current)
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

    def test_feature_branch_commit_age_does_not_overstate_time_waiting(self) -> None:
        comparison = self.read("/compare/")
        assert isinstance(comparison, dict)
        comparison["commits"].insert(
            0,
            {
                "sha": "d" * 40,
                "commit": {
                    "message": "Old feature work",
                    "committer": {"date": (self.now - timedelta(days=300)).isoformat()},
                },
                "parents": [{"sha": self.current}],
            },
        )
        comparison["total_commits"] += 1
        waiting = waiting_release_list(
            self.store,
            self.profile,
            read=lambda path: {"sha": self.head} if "/commits/" in path else comparison,
            now=self.now,
            public_url="https://launchplane.example.test",
            control_plane_root=self.root,
        )
        assert waiting is not None
        self.assertEqual(waiting.waiting_hours, int(timedelta(days=3).total_seconds() // 3600))

    def test_shared_addon_only_odoo_release_is_listed_from_release_tuple(self) -> None:
        self.profile = self.profile.model_copy(update={"driver_id": "odoo"})
        self.head = self.current
        for lane, shared in ((self.prod, "a" * 40), (self.testing, "b" * 40)):
            artifact = ArtifactIdentityManifest(
                artifact_id="odoo-" + lane.instance,
                source_commit=self.current,
                enterprise_base_digest="sha256:fixture",
                image=ArtifactImageReference(
                    repository="ghcr.io/example/site", digest="sha256:" + shared
                ),
                addon_sources=(
                    ArtifactAddonSource(repository="example/shared-addons", ref=shared),
                ),
            )
            self.store.write_artifact_manifest(artifact)
            self.store.write_release_tuple_record(
                ReleaseTupleRecord(
                    tuple_id="odoo-" + lane.instance,
                    context=lane.context,
                    channel=lane.instance,
                    artifact_id=artifact.artifact_id,
                    repo_shas={self.profile.repository: self.current},
                    provenance="ship",
                    minted_at=self.now.isoformat(),
                )
            )
        waiting = self.waiting()
        assert waiting is not None
        self.assertIsNone(waiting.waiting_hours)
        self.assertNotIn(DigestBlocker.TESTING_BEHIND, waiting.blockers)
        self.assertNotIn(DigestBlocker.TESTING_UNAVAILABLE, waiting.blockers)

    def test_website_settings_only_release_is_listed(self) -> None:
        self.head = self.current
        production = ReleaseVersion(
            artifact_id="same-artifact",
            source_commit=self.current,
            website_settings_sha256="a" * 64,
        )
        candidate = production.model_copy(update={"website_settings_sha256": "b" * 64})
        with patch(
            "control_plane.release_digest.release_version",
            side_effect=lambda **kwargs: production if kwargs["instance"] == "prod" else candidate,
        ):
            self.assertIsNotNone(self.waiting())

    def test_rate_limit_records_known_refusal_and_retries_safely(self) -> None:
        self.scheduler().run_once()
        self.sender.side_effect = DiscordNotificationError(429)
        self.assertEqual(self.worker(), "pending")
        delivery = self.store.list_outbox_delivery_records(kind="release_digest")[0]
        self.assertFalse(delivery.provider_operation_key)
        self.sender.side_effect = None
        # Advance only this row's due time; the real worker still claims/completes it.
        self.store.write_outbox_delivery_record(
            delivery.model_copy(update={"next_attempt_at": delivery.created_at})
        )
        self.assertEqual(self.worker(), "delivered")
        self.assertEqual(self.sender.call_count, 2)

    def test_unavailable_destination_recovers_without_a_send_marker(self) -> None:
        self.scheduler().run_once()
        self.drivers.secret_resolver = lambda _name: ""
        with patch(
            "control_plane.release_digest.managed_notification_secret_value", return_value=""
        ):
            self.assertEqual(self.worker(), "pending")
        delivery = self.store.list_outbox_delivery_records(kind="release_digest")[0]
        self.assertFalse(delivery.provider_operation_key)
        self.sender.assert_not_called()

    def test_expired_slot_and_retired_product_send_nothing(self) -> None:
        self.scheduler().run_once()
        self.now += timedelta(seconds=self.scheduler().interval)
        self.assertEqual(self.worker(), "delivered")
        self.sender.assert_not_called()
        self.scheduler().run_once()
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"lifecycle_state": "retired"})
        )
        self.assertEqual(self.worker(), "delivered")
        self.sender.assert_not_called()

    def test_http_refusal_is_distinguishable_from_a_lost_response(self) -> None:
        response = PublicHttpResponse(
            status_code=429,
            final_url="https://discord.com/api/webhooks/fixture/opaque",
            redirect_count=0,
            headers=(),
            body=b"",
        )
        with patch("control_plane.notifications.request_public_http", return_value=response):
            with self.assertRaises(DiscordNotificationError) as caught:
                post_discord_webhook(
                    "https://discord.com/api/webhooks/fixture/opaque", {"content": "fixture"}
                )
        self.assertEqual(caught.exception.status_code, response.status_code)
