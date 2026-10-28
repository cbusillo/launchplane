from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
import unittest
import subprocess
from unittest.mock import Mock, patch

from control_plane import client_release as releases, production_recipient as recipient
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
from control_plane.production_recipient_cutover import RecipientCutoverAdapter
from control_plane.production_delivery_monitor import (
    _delivery_failure_reference,
    _scan_failures,
    monitor_production_delivery_once,
    record_production_delivery_failure,
)
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneIdempotencyRow,
    LaunchplaneProductionRecipientRow,
)
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from tests import test_production_recipient_cutover as cutover_tests
from tests import test_production_recipient_release_settings as settings_tests
from tests.test_production_recipient_writer_fence import WriterFenceHarness
from tests.test_production_backup_provider import BackupHost
from control_plane.workflows.production_backup_gate_operation_worker import (
    run_production_backup_gate_operation_worker_once,
)
from control_plane.contracts.production_backup_authority import (
    ProxmoxGuestBackupDestinationReference,
)


class NativeDeliveryHistory(WriterFenceHarness, unittest.TestCase):
    store: PostgresRecordStore
    seed: Callable[[cutover_tests.RecipientCutoverTests], None]
    database_url = ""
    old_database_url = ""
    old_failure = "rollback"
    old_attempt = 1
    complete_current = True

    def setUp(self) -> None:
        self.old_case = settings_tests.RecipientReleaseSettingsTests()
        self.old_case.database_url = self.old_database_url
        self.seed = cutover_tests.RecipientCutoverTests.seed
        self.preparing_old = True
        with patch.object(cutover_tests.RecipientCutoverTests, "seed", self.configure):
            self.old_case.setUp()
        self.addCleanup(self.old_case.doCleanups)
        old = self.old_case.case.fixture
        self.old_decision = self.old_case.case.decision.model_copy(
            update={
                "record_id": "failed-native-release",
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.old_case.case.decision = self.old_decision
        old.store.write_release_review_decision_record(self.old_decision)
        if self.old_attempt == 2:
            self.old_case.capture_next_backup()
            self.old_case.advance()
            self.old_case.advance()
        if self.old_failure.startswith("backup"):
            self.fail_backup()
        elif self.old_failure in {"preflight", "checkpointed"}:
            self.old_case.capture_next_backup()

            def refuse(_record: Any, checkpoint: Any, _lease: Any) -> None:
                if self.old_failure == "checkpointed":
                    checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                raise ValueError("The upstream preflight failed")

            with patch.object(RecipientCutoverAdapter, "verify_upstreams", side_effect=refuse):
                releases.advance_client_releases(
                    store=old.store, control_plane_root=old.fixture.root
                )
        elif self.old_failure == "fenced":
            self.old_case.capture_next_backup()
            self.store = old.store
            self.product, self.context = old.record.product, old.record.context
            self.base = old.fixture.root.resolve() / "writer-custody"
            self.placements: list[str] = []
            self.reads: list[str] = []

            def lose_pause(*args: Any, **kwargs: Any) -> Any:
                if kwargs["old"] and kwargs["action"] == "pause":
                    kwargs["checkpoint"](ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                    raise TimeoutError("Lost before the fixed writer job ran")
                return old.writer(*args, **kwargs)

            with patch(
                "control_plane.production_recipient_cutover.writer_action", side_effect=lose_pause
            ):
                releases.advance_client_releases(
                    store=old.store, control_plane_root=old.fixture.root
                )
            operation_id = releases.client_release_step_operation_id(
                profile=self.old_case.profile,
                decision=self.old_decision,
                step=releases.ClientReleaseStep("promote", self.old_attempt),
            )
            with patch.object(
                old.store, "_database_mutation_timestamp", return_value=self.expired(operation_id)
            ):
                self.fence(operation_id)
            self.old_case.advance()
        else:
            self.old_case.capture_next_backup()
            with patch(
                "control_plane.production_recipient_cutover._verify_health_evidence_with_identity",
                side_effect=ValueError("Destination health failed"),
            ):
                self.old_case.advance()
        prior = recipient.custody(old.store, old.record.product, old.record.context)
        assert prior is not None
        self.prior = prior
        old_lane = next(lane for lane in self.old_case.profile.lanes if lane.instance == "prod")
        _scan_failures(
            old.store, self.old_case.profile, old_lane, datetime.now(timezone.utc).isoformat()
        )
        self.old_views = releases.read_client_release_step_views(
            store=old.store, profile=self.old_case.profile, decision=self.old_decision
        )
        self.old_operation_id = releases.client_release_step_operation_id(
            profile=self.old_case.profile,
            decision=self.old_decision,
            step=releases.ClientReleaseStep("promote", self.old_attempt),
        )
        self._old_receipt = old.store.read_idempotency_record_by_id(self.old_operation_id)
        self.old_candidate = self.old_decision.checklist.candidate.artifact_id
        self.old_observations = old.store.list_public_ingress_observation_records(
            check_name_prefix="production-delivery-"
        )
        self.old_incident = next(
            row
            for row in old.store.list_public_ingress_incident_records(status="open")
            if row.check_name.startswith("production-delivery-")
        )
        self.new_case = settings_tests.RecipientReleaseSettingsTests()
        self.new_case.database_url = self.database_url
        self.preparing_old = False
        with patch.object(cutover_tests.RecipientCutoverTests, "seed", self.configure):
            self.new_case.setUp()
        self.addCleanup(self.new_case.doCleanups)
        current = self.new_case.case.fixture
        self.store = current.store
        self.profile = self.new_case.profile
        self.lane = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        self.clock = max(
            datetime.now(timezone.utc), datetime.fromisoformat(self.old_incident.latest_observed_at)
        ) + timedelta(seconds=2)
        self.decision = self.new_case.case.decision.model_copy(
            update={"decided_at": self.clock.isoformat()}
        )
        self.new_case.case.decision = self.decision
        self.store.write_release_review_decision_record(self.decision)
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            side_effect=lambda _session: self.clock.isoformat(),
        ):
            self.new_case.capture_next_backup()
            self.new_case.advance()
            self.new_case.advance()
            self.new_case.capture_next_backup()
            with patch.object(
                RecipientCutoverAdapter,
                "verify_upstreams",
                side_effect=ValueError("Current final promotion preflight failed"),
            ):
                releases.advance_client_releases(
                    store=self.store, control_plane_root=current.fixture.root
                )
            _scan_failures(
                self.store, self.profile, self.lane, datetime.now(timezone.utc).isoformat()
            )
            self.current_incident = next(
                row
                for row in self.store.list_public_ingress_incident_records(status="open")
                if row.incident_id != self.old_incident.incident_id
                and row.check_name.startswith("production-delivery-")
            )
            if self.complete_current:
                last_failure = max(
                    datetime.fromisoformat(_delivery_failure_reference(failure).failed_at)
                    for failure in self.store.list_delivery_failure_observations(
                        incident_id=self.current_incident.incident_id
                    )
                )
                self.clock = max(self.clock, last_failure) + timedelta(seconds=1)
                self.new_case.advance()
        self.replay_history()
        record = recipient.custody(self.store, current.record.product, current.record.context)
        assert record is not None
        self.record = record
        run = releases.read_client_release_run(
            store=self.store, profile=self.profile, decision=self.decision
        )
        assert run is not None
        if self.complete_current:
            self.assertEqual(run.state, "passed")
        else:
            self.assertNotEqual(run.state, "passed")
        self.final = self.store.read_idempotency_record_by_id(run.steps[-1].operation_id)
        self.now = self.clock + timedelta(seconds=1)
        if self.final is not None:
            self.now = datetime.fromisoformat(self.final.updated_at) + timedelta(seconds=1)
        self.health()
        transport = Mock()
        transport.get_json.return_value = {"sha": self.record.request.source_commit}
        self.factory = Mock(return_value=transport)

    @property
    def old_receipt(self) -> LaunchplaneIdempotencyRecord:
        assert self._old_receipt is not None
        return self._old_receipt

    def fail_backup(self) -> None:
        old = self.old_case.case.fixture
        operation_id = self.old_case.advance()[0]
        operation = old.store.read_verireel_prod_backup_gate_operation_record(operation_id)
        assert operation.binding is not None
        source = operation.binding.source_target.destination
        assert isinstance(source, ProxmoxGuestBackupDestinationReference)
        backend = BackupHost()
        backend.source = source

        def provider(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "vzdump" in command:
                raise subprocess.TimeoutExpired(command, 1)
            return backend.run(command, **kwargs)

        with (
            patch(
                "control_plane.workflows.production_backup_gate.resolve_guest_capture_values",
                side_effect=ValueError("Capture credential lookup failed")
                if self.old_failure == "backup_preflight"
                else None,
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "synthetic",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "synthetic",
                },
            ),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=provider,
            ),
        ):
            result = run_production_backup_gate_operation_worker_once(
                record_store=old.store,
                control_plane_root_path=old.fixture.root,
                lease_owner="monitor-history-backup",
            )
        self.assertTrue(result.terminal_write_committed)

    def configure(self, case: cutover_tests.RecipientCutoverTests) -> None:
        self.seed(case)
        fixture = case.publication_case.fixture
        fixture.profile = fixture.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "health_monitoring": ProductLaneHealthMonitoringPolicy(
                                monitoring_intent="private",
                                checks=(
                                    ProductLaneHealthCheck(
                                        name="runtime",
                                        kind="private_http",
                                        private_endpoint_key=case.publication_case.monitor.endpoint_key,
                                        require_runtime_identity=True,
                                    ),
                                ),
                            )
                        }
                    )
                    if lane.instance == "prod"
                    else lane
                    for lane in fixture.profile.lanes
                )
            }
        )
        fixture.store.write_product_profile_record(fixture.profile)
        if self.preparing_old:
            fixture.body = fixture.body.model_copy(update={"source_commit": "e" * 40})
            fixture.adapter.request = fixture.body
            image = getattr(recipient, "verified_product_image")
            assert isinstance(image, Mock)
            image.return_value = "example/site@sha256:" + "e" * 64

    def replay_history(self) -> None:
        old = self.old_case.case.fixture
        self.store.write_release_review_decision_record(self.old_decision)
        for view in self.old_views:
            if view.kind == "backup":
                try:
                    operation = old.store.read_verireel_prod_backup_gate_operation_record(
                        view.operation_id
                    )
                except FileNotFoundError:
                    continue
                self.store.write_verireel_prod_backup_gate_operation_record(operation)
                self.store.write_backup_gate_record(
                    old.store.read_backup_gate_record(operation.backup_record_id)
                )
            else:
                receipt = old.store.read_idempotency_record_by_id(view.operation_id)
                if receipt is not None:
                    if receipt.state == "completed":
                        self.store.write_idempotency_record(receipt)
                    else:
                        self.seed_receipt(receipt)
        for promotion in old.store.list_promotion_records(
            context_name=self.prior.context, to_instance_name="prod"
        ):
            if promotion.artifact_identity.artifact_id == self.old_candidate:
                self.store.write_promotion_record(promotion)
        deployment_id = self.prior.execution["runtime_identity"]["deployment_record_id"]
        try:
            self.store.write_deployment_record(old.store.read_deployment_record(deployment_id))
        except FileNotFoundError:
            pass
        for observation in self.old_observations:
            self.store.write_public_ingress_observation_record(observation)
        self.store.write_public_ingress_incident_record(self.old_incident)
        current = recipient.custody(self.store, self.prior.product, self.prior.context)
        assert current is not None
        self.seed_custody(
            current.model_copy(
                update={
                    "prior_attempts": [
                        self.prior.model_dump(mode="json", exclude={"prior_attempts"})
                    ]
                }
            )
        )

    def seed_custody(self, record: Any) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneProductionRecipientRow, (record.product, record.context))
            assert row is not None
            row.payload = record.model_dump(mode="json")
            session.commit()

    def seed_receipt(self, receipt: LaunchplaneIdempotencyRecord) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneIdempotencyRow, receipt.record_id)
            if row is None:
                row = self.store._idempotency_row(receipt)
                session.add(row)
            self.store._sync_idempotency_row(row, receipt)
            session.commit()

    def health(self, *, mismatch: bool = False) -> None:
        identity = self.store.read_environment_inventory(
            context_name=self.lane.context, instance_name="prod"
        ).runtime_identity
        assert identity is not None
        if mismatch:
            identity = identity.model_copy(update={"source_git_ref": "d" * 40})
        identity_payload = identity.model_dump(mode="json")
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=self.now.isoformat(),
            private_http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                redirect_count=0,
                final_url=url,
                payload={"status": "ok", "runtime_identity": identity_payload},
            ),
        )

    def monitor(self) -> None:
        monitor_production_delivery_once(
            self.store,
            observed_at=self.now.isoformat(),
            transport_factory=self.factory,
        )

    def record_failure(self, trace: str, *, at: datetime | None = None) -> None:
        record_production_delivery_failure(
            self.store,
            self.profile,
            self.lane,
            candidate=self.old_candidate,
            reason="delivery_failed",
            trace_id=trace,
            observed_at=(at or self.now).isoformat(),
        )

    def assert_open(self) -> None:
        self.monitor()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(self.old_incident.incident_id).status,
            "open",
        )

    def assert_recovered(self) -> None:
        receipts = tuple(
            self.store.read_idempotency_record_by_id(view.operation_id)
            for view in self.old_views
            if view.kind != "backup"
        )
        before = self.store.list_delivery_failure_observations(
            incident_id=self.old_incident.incident_id
        )
        self.monitor()
        for incident in (self.old_incident, self.current_incident):
            self.assertEqual(
                self.store.read_public_ingress_incident_record(incident.incident_id).status,
                "resolved",
            )
        self.assertEqual(
            self.store.list_delivery_failure_observations(
                incident_id=self.old_incident.incident_id
            ),
            before,
        )
        self.assertEqual(
            tuple(
                self.store.read_idempotency_record_by_id(view.operation_id)
                for view in self.old_views
                if view.kind != "backup"
            ),
            receipts,
        )


