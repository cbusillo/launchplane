from datetime import datetime, timedelta, timezone
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane import client_release as releases
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductOwnerProfile,
)
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseVersion,
)
from control_plane.production_delivery_monitor import (
    monitor_production_delivery_once,
    record_production_delivery_failure,
)
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from tests import test_production_recipient_publication as publication_tests


class AcceptedRecipientDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = publication_tests.RecipientPublicationTests()
        with patch.object(self.case, "customize_preparation", side_effect=self.configure_profile):
            self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store
        self.profile = self.case.fixture.profile
        self.lane = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        self.record = self.case.publish()
        self.now = datetime.now(timezone.utc) + timedelta(seconds=10)
        self.finished = self.now - timedelta(seconds=2)
        self.old_artifact = self.profile.image.repository + "@sha256:" + "e" * 64
        self.decision = ReleaseReviewDecisionRecord(
            record_id="accepted-recipient",
            product=self.profile.product,
            checklist_digest="a" * 64,
            checklist=ReleaseChecklist(
                product=self.profile.product,
                repository=self.profile.repository,
                owner_github_id=self.profile.owner.github_id,
                testing_url="",
                production=ReleaseVersion(artifact_id=self.old_artifact, source_commit="e" * 40),
                candidate=ReleaseVersion(
                    artifact_id=self.record.image_reference,
                    source_commit=self.record.request.source_commit,
                ),
                items=(),
            ),
            decision="accepted",
            actor_github_id=self.profile.owner.github_id,
            actor_github_login=self.profile.owner.github_login,
            decided_at=(self.finished - timedelta(minutes=1)).isoformat(),
            release_issue_url="https://github.com/example/site/issues/10",
            release_start="promote_with_rollback_drill",
        )
        self.store.write_release_review_decision_record(self.decision)
        self.views = tuple(
            releases.ClientReleaseStepView(
                step=step.name,
                kind=step.kind,
                status="pass",
                operation_id=releases.client_release_step_operation_id(
                    profile=self.profile, decision=self.decision, step=step
                ),
            )
            for step in releases.client_release_steps(self.decision.release_start)
        )
        self.final = self.receipt(
            self.views[-1].operation_id, "final", self.record.plan_sha256, "pass"
        )
        self.old = self.receipt("old-reservation", "old", "e" * 64, "fail")
        self.old = self.old.model_copy(
            update={
                "response_payload": {
                    "result": {
                        "recipient_plan_sha256": "e" * 64,
                        "promotion_status": "fail",
                        "rollback_status": "pass",
                    }
                }
            }
        )
        self.store.write_idempotency_record(self.old)
        self.store.write_idempotency_record(self.final)
        prior = self.record.model_copy(
            update={
                "plan_sha256": "e" * 64,
                "image_reference": self.old_artifact,
                "request": self.record.request.model_copy(update={"source_commit": "e" * 40}),
                "phase": "rolled_back",
                "execution": {
                    "original_reservation": self.old.model_dump(mode="json"),
                    "promotion_record_id": "failed-promotion",
                },
            }
        )
        self.record = self.record.model_copy(
            update={
                "prior_attempts": [prior.model_dump(mode="json", exclude={"prior_attempts"})],
                "execution": {"release_trace": "client-release-" + self.decision.record_id},
            }
        )
        self.health()
        for trace in (self.old.record_id, self.old.response_trace_id, "failed-promotion"):
            self.record_failure(trace)
        self.incident = next(
            incident
            for incident in self.store.list_public_ingress_incident_records(status="open")
            if incident.check_name.startswith("production-delivery-")
        )
        self.transport = Mock()
        self.transport.get_json.return_value = {"sha": self.record.request.source_commit}

    def configure_profile(self) -> None:
        fixture = self.case.fixture
        fixture.profile = fixture.profile.model_copy(
            update={
                "production_use": "live",
                "owner": ProductOwnerProfile(github_id="1234", github_login="example-client"),
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "health_monitoring": ProductLaneHealthMonitoringPolicy(
                                monitoring_intent="private",
                                checks=(
                                    ProductLaneHealthCheck(
                                        name="runtime",
                                        kind="private_http",
                                        private_endpoint_key=self.case.monitor.endpoint_key,
                                        require_runtime_identity=True,
                                    ),
                                ),
                            )
                        }
                    )
                    if lane.instance == "prod"
                    else lane
                    for lane in fixture.profile.lanes
                ),
            }
        )
        fixture.store.write_product_profile_record(fixture.profile)

    def receipt(
        self, record_id: str, key: str, plan: str, status: str
    ) -> LaunchplaneIdempotencyRecord:
        return LaunchplaneIdempotencyRecord(
            record_id=record_id,
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path="/fixture/promotion",
            idempotency_key=key,
            request_fingerprint="f" * 64,
            response_status_code=202,
            response_trace_id="client-release-" + key,
            recorded_at=(
                self.finished if status == "pass" else self.finished - timedelta(minutes=2)
            ).isoformat(),
            response_payload={
                "result": {"promotion_status": status, "recipient_plan_sha256": plan}
            },
        )

    def health(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name=self.lane.context, instance_name="prod"
        )
        assert inventory.runtime_identity is not None
        identity = inventory.runtime_identity
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=self.now.isoformat(),
            private_http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                redirect_count=0,
                final_url=url,
                payload={
                    "status": "ok",
                    "runtime_identity": identity.model_dump(mode="json"),
                },
            ),
        )

    def record_failure(self, trace: str, *, at: datetime | None = None) -> None:
        record_production_delivery_failure(
            self.store,
            self.profile,
            self.lane,
            candidate=self.old_artifact,
            reason="delivery_failed",
            trace_id=trace,
            observed_at=(at or self.finished - timedelta(minutes=3)).isoformat(),
        )

    def monitor(self) -> None:
        with (
            patch("control_plane.production_recipient.custody", return_value=self.record),
            patch.object(releases, "read_client_release_step_views", return_value=self.views),
        ):
            monitor_production_delivery_once(
                self.store,
                observed_at=self.now.isoformat(),
                transport_factory=lambda _store, _profile: self.transport,
            )

    def assert_open(self) -> None:
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.incident.incident_id).status, "open"
        )

    def test_completed_accepted_release_recovers_retained_failures_and_keeps_history(self) -> None:
        failures = self.store.list_delivery_failure_observations(
            incident_id=self.incident.incident_id
        )
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.incident.incident_id).status,
            "resolved",
        )
        self.assertEqual(
            self.store.list_delivery_failure_observations(incident_id=self.incident.incident_id),
            failures,
        )
        self.assertEqual(self.store.read_idempotency_record_by_id(self.old.record_id), self.old)

    def test_partial_drill_does_not_recover(self) -> None:
        self.views = (*self.views[:-1], self.views[-1].model_copy(update={"status": "not_started"}))
        self.assert_open()

    def test_unknown_old_effects_do_not_recover(self) -> None:
        read = self.store.read_idempotency_record_by_id
        with patch.object(
            self.store,
            "read_idempotency_record_by_id",
            side_effect=lambda record_id: (
                self.old.model_copy(update={"state": "reconcile_required"})
                if record_id == self.old.record_id
                else read(record_id)
            ),
        ):
            self.assert_open()

    def test_failed_rollback_does_not_count_as_settled_effects(self) -> None:
        self.store.write_idempotency_record(
            self.old.model_copy(
                update={
                    "response_payload": {
                        "result": {
                            "recipient_plan_sha256": "e" * 64,
                            "promotion_status": "fail",
                            "rollback_status": "fail",
                        }
                    }
                }
            )
        )
        self.assert_open()

    def test_new_or_unattributed_failure_stays_open(self) -> None:
        self.record_failure("unknown-operation", at=self.now)
        self.assert_open()

    def test_newer_unaccepted_decision_does_not_recover(self) -> None:
        self.store.write_release_review_decision_record(
            self.decision.model_copy(
                update={
                    "record_id": "new-decision",
                    "decision": "changes_requested",
                    "reason": "Rework",
                    "release_start": "",
                    "decided_at": self.now.isoformat(),
                }
            )
        )
        self.assert_open()

    def test_current_authority_drift_does_not_recover(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name=self.lane.context, instance_name="prod"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"source_git_ref": "d" * 40})
        )
        self.assert_open()

    def test_stale_runtime_verification_does_not_recover(self) -> None:
        self.now += timedelta(hours=1)
        self.assert_open()

    def test_health_before_final_completion_does_not_recover(self) -> None:
        self.now += timedelta(seconds=3)
        self.store.write_idempotency_record(
            self.final.model_copy(
                update={"updated_at": (self.now - timedelta(seconds=1)).isoformat()}
            )
        )
        self.assert_open()

    def test_fenced_no_writer_effect_also_qualifies(self) -> None:
        self.store.write_idempotency_record(
            self.old.model_copy(
                update={
                    "response_payload": {
                        "result": {
                            "recipient_plan_sha256": "e" * 64,
                            "promotion_status": "fail",
                            "writer_effect": "none",
                        }
                    }
                }
            )
        )
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.incident.incident_id).status,
            "resolved",
        )

    def test_later_failure_of_the_same_settled_operation_stays_open(self) -> None:
        self.record_failure(self.old.record_id, at=self.now)
        self.assert_open()

    def test_runtime_identity_mismatch_stays_open(self) -> None:
        observation = self.store.list_public_ingress_observation_records(
            check_name="runtime", limit=1
        )[0]
        target = observation.targets[0]
        assert target.observed_runtime_identity is not None
        self.store.write_public_ingress_observation_record(
            observation.model_copy(
                update={
                    "targets": (
                        target.model_copy(
                            update={
                                "observed_runtime_identity": target.observed_runtime_identity.model_copy(
                                    update={"source_git_ref": "d" * 40}
                                )
                            }
                        ),
                    )
                }
            )
        )
        self.assert_open()

    def test_concurrent_new_failure_defers_recovery(self) -> None:
        write = self.store.write_public_ingress_transition_with_outbox
        raced = False

        def race(**kwargs: Any) -> object:
            nonlocal raced
            if not raced and kwargs["observation"].status == "pass":
                raced = True
                with patch.object(self.store, "write_public_ingress_transition_with_outbox", write):
                    self.record_failure("concurrent-operation", at=self.now)
            return write(**kwargs)

        with patch.object(
            self.store, "write_public_ingress_transition_with_outbox", side_effect=race
        ):
            self.assert_open()
        self.assertTrue(raced)
