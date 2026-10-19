"""Scheduled restores use the native worker and keep each predecessor in custody."""

from datetime import UTC, datetime
import unittest
from typing import Any
from unittest.mock import Mock, patch

from control_plane import testing_candidate_runtime as compiler
from control_plane import odoo_testing_native_source
from control_plane.contracts.odoo_testing_drill import (
    OdooTestingDrillStorageBudget,
    OdooTestingDrillEvidence,
)
from control_plane.contracts.odoo_testing_source import (
    NativeOdooSourceAuthority,
    NativeOdooTestingSource,
)
from control_plane.contracts.product_profile_record import (
    ProductOdooLaneDataPolicy,
    LaunchplaneProductProfileRecord,
)
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.testing_candidate_allocation import observe_candidate_storage
from control_plane.odoo_testing_drill import (
    DRILL_INTERVAL_SECONDS,
    OdooTestingDrillScheduler,
    testing_drill_evidence,
    DRILL_DEADLINE_STEP,
    DRILL_TIMEOUT_SECONDS,
)
from control_plane import testing_rebuild_service as service
from control_plane.odoo_testing_restore import generation_binding
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests import test_testing_rebuild_worker as workers


class ScheduledNativeRebuildTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        prod = next(lane for lane in profile.lanes if lane.instance == "prod")
        descriptor = NativeOdooTestingSource(
            product=profile.product,
            context=prod.context,
            target_id="production-compose",
            database_name="production_db",
            filestore_path="/production/filestore",
            settings_sha256="b" * 64,
            server_id="production-server",
            compose_project="production-project",
            database_container="c" * 64,
            web_container="d" * 64,
            database_image="sha256:" + "e" * 64,
            web_image="sha256:" + "f" * 64,
        )
        authority = NativeOdooSourceAuthority.model_validate(
            descriptor.model_dump(include=set(NativeOdooSourceAuthority.model_fields))
        )
        self.enterContext(
            patch.object(compiler, "native_testing_source_authority", return_value=authority)
        )
        self.enterContext(
            patch.object(compiler, "resolve_native_testing_source", return_value=descriptor)
        )
        self.enterContext(
            patch.object(
                odoo_testing_native_source,
                "native_testing_source_authority",
                return_value=authority,
            )
        )
        self.signins = self.enterContext(
            patch("control_plane.odoo_testing_drill.read_drill_recent_signins", return_value=False)
        )
        self.worker = workers.NativeTestingWorkerTests()
        self.worker.database_url = self.database_url
        self.worker.source_mode = "production"
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        self.store = self.worker.store
        self.assertEqual(self.worker.execute().deploy_status, "pass")
        profile = self.store.read_product_profile_record(profile.product)
        self.context = self.worker.operation.context
        self.budget = OdooTestingDrillStorageBudget(
            database_bytes=100, filestore_bytes=100, staging_bytes=100, reserve_bytes=100
        )
        profile = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_testing_drill_enabled": True,
                            "odoo_testing_drill_storage": self.budget,
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable",
                                allowed_rebuild_sources=("upstream_restore",),
                                upstream_source=f"{profile.product}:{prod.context}:prod",
                            ),
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(profile)
        self.profile = profile
        self.worker.fixture.fixture.fixture.before = self.worker.fixture.fixture.fixture.authority()

    def evidence(self) -> OdooTestingDrillEvidence:
        result = testing_drill_evidence(self.store, self.profile.product, self.context, "testing")
        assert result is not None
        return result

    def test_two_scheduled_native_restores_preserve_address_original_witnesses_and_production(
        self,
    ) -> None:
        def production_authority() -> tuple[tuple[object, ...], ...]:
            return tuple(
                (
                    self.store.read_dokploy_target_record(
                        context_name=lane.context, instance_name="prod"
                    ),
                    self.store.read_dokploy_target_id_record(
                        context_name=lane.context, instance_name="prod"
                    ),
                    self.store.read_provider_target_record(
                        context_name=lane.context, instance_name="prod"
                    ),
                    self.store.list_runtime_environment_records(
                        context_name=lane.context, instance_name="prod"
                    ),
                    tuple(
                        item
                        for item in self.store.list_environment_inventory()
                        if (item.context, item.instance) == (lane.context, "prod")
                    ),
                )
                for lane in self.profile.lanes
                if lane.instance == "prod"
            )

        production = production_authority()
        first_tick = datetime.now(UTC).timestamp()
        self.worker.complete_second_rebuild(scheduled=True, scheduled_at=first_tick)
        first = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.drill_storage_budget, self.budget)
        OdooTestingDrillScheduler(self.store, clock=lambda: first_tick).run_once()
        self.assertEqual(self.evidence().status, "pass")
        self.assertEqual(self.evidence().storage_status, "pass")
        self.assertEqual(self.evidence().disposal_attention, 1)
        self.worker.complete_second_rebuild(
            scheduled=True, scheduled_at=first_tick + DRILL_INTERVAL_SECONDS
        )
        second = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert second is not None
        self.assertNotEqual(generation_binding(first), generation_binding(second))
        OdooTestingDrillScheduler(
            self.store, clock=lambda: first_tick + DRILL_INTERVAL_SECONDS
        ).run_once()
        evidence = self.evidence()
        self.assertEqual(evidence.status, "pass")
        self.assertEqual(evidence.disposal_attention, 2)
        self.assertIn(generation_binding(first), evidence.retained_copy_bindings)
        self.assertEqual(production_authority(), production)
        OdooTestingDrillScheduler(
            self.store, clock=lambda: first_tick + 2 * DRILL_INTERVAL_SECONDS
        ).run_once()
        self.assertEqual(self.evidence().status, "queued")
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"] * 3)

    def test_late_observation_uses_original_publication_after_another_native_copy_serves(
        self,
    ) -> None:
        self.worker.complete_second_rebuild(scheduled=True)
        scheduled = self.worker.operation
        original = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert original is not None
        self.worker.complete_second_rebuild(operation_id="later-manual-copy")
        current = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert current is not None
        self.assertNotEqual(original.operation_id, current.operation_id)
        scheduler = OdooTestingDrillScheduler(self.store)
        slot = scheduler._read(scheduled.idempotency_key)
        assert slot is not None
        scheduler._observe(self.profile.product, self.context, slot, datetime.now(UTC).timestamp())
        self.assertEqual(self.evidence().status, "pass")
        self.assertIn(generation_binding(original), self.evidence().retained_copy_bindings)

    def test_native_capacity_refusal_precedes_preparation_and_uses_no_legacy_receiver(self) -> None:
        self.worker.capacity = False
        before = self.worker.current()
        with (
            patch.object(service, "prepare_candidate_storage") as prepare,
            patch("control_plane.odoo_testing_storage.execute_maintenance") as legacy,
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.complete_second_rebuild(scheduled=True)
        prepare.assert_not_called()
        legacy.assert_not_called()
        self.assertEqual(self.worker.current(), before)
        self.assertEqual(self.evidence().storage_status, "insufficient")
        self.assertEqual(self.worker.fixture.fixture.effects, [])

    def test_interrupted_original_storage_is_read_before_capacity_without_reallocation(
        self,
    ) -> None:
        stages = []
        observe = observe_candidate_storage
        physical = self.worker.physical

        def readback(**kwargs: Any) -> TestingCandidateAllocation:
            stages.append("original-storage-readback")
            return observe(**kwargs)

        def capacity(**kwargs: Any) -> dict[str, Any]:
            if kwargs["payload"]["mode"] == "capacity":
                stages.append("capacity")
                self.assertTrue(kwargs["payload"]["candidate_allocated"])
            return physical(**kwargs)

        with (
            patch.object(service, "observe_candidate_storage", side_effect=readback),
            patch.object(service, "retained_copy_operation", side_effect=capacity),
        ):
            self.worker.complete_second_rebuild(scheduled=True, interrupted_storage=True)
        self.assertEqual(stages, ["original-storage-readback", "capacity"])

    def test_deadline_alert_and_original_key_recovery_do_not_repeat_old_stop(self) -> None:
        self.worker.fail_after_stop = True
        with self.assertRaises(TimeoutError):
            self.worker.complete_second_rebuild(scheduled=True)
        operation = self.worker.operation
        notifications = Mock(return_value=True)
        expired = (
            datetime.fromisoformat(operation.created_at).timestamp() + DRILL_TIMEOUT_SECONDS + 1
        )
        scheduler = OdooTestingDrillScheduler(
            self.store, clock=lambda: expired, notify=notifications
        )
        scheduler.run_once()
        self.assertEqual(self.evidence().step, DRILL_DEADLINE_STEP)
        self.assertEqual(self.evidence().disposal_attention, 1)
        notifications.assert_called_once()
        effects = list(self.worker.effects)
        result = self.worker.execute()
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(self.worker.effects, effects)
        from control_plane.workflows.odoo_stable_operation_worker import (
            _target_replacement_terminal_operation,
        )

        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=_target_replacement_terminal_operation(
                    operation=operation, result=result, lease_owner=operation.lease_owner
                ),
                lease_owner=operation.lease_owner,
            )
        )
        scheduler.run_once()
        self.assertEqual(self.evidence().status, "pass")
        self.assertEqual(self.evidence().step, "late_completion")
        self.assertEqual(self.evidence().operation_id, operation.operation_id)

    def test_failed_candidate_custody_is_visible_without_calling_the_serving_predecessor_retained(
        self,
    ) -> None:
        before = self.worker.current()
        self.worker.fail_public = True
        self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        self.assertEqual(self.worker.current().target_id, before.target_id)
        evidence = self.evidence()
        self.assertEqual(evidence.disposal_attention, 1)
        self.assertEqual(
            evidence.retained_candidate_namespaces, (self.worker.fixture.candidate.namespace,)
        )
        self.assertEqual(evidence.retained_copy_bindings, ())

    def test_new_login_after_route_preparing_is_checked_before_any_forward_write(self) -> None:
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
        from control_plane.testing_route_effects import ROUTE_PREPARING

        fence = self.worker.route_fence_ensure.return_value
        observed = []

        def after_preparing(_custody: Any) -> Any:
            operation = self.worker.operation
            namespace = self.worker.fixture.candidate.namespace
            prior = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=operation.operation_id + ":route:" + namespace,
            )
            assert prior is not None
            self.assertEqual(prior.provider_effect_phase, ROUTE_PREPARING)
            observed.append(namespace)
            self.signins.return_value = True
            return fence

        self.worker.route_fence_ensure.side_effect = after_preparing
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.worker.complete_second_rebuild(scheduled=True)
        self.assertEqual(observed, [self.worker.fixture.candidate.namespace])
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])
        self.assertIsNotNone(self.signins.call_args.kwargs["since"])