class AcceptedRecipientDeliveryRecoveryTests(NativeDeliveryHistory):
    def test_completed_accepted_release_recovers_retained_failures_and_keeps_history(self) -> None:
        self.assert_recovered()

    def test_unknown_old_effects_do_not_recover(self) -> None:
        saved = self.prior.execution["reservation:" + self.old_receipt.record_id]
        held = LaunchplaneIdempotencyRecord.model_validate({**saved, "state": "reconcile_required"})
        self.seed_receipt(held)
        self.assert_open()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(
                self.current_incident.incident_id
            ).status,
            "resolved",
        )

    def test_failed_rollback_does_not_count_as_settled_effects(self) -> None:
        payload = dict(self.old_receipt.response_payload)
        payload["result"] = {**payload["result"], "rollback_status": "fail"}
        self.store.write_idempotency_record(
            self.old_receipt.model_copy(update={"response_payload": payload})
        )
        self.assert_open()

    def test_new_or_unattributed_failure_stays_open(self) -> None:
        self.record_failure("unknown-operation")
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
        assert self.final is not None
        self.now += timedelta(seconds=3)
        self.store.write_idempotency_record(
            self.final.model_copy(
                update={"updated_at": (self.now - timedelta(seconds=1)).isoformat()}
            )
        )
        self.assert_open()

    def test_later_failure_of_the_same_settled_operation_stays_open(self) -> None:
        self.record_failure(self.old_receipt.record_id)
        self.assert_open()

    def test_runtime_identity_mismatch_stays_open(self) -> None:
        self.now += timedelta(seconds=1)
        self.health(mismatch=True)
        self.assert_open()

    def test_malformed_old_history_does_not_block_current_incident_recovery(self) -> None:
        original = self.record.prior_attempts[0]
        prior = {**original, "execution": {**original["execution"], "release_trace": 1}}
        self.seed_custody(self.record.model_copy(update={"prior_attempts": [prior]}))
        self.assert_open()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(
                self.current_incident.incident_id
            ).status,
            "resolved",
        )
        self.assertEqual(
            {
                incident.incident_id
                for incident in self.store.list_public_ingress_incident_records(status="open")
            },
            {self.old_incident.incident_id},
        )

    def test_concurrent_new_failure_defers_recovery(self) -> None:
        write = self.store.write_public_ingress_transition_with_outbox
        raced = False

        def race(**kwargs: Any) -> object:
            nonlocal raced
            if not raced and kwargs["observation"].status == "pass":
                raced = True
                with patch.object(self.store, "write_public_ingress_transition_with_outbox", write):
                    self.record_failure("concurrent-operation")
            return write(**kwargs)

        with patch.object(
            self.store, "write_public_ingress_transition_with_outbox", side_effect=race
        ):
            self.assert_open()
        self.assertTrue(raced)

    def test_missing_current_publication_refreshes_all_incidents_then_reports_unknown(self) -> None:
        self.seed_custody(self.record.model_copy(update={"publication": {}}))
        self.assert_current_evidence_unknown()

    def test_malformed_current_receipt_refreshes_all_incidents_then_reports_unknown(self) -> None:
        assert self.final is not None
        self.seed_receipt(self.final.model_copy(update={"response_payload": {"result": []}}))
        self.assert_current_evidence_unknown()

    def assert_current_evidence_unknown(self) -> None:
        self.assert_open()
        for incident in (self.old_incident, self.current_incident):
            self.assertEqual(
                self.store.read_public_ingress_incident_record(
                    incident.incident_id
                ).latest_observed_at,
                self.now.isoformat(),
            )
        owned = {self.old_incident.incident_id, self.current_incident.incident_id}
        self.assertTrue(
            {
                incident.incident_id
                for incident in self.store.list_public_ingress_incident_records(status="open")
            }
            - owned
        )


