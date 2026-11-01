from datetime import UTC, datetime, timedelta
import unittest
import json
from unittest.mock import Mock, patch

from control_plane.client_release import read_client_release_run
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.production_delivery_monitor import (
    monitor_production_delivery_once,
    DELIVERY_CHECK_PREFIX,
    _candidate_key,
    record_production_delivery_failure,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import LaunchplaneIdempotencyRow
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from tests import test_generic_web_client_release as release_tests
from tests.support.lane_builds import LaneBuildGitHub
from tests.test_release_review import BASE, HEAD, github_read


class AcceptedInPlaceDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = release_tests.GenericWebClientReleaseTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store
        self.profile = self.case.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "health_monitoring": ProductLaneHealthMonitoringPolicy(
                                monitoring_intent="public",
                                checks=(
                                    ProductLaneHealthCheck(
                                        name="runtime",
                                        kind="public_http",
                                        require_runtime_identity=True,
                                    ),
                                ),
                            ),
                        }
                    )
                    for lane in self.case.profile.lanes
                )
            }
        )
        self.case.profile = self.profile
        self.store.write_product_profile_record(self.profile)
        self.pre_effect = self.case.accept(suffix="-pre-effect")
        self.case.advance()
        self.case.capture()
        with patch(
            "control_plane.generic_web_promotion_provider_adapter.execute_generic_web_prod_promotion_result",
            side_effect=ValueError("Candidate prerequisite changed"),
        ):
            self.case.advance()
        self.old = self.case.accept(suffix="-old")
        self.case.advance()
        self.case.capture()
        self.case.fail_health = True
        self.case.advance()
        self.now = datetime.now(UTC) + timedelta(seconds=1)
        self.transport = Mock()
        self.factory = Mock(return_value=self.transport)
        self.transport.get_json.return_value = {"sha": self.old.checklist.candidate.source_commit}
        self.monitor()
        self.incident = next(
            incident
            for incident in self.store.list_public_ingress_incident_records(status="open")
            if incident.check_name.startswith("production-delivery-")
            and incident.check_name
            not in {
                DELIVERY_CHECK_PREFIX + _candidate_key("monitor:source"),
                DELIVERY_CHECK_PREFIX + _candidate_key("monitor:evidence"),
            }
        )
        run = read_client_release_run(store=self.store, profile=self.profile, decision=self.old)
        assert run is not None
        old_receipt = self.store.read_idempotency_record_by_id(run.steps[-1].operation_id)
        assert old_receipt is not None
        self.old_receipt = old_receipt
        self.failures = self.store.list_delivery_failure_observations(
            incident_id=self.incident.incident_id
        )
        self.case.fail_health = False
        inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        source = "d" * 40
        digest = "sha256:" + "c" * 64
        self.case.write_qualified_testing_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={
                            "artifact_id": self.profile.image.repository + "@" + digest,
                            "source_git_ref": source,
                        }
                    ),
                }
            )
        )
        self.case.raw_read = lambda path: json.loads(
            json.dumps(github_read(path)).replace(HEAD, source)
        )
        self.case.enterContext(
            patch(
                "control_plane.product_reconcile.resolve_build_provenance_transport",
                return_value=LaneBuildGitHub(
                    self.profile.repository,
                    self.profile.image.repository,
                    {BASE: "sha256:" + "f" * 64, HEAD: "sha256:" + "b" * 64, source: digest},
                ),
            )
        )
        self.case.enterContext(
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(datetime.now(UTC) + timedelta(seconds=2)).isoformat(),
            )
        )
        self.current = self.case.accept(suffix="-current")
        self.assertTrue(
            self.case.advance(),
            read_client_release_run(store=self.store, profile=self.profile, decision=self.current),
        )
        self.case.capture()
        self.case.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=self.current)
        assert run is not None
        self.assertEqual(run.state, "passed")
        final = self.store.read_idempotency_record_by_id(run.steps[-1].operation_id)
        assert final is not None
        self.final = final
        self.now = datetime.fromisoformat(self.final.updated_at) + timedelta(seconds=1)
        self.health()

    def health(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="prod"
        )
        assert inventory.runtime_identity is not None
        identity_payload = inventory.runtime_identity.model_dump(mode="json")
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=self.now.isoformat(),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="unsupported", failure_code="tls_unsupported", summary="Outside this fixture"
            ),
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                redirect_count=0,
                final_url=url,
                payload={
                    "status": "ok",
                    "runtime_identity": identity_payload,
                },
            ),
        )

    def monitor(self) -> None:
        monitor_production_delivery_once(
            self.store,
            observed_at=self.now.isoformat(),
            transport_factory=self.factory,
        )

    def test_completed_accepted_in_place_release_recovers_older_failure_and_keeps_history(
        self,
    ) -> None:
        self.assertNotEqual(self.old.checklist.candidate, self.current.checklist.candidate)
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.incident.incident_id).status,
            "resolved",
        )
        self.assertEqual(
            self.store.list_delivery_failure_observations(incident_id=self.incident.incident_id),
            self.failures,
        )
        self.assertEqual(
            self.store.read_idempotency_record_by_id(self.old_receipt.record_id), self.old_receipt
        )

    def seed_receipt(self, receipt: LaunchplaneIdempotencyRecord) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneIdempotencyRow, receipt.record_id)
            assert row is not None
            self.store._sync_idempotency_row(row, receipt)
            session.commit()

    def assert_open(self) -> None:
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.incident.incident_id).status, "open"
        )

    def test_unsettled_original_effects_never_recover(self) -> None:
        for state, rollback in (
            ("reconcile_required", "pass"),
            ("completed", "fail"),
            ("completed", "skipped"),
        ):
            with self.subTest(state=state, rollback=rollback):
                payload = self.old_receipt.model_dump(mode="json")
                payload["state"] = state
                payload["response_payload"]["result"]["rollback_status"] = rollback
                if state == "reconcile_required":
                    payload.update(
                        response_payload={},
                        response_status_code=None,
                        response_trace_id="",
                        recorded_at="",
                    )
                self.seed_receipt(LaunchplaneIdempotencyRecord.model_validate(payload))
                self.assert_open()

    def test_incomplete_or_unaccepted_release_never_recovers(self) -> None:
        self.seed_receipt(
            self.final.model_copy(
                update={
                    "state": "reconcile_required",
                    "response_payload": {},
                    "response_status_code": None,
                    "response_trace_id": "",
                    "recorded_at": "",
                }
            )
        )
        self.assert_open()
        self.seed_receipt(self.final)
        self.store.write_release_review_decision_record(
            self.current.model_copy(update={"decision": "changes_requested"})
        )
        self.assert_open()

    def test_current_deployment_must_still_be_the_accepted_final_deployment(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="prod"
        )
        assert inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "e" * 40}
                    )
                }
            )
        )
        self.health()
        self.assert_open()

    def test_health_must_be_fresh_and_after_final_completion(self) -> None:
        self.now += timedelta(hours=1)
        self.assert_open()
        self.now = datetime.fromisoformat(self.final.updated_at)
        self.health()
        self.assert_open()

    def record_failure(self, trace: str, *, at: datetime) -> None:
        record_production_delivery_failure(
            self.store,
            self.profile,
            next(lane for lane in self.profile.lanes if lane.instance == "prod"),
            candidate=self.old.checklist.candidate.artifact_id,
            reason="delivery_failed",
            trace_id=trace,
            observed_at=at.isoformat(),
        )

    def test_newer_failure_of_a_settled_operation_never_recovers(self) -> None:
        self.record_failure(self.old_receipt.response_trace_id, at=self.now)
        self.assert_open()

    def test_unattributed_failure_before_completion_never_recovers(self) -> None:
        self.record_failure(
            "unattributed-provider-operation",
            at=datetime.fromisoformat(self.final.updated_at) - timedelta(seconds=1),
        )
        self.assert_open()

    def test_current_target_must_still_own_the_final_deployment(self) -> None:
        target = self.store.read_provider_target_record(
            context_name=self.case.context, instance_name="prod"
        )
        self.store.write_provider_target_record(
            target.model_copy(update={"target_id": "replacement-application"})
        )
        self.assert_open()
