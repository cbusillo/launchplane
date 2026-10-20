from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from itertools import product
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, patch

import click

from control_plane.contracts.generic_web_extensions import (
    GenericWebApplicationSchedule,
    GenericWebLaneExtensions,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.dokploy import api
from control_plane.generic_web_deploy_http import (
    GenericWebDeployEnvelope,
    execute_generic_web_deploy_result,
)
from control_plane.provider_operations import resume_acquired_provider_operation
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_testing_failure_settlement import TESTING_FAILURE_SETTLEMENT_PHASE
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_application_lifecycle import (
    DokployApplicationLifecycle,
    schedule_payload,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    application_snapshot_for_deploy,
    generic_web_provider_deployment_record_id,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
)
from control_plane.workflows.ship import build_deployment_record
from control_plane.workflows.inventory import build_environment_inventory
from tests.test_generic_web_application_lifecycle import _LifecycleDeployProvider
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url, _seed_generic_web_deploy_target_records
from tests.test_generic_web_deploy_recovery import (
    _RecoveryObservationProvider,
    _create_recovery_app,
    _generic_web_recovery_original_deploy,
    _generic_web_recovery_reservation,
    _generic_web_recovery_target,
    _invoke_recovery,
    _invoke_recovery_apply,
    _write_generic_web_recovery_reservation,
)


class _LifecycleRecoveryProvider(_RecoveryObservationProvider):
    def application_lifecycle(self, **kwargs: Any) -> DokployApplicationLifecycle:
        return DokployApplicationLifecycle(
            "fixture",
            "fixture",
            kwargs["target_id"],
            kwargs["snapshot"],
            kwargs["checkpoint"],
            kwargs["effect_started"],
        )


class GenericWebTestingFailureSettlementTests(TestCase):
    @contextmanager
    def scenario(
        self,
        *,
        job_status: str = "done",
        receipt_state: str = "prepared",
        phase: str = "deploy_trigger",
        runtime_changes: dict[str, str] | None = None,
    ) -> Iterator[None]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.store = PostgresRecordStore(
                database_url=sqlite_database_url(root / "state.sqlite3")
            )
            self.store.ensure_schema()
            self.profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
            self.store.write_product_profile_record(self.profile)
            self.target = _generic_web_recovery_target()
            _seed_generic_web_deploy_target_records(
                store=self.store,
                context=self.target.ship_request.context,
                instance=self.target.ship_request.instance,
                target_id=self.target.resolved_target.target_id,
                target_name=self.target.resolved_target.target_name,
            )
            self.original = _generic_web_recovery_original_deploy()
            self.reservation = _write_generic_web_recovery_reservation(
                self.store,
                _generic_web_recovery_reservation(
                    original_deploy=self.original,
                    idempotency_key="original",
                    provider_effect_phase=phase,
                ),
            )
            self.provider = _LifecycleRecoveryProvider(
                GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status=job_status,
                    deployment_id="provider-job",
                    started_at="2026-08-15T12:00:00Z",
                    finished_at="2026-08-15T12:00:01Z",
                ),
                reservation=self.reservation,
            )
            self.record_id = generic_web_provider_deployment_record_id(
                self.provider.original_operation_key,
                context=self.target.ship_request.context,
                instance=self.target.ship_request.instance,
            )
            definitions = tuple(
                GenericWebApplicationSchedule(
                    name=name, command="./job", cron_expression="*/5 * * * *"
                )
                for name in ("first", "second")
            )
            self.schedules = {
                item.name: schedule_payload(
                    item, self.target.resolved_target.target_id, enabled=True
                )
                for item in definitions
            }
            self.receipt = ApplicationLifecycleSnapshot(
                product=self.profile.product,
                context=self.target.ship_request.context,
                instance=self.target.ship_request.instance,
                provider_id=self.provider.provider_id,
                target_id=self.target.resolved_target.target_id,
                artifact_id=self.target.ship_request.artifact_id,
                source_git_ref=self.target.ship_request.source_git_ref,
                extensions=GenericWebLaneExtensions(schedules=definitions),
                managed_names=tuple(self.schedules),
                originals=dict(self.schedules),
                state=receipt_state,  # type: ignore[arg-type]
            )
            self.store.write_deployment_record(
                build_deployment_record(
                    request=self.target.ship_request,
                    record_id=self.record_id,
                    deployment_id="provider-job",
                    deployment_status="pending",
                    started_at="2026-08-15T12:00:00Z",
                    finished_at="",
                    resolved_target=self.target.resolved_target,
                    post_deploy_update=PostDeployUpdateEvidence(evidence=self.receipt.evidence()),
                )
            )
            self.app = _create_recovery_app(root=root, store=self.store)
            lookup_original = self.store.lookup_existing_mutation_reservation

            def lookup(**kwargs: Any) -> Any:
                result = lookup_original(**kwargs)
                if result.record is not None and result.record.provider_effect_started_at:
                    observed = parse_launchplane_mutation_timestamp(
                        result.record.provider_effect_started_at, field_name="checkpoint"
                    ) + timedelta(seconds=self.target.deploy_timeout_seconds + 1)
                    return result._replace(observed_at=observed.isoformat())
                return result

            def read(**kwargs: object) -> api.JsonObject | None:
                value = self.schedules.get(str(kwargs["schedule_name"]))
                return dict(value) if value is not None else None

            def upsert(**kwargs: object) -> api.JsonObject:
                payload = cast(api.JsonObject, kwargs["schedule_payload"])
                self.schedules[str(payload["name"])] = dict(payload)
                return {**payload, "scheduleId": payload["name"]}

            self.upsert = Mock(side_effect=upsert)
            runtime_read = self.provider.observe_testing_application_runtime
            self.base_runtime_read = runtime_read

            def runtime(**kwargs: Any) -> Any:
                observed = runtime_read(**kwargs)
                return replace(
                    observed,
                    running_container_images=(),
                    running_container_deployment_record_ids=(),
                    running_container_health=(),
                    pending_task_count=1,
                    rollout_state=(runtime_changes or {}).get(
                        "rollout_state", observed.rollout_state
                    ),
                    service_deployment_record_id=(runtime_changes or {}).get(
                        "service_deployment_record_id", observed.service_deployment_record_id
                    ),
                    service_image=(runtime_changes or {}).get(
                        "service_image", observed.service_image
                    ),
                    rollout_started_at=(runtime_changes or {}).get(
                        "rollout_started_at", observed.rollout_started_at
                    ),
                )

            try:
                with (
                    patch.object(
                        self.store, "lookup_existing_mutation_reservation", side_effect=lookup
                    ),
                    patch.object(
                        self.provider, "observe_testing_application_runtime", side_effect=runtime
                    ),
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                        return_value=self.provider,
                    ),
                    patch.object(api, "read_dokploy_application_schedule", side_effect=read),
                    patch.object(api, "upsert_dokploy_application_schedule", self.upsert),
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
                        side_effect=AssertionError("ordinary deploy must not run"),
                    ),
                    patch.object(
                        DokployApplicationLifecycle,
                        "capture",
                        side_effect=AssertionError("baseline recapture must not run"),
                    ),
                    patch.object(
                        DokployApplicationLifecycle,
                        "prepare",
                        side_effect=AssertionError("prepare must not run"),
                    ),
                    patch.object(
                        DokployApplicationLifecycle,
                        "maintain",
                        side_effect=AssertionError("maintenance must not run"),
                    ),
                    patch.object(
                        DokployApplicationLifecycle,
                        "complete",
                        side_effect=AssertionError("activation must not run"),
                    ),
                    patch.object(
                        DokployApplicationLifecycle,
                        "restore",
                        side_effect=AssertionError("restore must not run"),
                    ),
                ):
                    yield
            finally:
                self.store.close()

    def dry(self) -> tuple[int, dict[str, Any]]:
        return _invoke_recovery(
            self.app,
            original_deploy=self.original,
            idempotency_key=self.reservation.idempotency_key,
            reason="Review original failure hold.",
        )

    def apply(self, dry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return _invoke_recovery_apply(
            self.app,
            original_deploy=self.original,
            idempotency_key=self.reservation.idempotency_key,
            reason="Review original failure hold.",
            expected_recovery_digest=dry["recovery_digest"],
        )

    def test_pending_partial_and_legacy_held_receipts_settle_failure_without_redeploy(self) -> None:
        for job, state, phase in (
            ("done", "prepared", "deploy_trigger"),
            ("done", "prepared", "application_schedule_hold"),
            ("done", "held", "records_commit"),
            ("error", "prepared", "deploy_trigger"),
            ("error", "held", "application_schedule_hold"),
        ):
            with (
                self.subTest(job=job, state=state, phase=phase),
                self.scenario(job_status=job, receipt_state=state, phase=phase),
            ):
                if phase == "records_commit":
                    record = self.store.read_deployment_record(self.record_id)
                    self.store.write_deployment_record(
                        record.model_copy(
                            update={
                                "deploy": record.deploy.model_copy(update={"status": "fail"}),
                            }
                        )
                    )
                status, dry = self.dry()
                self.assertEqual(status, 200, dry)
                self.assertEqual(dry["proposed_action"], "settle_testing_failure")
                self.assertEqual(dry["provider_outcome"], "present")
                self.assertFalse(dry["retry_safe"])
                self.upsert.assert_not_called()
                status, applied = self.apply(dry)
                self.assertEqual(status, 202, applied)
                self.assertFalse(any(item["enabled"] for item in self.schedules.values()))
                stored = self.store.read_deployment_record(self.record_id)
                receipt = recorded_application_lifecycle(stored)
                assert receipt is not None
                self.assertEqual(receipt.state, "held")
                self.assertEqual(receipt.originals, self.receipt.originals)
                self.assertEqual(stored.deploy.status, "fail")
                self.assertFalse(self.store.list_held_provider_target_reservations())
                self.assertFalse(self.store.list_environment_inventory())
                writes = self.upsert.call_count
                reads = self.provider.observation_calls
                replay_status, replay = self.apply(dry)
                self.assertEqual(replay_status, 202, replay)
                self.assertEqual(replay["recovery_action"], "settle_testing_failure")
                self.assertEqual(self.upsert.call_count, writes)
                self.assertEqual(self.provider.observation_calls, reads)

    def test_partial_hold_failure_keeps_lease_fence_then_retries_only_remaining_hold(self) -> None:
        with self.scenario(job_status="error"):
            status, dry = self.dry()
            self.assertEqual(status, 200)
            upsert = self.upsert.side_effect

            def fail_second(**kwargs: object) -> api.JsonObject:
                payload = cast(api.JsonObject, kwargs["schedule_payload"])
                if payload["name"] == "second":
                    raise click.ClickException("schedule update interrupted")
                assert callable(upsert)
                return cast(api.JsonObject, upsert(**kwargs))

            self.upsert.side_effect = fail_second
            status, _failed = self.apply(dry)
            self.assertEqual(status, 409)
            self.assertFalse(self.schedules["first"]["enabled"])
            self.assertTrue(self.schedules["second"]["enabled"])
            held = self.store.list_held_provider_target_reservations()
            self.assertTrue(held)
            self.assertEqual(held[0].provider_effect_phase, TESTING_FAILURE_SETTLEMENT_PHASE)
            self.upsert.side_effect = upsert
            status, second = self.dry()
            self.assertEqual(status, 200)
            self.assertNotEqual(second["recovery_digest"], dry["recovery_digest"])
            status, applied = self.apply(second)
            self.assertEqual(status, 202, applied)
            self.assertFalse(any(item["enabled"] for item in self.schedules.values()))
            self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_changed_receipt_invalidates_review_without_provider_write(self) -> None:
        with self.scenario():
            _, dry = self.dry()
            record = self.store.read_deployment_record(self.record_id)
            self.receipt.originals["first"] = None
            self.store.write_deployment_record(
                record.model_copy(
                    update={
                        "post_deploy_update": record.post_deploy_update.model_copy(
                            update={
                                "evidence": self.receipt.evidence(),
                            }
                        ),
                    }
                )
            )
            status, applied = self.apply(dry)
            self.assertEqual(status, 409, applied)
            self.assertEqual(applied["error"]["code"], "stale_recovery_digest")
            self.upsert.assert_not_called()

    def test_missing_or_malformed_baseline_does_not_fall_back_to_image_only_adoption(self) -> None:
        for change in ("missing name", "foreign target", "malformed", "missing receipt"):
            with self.subTest(change=change), self.scenario(job_status="error"):
                record = self.store.read_deployment_record(self.record_id)
                if change == "missing name":
                    del self.receipt.originals["first"]
                elif change == "foreign target":
                    self.receipt.target_id = "foreign"
                if change == "malformed":
                    evidence = {next(iter(self.receipt.evidence())): "{"}
                elif change == "missing receipt":
                    lane = self.profile.lanes[0].model_copy(
                        update={"generic_web_extensions": self.receipt.extensions}
                    )
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"lanes": (lane, *self.profile.lanes[1:])})
                    )
                    evidence = {}
                else:
                    evidence = self.receipt.evidence()
                self.store.write_deployment_record(
                    record.model_copy(
                        update={
                            "post_deploy_update": record.post_deploy_update.model_copy(
                                update={"evidence": evidence}
                            ),
                        }
                    )
                )
                status, dry = self.dry()
                self.assertEqual(status, 200, dry)
                self.assertEqual(dry["proposed_action"], "hold_unknown")
                status, _applied = self.apply(dry)
                self.assertEqual(status, 409)
                self.upsert.assert_not_called()
                self.assertTrue(self.store.list_held_provider_target_reservations())

    def test_failed_job_with_unknown_or_active_service_keeps_fence(self) -> None:
        for changes in ({"rollout_state": "updating"}, {"rollout_state": "rollback_started"}):
            with (
                self.subTest(changes=changes),
                self.scenario(job_status="error", runtime_changes=changes),
            ):
                status, dry = self.dry()
                self.assertEqual(status, 200, dry)
                self.assertEqual(dry["proposed_action"], "hold_unknown")
                self.upsert.assert_not_called()

    def test_automatic_observation_cannot_adopt_a_lifecycle_failure(self) -> None:
        with self.scenario(job_status="error"):
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=Path("."),
                record_store=self.store,
                deploy_request=GenericWebDeployEnvelope.model_validate(self.original),
                profile=self.profile,
                lane=self.profile.lanes[0],
                trace_id="observe",
                deploy_provider=cast(GenericWebDeployProvider, self.provider),
            )
            observation = adapter.observe(
                self.provider.original_operation_key,
                self.reservation.provider_effect_phase,
                self.reservation.reconciliation_key,
            )
            self.assertEqual(observation.outcome, "unknown")
            self.assertEqual(
                self.store.read_deployment_record(self.record_id).deploy.status, "pending"
            )
            self.upsert.assert_not_called()

    def test_failed_record_write_resumes_hold_without_reconstructing_baseline(self) -> None:
        with self.scenario():
            _, dry = self.dry()
            write = self.store.write_deployment_record

            def fail_record(record: Any) -> object:
                if record.deploy.status == "fail":
                    raise click.ClickException("failure publication interrupted")
                return write(record)

            with patch.object(self.store, "write_deployment_record", side_effect=fail_record):
                status, _applied = self.apply(dry)
            self.assertEqual(status, 409)
            self.assertTrue(self.store.list_held_provider_target_reservations())
            current = self.store.read_deployment_record(self.record_id)
            self.assertEqual(current.deploy.status, "pending")
            receipt = recorded_application_lifecycle(current)
            assert receipt is not None
            self.assertEqual(receipt.state, "held")
            self.assertEqual(receipt.originals, self.receipt.originals)
            _, fresh = self.dry()
            status, applied = self.apply(fresh)
            self.assertEqual(status, 202, applied)
            self.assertEqual(
                self.store.read_deployment_record(self.record_id).deploy.status, "fail"
            )
            self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_lease_loss_during_hold_prevents_remaining_writes_and_completion(self) -> None:
        with self.scenario(job_status="error"):
            _, dry = self.dry()
            renew = self.store.renew_mutation_reservation

            def lose_lease(**kwargs: Any) -> Any:
                if self.upsert.call_count:
                    raise RuntimeError("lease lost")
                return renew(**kwargs)

            with patch.object(self.store, "renew_mutation_reservation", side_effect=lose_lease):
                status, _applied = self.apply(dry)
            self.assertEqual(status, 409)
            self.assertFalse(self.schedules["first"]["enabled"])
            self.assertTrue(self.schedules["second"]["enabled"])
            self.assertTrue(self.store.list_held_provider_target_reservations())
            self.assertEqual(
                self.store.read_deployment_record(self.record_id).deploy.status, "pending"
            )

    def test_changed_target_authority_refuses_hold_under_recovery_lease(self) -> None:
        with self.scenario(job_status="error"):
            _, dry = self.dry()
            binding = self.store.read_provider_target_record(
                context_name=self.target.ship_request.context,
                instance_name=self.target.ship_request.instance,
            )
            self.store.write_provider_target_record(
                binding.model_copy(update={"target_id": "foreign"})
            )
            status, _applied = self.apply(dry)
            self.assertEqual(status, 409)
            self.upsert.assert_not_called()
            self.assertTrue(self.store.list_held_provider_target_reservations())

    def test_changed_runtime_invalidates_review_before_hold(self) -> None:
        with self.scenario(job_status="error"):
            _, dry = self.dry()
            observed = self.provider.application_runtime
            assert observed is not None
            with patch.object(
                self.provider,
                "observe_testing_application_runtime",
                return_value=replace(observed, rollout_state="updating"),
            ):
                status, applied = self.apply(dry)
            self.assertEqual(status, 409, applied)
            self.assertEqual(applied["error"]["code"], "stale_recovery_digest")
            self.upsert.assert_not_called()
            self.assertTrue(self.store.list_held_provider_target_reservations())

    def test_runtime_recovery_does_not_erase_original_failed_lifecycle_intent(self) -> None:
        with self.scenario(receipt_state="held", phase="application_schedule_hold"):
            record = self.store.read_deployment_record(self.record_id)
            self.store.write_deployment_record(
                record.model_copy(
                    update={
                        "deploy": record.deploy.model_copy(update={"status": "fail"}),
                    }
                )
            )
            with patch.object(
                self.provider,
                "observe_testing_application_runtime",
                side_effect=self.base_runtime_read,
            ):
                status, dry = self.dry()
                self.assertEqual(status, 200, dry)
                self.assertEqual(dry["proposed_action"], "settle_testing_failure")
                status, applied = self.apply(dry)
            self.assertEqual(status, 202, applied)
            self.assertEqual(
                self.store.read_deployment_record(self.record_id).deploy.status, "fail"
            )
            self.assertFalse(self.store.list_environment_inventory())
            self.assertFalse(any(item["enabled"] for item in self.schedules.values()))

    def test_quiescent_checkpoints_recover_without_inventing_lifecycle_qualification(
        self,
    ) -> None:
        for phase, job_status, healthy in product(
            (
                "records_commit",
                "application_maintenance_prepare",
                "application_schedule_prepare",
                "application_schedule_remove",
            ),
            ("done", "error", "failed"),
            (False, True),
        ):
            with (
                self.subTest(phase=phase, job_status=job_status, healthy=healthy),
                self.scenario(phase=phase, job_status=job_status),
            ):
                with (
                    patch.object(
                        self.provider,
                        "observe_testing_application_runtime",
                        side_effect=self.base_runtime_read,
                    )
                    if healthy
                    else nullcontext()
                ):
                    status, dry = self.dry()
                    self.assertEqual(status, 200, dry)
                    adopted = job_status == "done" and healthy
                    self.assertEqual(
                        dry["proposed_action"],
                        "adopt_observed" if adopted else "settle_testing_failure",
                    )
                    self.assertEqual(dry["provider_outcome"], "present")
                    self.assertFalse(dry["retry_safe"])
                    status, applied = self.apply(dry)
                self.assertEqual(status, 202, applied)
                self.assertFalse(self.store.list_held_provider_target_reservations())
                record = self.store.read_deployment_record(self.record_id)
                self.assertEqual(record.deploy.status, "pass" if adopted else "fail")
                self.assertEqual(record.post_deploy_update.status, "fail")
                receipt = recorded_application_lifecycle(record)
                assert receipt is not None
                self.assertEqual(receipt.state, "prepared" if adopted else "held")
                self.assertEqual(receipt.originals, self.receipt.originals)
                if adopted:
                    self.upsert.assert_not_called()
                    self.assertTrue(all(item["enabled"] for item in self.schedules.values()))
                    inventory = self.store.read_environment_inventory(
                        context_name=self.target.ship_request.context,
                        instance_name=self.target.ship_request.instance,
                    )
                    self.assertEqual(inventory.deployment_record_id, record.record_id)
                    self.assertIsNotNone(record.runtime_identity)
                else:
                    self.assertFalse(self.store.list_environment_inventory())
                    self.assertIsNone(record.runtime_identity)
                    self.assertFalse(any(item["enabled"] for item in self.schedules.values()))
                successor = application_snapshot_for_deploy(
                    store=self.store,
                    product=self.profile.product,
                    context=receipt.context,
                    instance=receipt.instance,
                    provider_id=receipt.provider_id,
                    target_id=receipt.target_id,
                    artifact_id=receipt.artifact_id,
                    source_git_ref=receipt.source_git_ref,
                    deployment_record_id="next-deliberate-deployment",
                    extensions=receipt.extensions,
                    rollback=False,
                )
                assert successor is not None
                self.assertTrue(successor.resume_from_hold)
                self.assertEqual(successor.originals, self.receipt.originals)
                self.assertEqual(successor.state, "prepared")

    def test_written_final_record_does_not_fence_a_settled_runtime_failure(self) -> None:
        for state in ("prepared", "qualified"):
            for inventory_written in (False, True):
                with (
                    self.subTest(state=state, inventory_written=inventory_written),
                    self.scenario(phase="records_commit", receipt_state=state),
                ):
                    pending = self.store.read_deployment_record(self.record_id)
                    completed = pending.model_copy(
                        update={
                            "deploy": pending.deploy.model_copy(update={"status": "pass"}),
                            "post_deploy_update": pending.post_deploy_update.model_copy(
                                update={"attempted": True, "status": "pass"}
                            ),
                        }
                    )
                    self.store.write_deployment_record(completed)
                    if inventory_written:
                        self.store.write_environment_inventory(
                            build_environment_inventory(
                                deployment_record=completed, updated_at=completed.deploy.started_at
                            )
                        )
                    status, dry = self.dry()
                    self.assertEqual(status, 200, dry)
                    self.assertEqual(dry["proposed_action"], "settle_testing_failure")
                    self.assertEqual(dry["provider_outcome"], "present")
                    self.assertFalse(dry["retry_safe"])
                    status, applied = self.apply(dry)
                    self.assertEqual(status, 202, applied)
                    stored = self.store.read_deployment_record(self.record_id)
                    self.assertEqual(stored.deploy.status, "fail")
                    self.assertEqual(stored.post_deploy_update.status, "fail")
                    self.assertIsNone(stored.runtime_identity)
                    receipt = recorded_application_lifecycle(stored)
                    assert receipt is not None
                    self.assertEqual(receipt.state, "held")
                    self.assertEqual(receipt.originals, self.receipt.originals)
                    self.assertFalse(any(item["enabled"] for item in self.schedules.values()))
                    self.assertFalse(self.store.list_held_provider_target_reservations())
                    self.assertEqual(
                        bool(self.store.list_environment_inventory()), inventory_written
                    )
                    completed_operation = self.store.read_idempotency_record(
                        scope=self.reservation.scope,
                        route_path=self.reservation.route_path,
                        idempotency_key=self.reservation.idempotency_key,
                    )
                    assert completed_operation is not None
                    self.assertEqual(completed_operation.response_status_code, 502)
                    self.assertEqual(
                        completed_operation.response_payload["result"]["deploy_status"], "fail"
                    )
                    successor = application_snapshot_for_deploy(
                        store=self.store,
                        product=self.profile.product,
                        context=receipt.context,
                        instance=receipt.instance,
                        provider_id=receipt.provider_id,
                        target_id=receipt.target_id,
                        artifact_id=receipt.artifact_id,
                        source_git_ref=receipt.source_git_ref,
                        deployment_record_id="next-deliberate-deployment",
                        extensions=receipt.extensions,
                        rollback=False,
                    )
                    assert successor is not None
                    self.assertTrue(successor.resume_from_hold)
                    self.assertEqual(successor.originals, self.receipt.originals)

    def test_saved_pass_and_exact_healthy_runtime_survive_auxiliary_job_failure(self) -> None:
        for lifecycle in (False, True):
            for job_status in ("error", "failed"):
                with (
                    self.subTest(lifecycle=lifecycle, job_status=job_status),
                    self.scenario(
                        phase="records_commit", receipt_state="qualified", job_status=job_status
                    ),
                ):
                    pending = self.store.read_deployment_record(self.record_id)
                    self.store.write_deployment_record(
                        pending.model_copy(
                            update={
                                "deploy": pending.deploy.model_copy(update={"status": "pass"}),
                                "post_deploy_update": pending.post_deploy_update.model_copy(
                                    update={
                                        "attempted": lifecycle,
                                        "status": "pass" if lifecycle else "skipped",
                                        "evidence": self.receipt.evidence() if lifecycle else {},
                                    }
                                ),
                            }
                        )
                    )
                    with patch.object(
                        self.provider,
                        "observe_testing_application_runtime",
                        side_effect=self.base_runtime_read,
                    ):
                        status, dry = self.dry()
                        self.assertEqual(status, 200, dry)
                        self.assertEqual(dry["proposed_action"], "adopt_observed")
                        status, applied = self.apply(dry)
                    self.assertEqual(status, 202, applied)
                    stored = self.store.read_deployment_record(self.record_id)
                    self.assertEqual(stored.deploy.status, "pass")
                    self.assertEqual(
                        stored.post_deploy_update.status, "pass" if lifecycle else "skipped"
                    )
                    self.assertIsNotNone(stored.runtime_identity)
                    self.upsert.assert_not_called()
                    self.assertTrue(all(item["enabled"] for item in self.schedules.values()))
                    self.assertFalse(self.store.list_held_provider_target_reservations())
                    receipt = recorded_application_lifecycle(stored)
                    if lifecycle:
                        assert receipt is not None
                        self.assertEqual(receipt.state, "qualified")
                    else:
                        self.assertIsNone(receipt)

    def test_terminal_failed_job_with_old_service_repeats_only_captured_hold(self) -> None:
        for status, state, failed_record in (
            ("error", "completed", False),
            ("failed", "", True),
            ("error", "rollback_completed", True),
            ("error", "paused", False),
            ("failed", "rollback_paused", True),
        ):
            with (
                self.subTest(status=status, state=state, failed_record=failed_record),
                self.scenario(
                    job_status=status,
                    runtime_changes={
                        "service_image": "ghcr.io/example/application:old",
                        "service_deployment_record_id": "previous-deployment",
                        "rollout_state": state,
                        "rollout_started_at": "2026-01-01T00:00:00Z",
                    },
                ),
            ):
                if failed_record:
                    record = self.store.read_deployment_record(self.record_id)
                    self.store.write_deployment_record(
                        record.model_copy(
                            update={
                                "deploy": record.deploy.model_copy(update={"status": "fail"}),
                            }
                        )
                    )
                status_code, dry = self.dry()
                self.assertEqual(status_code, 200, dry)
                self.assertEqual(dry["proposed_action"], "settle_testing_failure")
                self.assertEqual(dry["provider_outcome"], "present")
                self.assertFalse(dry["retry_safe"])
                status_code, applied = self.apply(dry)
                self.assertEqual(status_code, 202, applied)
                self.assertEqual(
                    self.store.read_deployment_record(self.record_id).deploy.status, "fail"
                )
                self.assertFalse(self.store.list_environment_inventory())
                self.assertFalse(self.store.list_held_provider_target_reservations())
                self.assertFalse(any(item["enabled"] for item in self.schedules.values()))

    def test_no_input_lifecycle_failure_write_keeps_original_recovery_phase(self) -> None:
        capture = DokployApplicationLifecycle.capture
        prepare = DokployApplicationLifecycle.prepare
        with self.scenario():
            lane = self.profile.lanes[0].model_copy(
                update={"generic_web_extensions": self.receipt.extensions}
            )
            self.profile = self.profile.model_copy(update={"lanes": (lane,)})
            self.store.write_product_profile_record(self.profile)
            provider = _LifecycleDeployProvider(
                target_id=self.target.resolved_target.target_id,
                target_name=self.target.resolved_target.target_name,
                original_operation_key=self.provider.original_operation_key,
                observation=self.provider.observation,
            )
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=Path("."),
                record_store=self.store,
                deploy_request=GenericWebDeployEnvelope.model_validate(self.original),
                profile=self.profile,
                lane=lane,
                trace_id="original-failure-publication",
                deploy_provider=provider,
                recorded_artifact=True,
            )
            inspected = adapter.inspect_evidence(
                provider_operation_key=self.provider.original_operation_key,
                provider_effect_phase=self.reservation.provider_effect_phase,
                reconciliation_key=self.reservation.reconciliation_key,
                expected_provider_target_key=self.reservation.provider_target_key,
            )
            self.assertIsNotNone(inspected.resolved_deploy_target)
            assert inspected.resolved_deploy_target is not None
            self.assertFalse(inspected.resolved_deploy_target.testing_runtime_digest)
            retry = self.store.retry_reconciled_mutation(
                reservation=self.reservation,
                lease_owner="original-failure-publication",
                preserve_effect_checkpoint=True,
            )
            self.assertEqual(retry.status, "acquired")
            assert retry.record is not None
            execute = provider.execute_artifact_deploy
            write = self.store.write_deployment_record

            def pending_runtime(**kwargs: object) -> None:
                execute(**kwargs)  # type: ignore[arg-type]
                assert provider.application_runtime is not None
                provider.application_runtime = replace(
                    provider.application_runtime,
                    running_container_images=(),
                    running_container_deployment_record_ids=(),
                    running_container_health=(),
                    pending_task_count=1,
                )

            def fail_publication(record: Any) -> object:
                if record.deploy.status == "fail":
                    raise click.ClickException("failed final record write interrupted")
                return write(record)

            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
                    side_effect=execute_generic_web_deploy_result,
                ),
                patch.object(provider, "execute_artifact_deploy", side_effect=pending_runtime),
                patch.object(self.store, "write_deployment_record", side_effect=fail_publication),
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("fixture", "fixture"),
                ),
                patch(
                    "control_plane.workflows.generic_web_deploy_provider.monotonic",
                    return_value=1e12,
                ),
                patch.object(DokployApplicationLifecycle, "capture", new=capture),
                patch.object(DokployApplicationLifecycle, "prepare", new=prepare),
            ):
                result = resume_acquired_provider_operation(
                    store=self.store,
                    reservation=retry.record,
                    response_trace_id="original-failure-publication",
                    adapter=adapter,
                )
            self.assertEqual(result.status, "reconcile_required")
            held = self.store.list_held_provider_target_reservations()
            self.assertTrue(held)
            stored = self.store.read_deployment_record(self.record_id)
            self.assertEqual(stored.deploy.status, "pending")
            baseline = recorded_application_lifecycle(stored)
            assert baseline is not None
            self.assertEqual(baseline.originals, self.receipt.originals)
            status, dry = self.dry()
            self.assertEqual(status, 200, dry)
            self.assertEqual(dry["proposed_action"], "settle_testing_failure")
            status, applied = self.apply(dry)
            self.assertEqual(status, 202, applied)
            self.assertFalse(self.store.list_held_provider_target_reservations())
            self.assertFalse(self.store.list_environment_inventory())