class FencedRecipientDeliveryRecoveryTests(NativeDeliveryHistory):
    old_failure = "fenced"

    def test_fenced_no_writer_effect_also_qualifies(self) -> None:
        self.assert_recovered()


class FinalPromotionDeliveryRecoveryTests(NativeDeliveryHistory):
    old_attempt = 2

    def test_failed_second_promotion_recovers_without_using_the_first_receipt(self) -> None:
        self.assert_recovered()

    def test_another_unsettled_step_fences_the_shared_decision_trace(self) -> None:
        first = self.old_views[1]
        saved = self.prior.execution["reservation:" + first.operation_id]
        held = LaunchplaneIdempotencyRecord.model_validate({**saved, "state": "reconcile_required"})
        self.seed_receipt(held)
        self.assert_open()


class PartialRecipientDeliveryRecoveryTests(NativeDeliveryHistory):
    complete_current = False

    def test_partial_drill_and_healthy_retained_runtime_do_not_recover(self) -> None:
        self.assert_open()
        self.assertEqual(
            self.store.read_public_ingress_incident_record(
                self.current_incident.incident_id
            ).status,
            "open",
        )


class ReleasedPreflightDeliveryRecoveryTests(NativeDeliveryHistory):
    old_failure = "preflight"

    def test_abandoned_preflight_recovers_without_recreating_its_reservation(self) -> None:
        self.assert_recovered()
        self.assertIsNone(self.store.read_idempotency_record_by_id(self.old_operation_id))

    def test_a_retained_unknown_reservation_still_fences_preflight_recovery(self) -> None:
        saved = self.prior.execution["reservation:" + self.old_operation_id]
        self.seed_receipt(
            LaunchplaneIdempotencyRecord.model_validate({**saved, "state": "reconcile_required"})
        )
        self.assert_open()


class ReleasedFinalPreflightDeliveryRecoveryTests(ReleasedPreflightDeliveryRecoveryTests):
    old_attempt = 2


class CheckpointedPreflightDeliveryRecoveryTests(NativeDeliveryHistory):
    old_failure = "checkpointed"

    def test_checkpointed_preflight_keeps_unknown_effects_fenced(self) -> None:
        self.assert_open()


class PrecaptureBackupDeliveryRecoveryTests(NativeDeliveryHistory):
    old_failure = "backup_preflight"
    old_attempt = 2

    def test_positively_precapture_backup_failure_is_settled(self) -> None:
        self.assert_recovered()


class UncertainBackupDeliveryRecoveryTests(NativeDeliveryHistory):
    old_failure = "backup_timeout"
    old_attempt = 2

    def test_terminal_backup_timeout_keeps_uncertain_capture_fenced(self) -> None:
        self.assert_open()
