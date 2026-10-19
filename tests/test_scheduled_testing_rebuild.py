"""Scheduled restores use the native worker and keep each predecessor in custody."""

from datetime import UTC, datetime
import unittest
import json
import time
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import Mock, patch

from sqlalchemy import event

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
from control_plane.testing_candidate_allocation import (
    observe_candidate_storage,
    prepare_candidate_storage,
)
from control_plane.odoo_testing_drill import (
    DRILL_INTERVAL_SECONDS,
    OdooTestingDrillScheduler,
    testing_drill_evidence,
    DRILL_DEADLINE_STEP,
    DRILL_TIMEOUT_SECONDS,
)
from control_plane import testing_rebuild_service as service
from control_plane.odoo_testing_restore import generation_binding
from control_plane._odoo_testing_storage_program import MAX_DISPOSALS_PER_PASS
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
        self.enterContext(
            patch(
                "control_plane.odoo_testing_native_maintenance.native_maintenance_operation",
                side_effect=self.native_maintenance,
            )
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

    def native_maintenance(self, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs["payload"]
        clock = {
            "host_boot_id": "fixture-boot",
            "host_monotonic": time.monotonic(),
            "host_observed_at": time.time(),
            "namespace_sha256": "9" * 64,
        }
        if payload["mode"] == "inspect-original":
            return {"type": "complete", "status": "missing", **clock}
        if payload["mode"] == "qualify-retirement":
            # A bounded remote send never opens another CP write transaction.
            # Keep the fixture's publication assertion outside that send.
            snapshot = self.worker.current()

            def stop() -> None:
                with patch.object(self.worker, "current", return_value=snapshot):
                    self.worker.provider_request(
                        path="/api/compose.stop",
                        payload={"composeId": payload["identity"]["target_id"]},
                    )

            kwargs["dispatch_guard"](stop)
            return {"type": "complete", "status": "eligible", **clock}
        raise AssertionError("This fixture must explicitly model old-copy physical collection")

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

    def test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline(
        self,
        *,
        manual_between: bool = False,
        cp_clock_jump: bool = False,
        fail_next_admission: bool = False,
        recent_old: bool = False,
    ) -> None:
        from control_plane import odoo_testing_native_maintenance as maintenance
        from control_plane.dokploy import api
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
        from control_plane.testing_serving_copy import published_testing_copy

        manual = self.worker.fixture.candidate.namespace
        physical = {manual}
        archived: dict[str, dict[str, Any]] = {}
        removals = []
        qualified: set[str] = set()
        host_monotonic = 10.0
        disposal_sends = 0
        clock_exercised = False
        recent_namespace = ""
        from control_plane.provider_operations import run_durable_provider_operation

        run_provider = run_durable_provider_operation

        def durable(**kwargs: Any) -> Any:
            nonlocal host_monotonic, clock_exercised
            from control_plane.contracts.odoo_testing_drill import NATIVE_DRILL_MAINTENANCE_ROUTE
            from control_plane.odoo_testing_drill import DRILL_SCOPE

            if (
                not cp_clock_jump
                or clock_exercised
                or kwargs["route_path"] != NATIVE_DRILL_MAINTENANCE_ROUTE
            ):
                return run_provider(**kwargs)
            clock_exercised = True
            try:
                return run_provider(**kwargs)
            except TimeoutError:
                original = self.store.read_idempotency_record(
                    scope=DRILL_SCOPE,
                    route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
                    idempotency_key=kwargs["idempotency_key"],
                )
                assert original is not None
                admission = self.store.read_idempotency_record(
                    scope=DRILL_SCOPE,
                    route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                    idempotency_key=kwargs["idempotency_key"] + ":" + str(original.attempt),
                )
                assert admission is not None
                jumped = float(admission.response_payload["expires_at"]) + 1

                class ForwardClock(datetime):
                    @classmethod
                    def now(cls, tz: Any = None) -> Self:
                        return cls.fromtimestamp(jumped, tz)

                with patch.object(maintenance, "datetime", ForwardClock):
                    held = run_provider(**kwargs)
                self.assertEqual(held.status, "reconcile_required")
                self.assertEqual(disposal_sends, 1)
                host_monotonic = float(admission.response_payload["host_expires_at"]) + 1
                if fail_next_admission:
                    from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

                    def fail_registration(*args: Any, **options: Any) -> Any:
                        self.assertEqual(args[1].attempt, 2)
                        raise NativeSourceUnavailable("fixture DB failed before admission 2")

                    with (
                        patch.object(
                            maintenance, "register_native_admission", side_effect=fail_registration
                        ),
                        self.assertRaises(NativeSourceUnavailable),
                    ):
                        run_provider(**kwargs)
                    current = self.store.read_idempotency_record(
                        scope=DRILL_SCOPE,
                        route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
                        idempotency_key=kwargs["idempotency_key"],
                    )
                    assert current is not None
                    self.assertEqual(current.attempt, 2)
                    self.assertIsNone(
                        self.store.read_idempotency_record(
                            scope=DRILL_SCOPE,
                            route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                            idempotency_key=current.idempotency_key + ":2",
                        )
                    )
                    self.assertEqual(disposal_sends, 1)
                return run_provider(**kwargs)

        fetch = cast(Mock, api.fetch_dokploy_target_payload).side_effect
        read = self.worker.route_read

        def provider(**kwargs: Any) -> Any:
            return (
                dict(archived[kwargs["target_id"]])
                if kwargs["target_id"] in archived
                else fetch(**kwargs)
            )

        def route() -> dict[str, Any]:
            return {**read(), "execution_fence": {"status": "ready"}}

        def host(**kwargs: Any) -> dict[str, Any]:
            nonlocal disposal_sends, recent_namespace
            payload = kwargs["payload"]
            identity = payload["identity"]
            namespace = identity["namespace"]
            if payload["mode"] == "qualify-retirement":
                archived[identity["target_id"]] = dict(self.worker.old_provider)
                qualified.add(namespace)
                if not recent_namespace:
                    recent_namespace = namespace
                return self.native_maintenance(**kwargs)
            if payload["mode"] == "inspect-original":
                return {
                    "type": "complete",
                    "status": (
                        "recent_use"
                        if recent_old and namespace == recent_namespace
                        else "eligible"
                        if namespace in qualified
                        else "missing"
                    )
                    if namespace in physical
                    else "disposed",
                    "identity_sha256": payload["identity_sha256"],
                    "host_boot_id": "fixture-boot",
                    "host_monotonic": host_monotonic,
                    "host_observed_at": time.time(),
                    "namespace_sha256": "9" * 64,
                }

            disposal_sends += 1
            if cp_clock_jump and disposal_sends == 1:
                kwargs["dispatch_guard"](lambda: None)
                raise TimeoutError("fixture payload reply lost before any host disposal")

            current = published_testing_copy(
                self.store, product=self.profile.product, context=self.context
            )
            assert current is not None

            def remove() -> None:
                self.assertNotIn(
                    identity["target_id"],
                    {
                        current.plan.after.target_id.target_id,
                        current.plan.before.target_id.target_id,
                    },
                )
                self.assertIn(namespace, physical)
                physical.remove(namespace)
                removals.append(namespace)

            kwargs["dispatch_guard"](remove)
            return {
                "type": "complete",
                "status": "disposed",
                "identity_sha256": payload["identity_sha256"],
            }

        tick = datetime.now(UTC).timestamp()
        with (
            patch.object(maintenance, "native_maintenance_operation", side_effect=host),
            patch.object(maintenance, "run_durable_provider_operation", side_effect=durable),
            patch.object(api, "fetch_dokploy_target_payload", side_effect=provider),
            patch.object(DokployTestingRouteTransport, "read", side_effect=route),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                return_value=[],
            ),
        ):
            for number in range(5):
                self.worker.complete_second_rebuild(
                    scheduled=True, scheduled_at=tick + number * DRILL_INTERVAL_SECONDS
                )
                physical.add(self.worker.fixture.candidate.namespace)
                self.assertIn(manual, physical)
                # Collection precedes allocation, preserving the current and
                # immediate predecessor at that boundary plus one new copy.
                self.assertLessEqual(len(physical), (5 if manual_between else 4) + int(recent_old))
                OdooTestingDrillScheduler(
                    self.store, clock=lambda: tick + number * DRILL_INTERVAL_SECONDS
                ).run_once()
                self.assertLessEqual(
                    self.evidence().disposal_attention,
                    (4 if manual_between else 3) + int(recent_old),
                )
                if manual_between and number == 0:
                    original = self.store.read_testing_candidate(
                        self.worker.fixture.candidate.namespace
                    )
                    assert original is not None
                    self.worker.complete_second_rebuild(operation_id="manual-native-successor")
                    from control_plane.workflows.odoo_stable_operation_worker import (
                        _target_replacement_terminal_operation,
                    )

                    self.store.write_odoo_stable_target_replacement_operation_record(
                        _target_replacement_terminal_operation(
                            operation=self.worker.operation,
                            result=self.worker.execute(),
                            lease_owner=self.worker.operation.lease_owner,
                        )
                    )
                    physical.add(self.worker.fixture.candidate.namespace)
                    qualification = maintenance._qualification(self.store, original.namespace)
                    assert qualification is not None
                    maintenance._require_completed_copy(self.store, original, qualification)
                    successor = self.store.read_odoo_stable_target_replacement_operation_record(
                        self.worker.operation.operation_id
                    )
                    self.store.write_odoo_stable_target_replacement_operation_record(
                        successor.model_copy(
                            update={
                                "request": successor.request.model_copy(
                                    update={
                                        "strategy": "recreate-in-place",
                                        "data_source_mode": "existing",
                                    }
                                )
                            }
                        )
                    )
                    from control_plane.odoo_testing_drill import DrillPrerequisite

                    with self.assertRaises(DrillPrerequisite):
                        maintenance._require_completed_copy(self.store, original, qualification)
                    self.store.write_odoo_stable_target_replacement_operation_record(successor)
        self.assertEqual(len(removals), 1 if recent_old else 2)
        if recent_old:
            self.assertIn(recent_namespace, physical)
            self.assertNotIn(recent_namespace, removals)
        self.assertNotIn(manual, removals)
        if cp_clock_jump:
            self.assertTrue(clock_exercised)
            self.assertEqual(disposal_sends, len(removals) + 1)

    def test_recent_old_copy_does_not_skip_another_eligible_original(self) -> None:
        self.test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline(
            recent_old=True
        )

    def test_disposal_attempt_without_admission_reuses_the_complete_original_family(self) -> None:
        self.test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline(
            cp_clock_jump=True, fail_next_admission=True
        )

    def test_recent_bounded_pass_does_not_hide_an_unseen_backlog(self) -> None:
        from control_plane import odoo_testing_native_maintenance as maintenance
        from control_plane.odoo_testing_drill import DRILL_SCOPE, DrillSkipped, DrillPrerequisite

        observed = tuple(
            SimpleNamespace(namespace=f"recent-{number}")
            for number in range(MAX_DISPOSALS_PER_PASS)
        )
        unseen = SimpleNamespace(namespace="unseen-original")
        executor = SimpleNamespace(
            store=self.store,
            operation=self.worker.operation.model_copy(update={"idempotency_scope": DRILL_SCOPE}),
        )
        with (
            patch.object(
                maintenance,
                "native_drill_maintenance_candidates",
                side_effect=[observed, (unseen,)],
            ) as candidates,
            patch.object(maintenance, "_qualification", return_value={}),
            patch.object(
                maintenance,
                "_reconcile_copy",
                side_effect=DrillSkipped("native_testing_copy_recent_successful_signin"),
            ) as reconcile,
        ):
            with self.assertRaises(DrillPrerequisite) as refused:
                maintenance.reconcile_native_drill_storage(executor)
        self.assertEqual(refused.exception.code, "native_testing_copy_cleanup_pending")
        self.assertEqual(reconcile.call_count, len(observed))
        self.assertEqual(
            set(candidates.call_args.kwargs["retained_namespaces"]),
            {item.namespace for item in observed},
        )
        self.assertNotIn(unseen.namespace, candidates.call_args.kwargs["retained_namespaces"])

    def test_manual_native_successor_qualifies_original_drill_for_later_collection(self) -> None:
        self.test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline(
            manual_between=True
        )

    def test_disposal_cp_clock_jump_cannot_exclude_live_host_admission(self) -> None:
        self.test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline(
            cp_clock_jump=True
        )

    def test_uncertain_original_retirement_cannot_rollback_or_borrow_retention(self) -> None:
        from control_plane import odoo_testing_native_maintenance as maintenance

        tick = datetime.now(UTC).timestamp()
        self.worker.complete_second_rebuild(scheduled=True, scheduled_at=tick)
        original = self.worker.fixture.candidate
        dispatched = False

        def host(**kwargs: Any) -> dict[str, Any]:
            nonlocal dispatched
            if kwargs["payload"]["mode"] == "qualify-retirement":
                kwargs["dispatch_guard"](lambda: None)
                dispatched = True
            return {
                "type": "complete",
                "host_boot_id": "fixture-boot",
                "host_monotonic": time.monotonic(),
                "host_observed_at": time.time(),
                "namespace_sha256": "9" * 64,
                "status": "recovery_required" if dispatched else "missing",
                "reason": "original_stop_outcome_unknown",
            }

        with patch.object(maintenance, "native_maintenance_operation", side_effect=host):
            with self.assertRaises(service.NativeTestingRebuildPending) as pending:
                self.worker.complete_second_rebuild(
                    scheduled=True, scheduled_at=tick + DRILL_INTERVAL_SECONDS
                )
            self.assertTrue(pending.exception.code.startswith("native_testing_copy_retirement_"))
            self.store.write_odoo_stable_target_replacement_operation_record(
                self.worker.operation.model_copy(
                    update={
                        "status": "pending",
                        "error_code": pending.exception.code,
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                    }
                )
            )
            OdooTestingDrillScheduler(
                self.store, clock=lambda: tick + DRILL_INTERVAL_SECONDS
            ).run_once()
            self.assertEqual(self.evidence().step, pending.exception.code)
            self.store.update_odoo_testing_drill_receipt(
                self.worker.operation.idempotency_key,
                tick + DRILL_INTERVAL_SECONDS,
                {"failure_step": DRILL_DEADLINE_STEP, "alert_delivered": False},
            )
            OdooTestingDrillScheduler(
                self.store, clock=lambda: tick + DRILL_INTERVAL_SECONDS
            ).run_once()
            slot = OdooTestingDrillScheduler(self.store)._read(
                self.worker.operation.idempotency_key
            )
            assert slot is not None
            self.assertEqual(slot.response_payload["failure_step"], DRILL_DEADLINE_STEP)
            current = self.worker.current()
            effects = list(self.worker.effects)
            profile = self.store.read_product_profile_record(self.profile.product)
            self.store.write_product_profile_record(
                profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(
                                update={
                                    "odoo_testing_drill_storage": self.budget.model_copy(
                                        update={
                                            "retained_candidate_namespaces": (original.namespace,),
                                        }
                                    ),
                                }
                            )
                            if lane.instance == "testing"
                            else lane
                            for lane in profile.lanes
                        ),
                    }
                )
            )
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.worker.execute()
            self.worker.fail_public = True
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.worker.execute()
            self.assertEqual(self.worker.effects, effects)
            self.assertEqual(self.worker.current(), current)
            self.assertFalse(self.worker.retained["stopped"])

    def test_original_retirement_resumes_only_after_host_no_dispatch_exclusion(
        self, *, fail_next_admission: bool = False
    ) -> None:
        from control_plane import odoo_testing_native_maintenance as maintenance
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE

        tick = datetime.now(UTC).timestamp()
        self.worker.complete_second_rebuild(scheduled=True, scheduled_at=tick)
        state = "missing"
        host_monotonic = 10.0
        calls = []
        failed_second = False

        def host(**kwargs: Any) -> dict[str, Any]:
            nonlocal state, failed_second
            payload = kwargs["payload"]
            if payload["mode"] == "inspect-original" and fail_next_admission and not failed_second:
                current = self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=REBUILD_ROUTE + ":effect",
                    idempotency_key=self.worker.operation.operation_id + ":retire-old",
                )
                if current is not None and current.attempt == 2:
                    from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

                    failed_second = True
                    raise NativeSourceUnavailable("fixture observation failed before admission 2")
            if payload["mode"] == "qualify-retirement":
                if state == "missing":
                    kwargs["dispatch_guard"](lambda: None)
                    state = "active"
                    calls.append("excluded-preflight")
                else:
                    calls.append("original-stop")
                    return self.native_maintenance(**kwargs)
            return {
                "type": "complete",
                "status": state,
                "host_boot_id": "fixture-boot",
                "host_monotonic": host_monotonic,
                "host_observed_at": time.time(),
                "namespace_sha256": "9" * 64,
            }

        with patch.object(maintenance, "native_maintenance_operation", side_effect=host):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.worker.complete_second_rebuild(
                    scheduled=True, scheduled_at=tick + DRILL_INTERVAL_SECONDS
                )
            key = self.worker.operation.operation_id + ":retire-old"
            original = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
            )
            assert original is not None
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.worker.execute()
            self.assertEqual(calls, ["excluded-preflight"])
            admission = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":retirement-admission",
                idempotency_key=key + ":" + str(original.attempt),
            )
            assert admission is not None
            jumped = float(admission.response_payload["expires_at"]) + 1

            class ForwardClock(datetime):
                @classmethod
                def now(cls, tz: Any = None) -> Self:
                    return cls.fromtimestamp(jumped, tz)

            # No receipt is not no-effect proof while the original host
            # admission still permits a delayed payload to start.
            state = "missing"
            with patch.object(maintenance, "datetime", ForwardClock):
                with self.assertRaises(service.NativeTestingRebuildPending):
                    self.worker.execute()
            self.assertEqual(calls, ["excluded-preflight"])
            host_monotonic = float(admission.response_payload["host_expires_at"]) + 1
            state = "resumable_retirement"
            if fail_next_admission:
                with self.assertRaises(service.NativeTestingRebuildPending):
                    self.worker.execute()
                current = self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
                )
                assert current is not None
                self.assertEqual(current.attempt, 2)
                self.assertIsNone(
                    self.store.read_idempotency_record(
                        scope=REBUILD_SCOPE,
                        route_path=REBUILD_ROUTE + ":retirement-admission",
                        idempotency_key=key + ":2",
                    )
                )
                self.assertEqual(calls, ["excluded-preflight"])
            self.assertEqual(self.worker.execute().deploy_status, "pass")
            continued = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
            )
            assert continued is not None
            self.assertEqual(continued.record_id, original.record_id)
            self.assertEqual(continued.request_fingerprint, original.request_fingerprint)
            self.assertEqual(
                continued.provider_effect_started_at, original.provider_effect_started_at
            )
            self.assertGreater(continued.attempt, original.attempt)
            self.assertEqual(calls, ["excluded-preflight", "original-stop"])

    def test_retirement_attempt_without_admission_recovers_the_original_family(self) -> None:
        self.test_original_retirement_resumes_only_after_host_no_dispatch_exclusion(
            fail_next_admission=True
        )

    def test_real_host_preservation_completes_healthy_publication_after_lost_cp_close(self) -> None:
        self.exercise_host_sealed_retirement(unhealthy=False)

    def test_real_host_no_stop_allows_original_inverse_and_a_new_native_retirer(self) -> None:
        self.exercise_host_sealed_retirement(unhealthy=True)

    def test_native_rollback_exclusion_survives_lost_reply_and_recovered_new_health(self) -> None:
        self.exercise_host_sealed_retirement(unhealthy=True, lost_rollback=True)

    def test_receipt_exposure_remains_untrusted_after_the_mount_is_removed(self) -> None:
        self.exercise_host_sealed_retirement(
            unhealthy=False, exposure=True, late_shared_exposure=True
        )

    def exercise_host_sealed_retirement(
        self,
        *,
        unhealthy: bool,
        exposure: bool = False,
        lost_rollback: bool = False,
        late_shared_exposure: bool = False,
    ) -> None:
        from control_plane import odoo_testing_native_maintenance as maintenance
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
        from control_plane.dokploy import testing_native_maintenance as transport
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore
        from control_plane.workflows.odoo_stable_operation_worker import (
            _target_replacement_terminal_operation,
        )
        from tests.test_testing_native_maintenance import NativeMaintenanceHostTests

        tick = datetime.now(UTC).timestamp()
        self.worker.complete_second_rebuild(scheduled=True, scheduled_at=tick)
        original_copy = self.worker.fixture.candidate
        original_app = self.worker.app
        host = NativeMaintenanceHostTests()
        host.setUp()
        self.addCleanup(host.doCleanups)
        if exposure and not late_shared_exposure:
            host.containers["foreign"] = {
                "Id": "foreign",
                "Mounts": [{"Source": str(host.root), "RW": True}],
            }
        defer = unhealthy
        closed = False
        from control_plane.storage.testing_native_disposal import close_native_admissions

        close = close_native_admissions

        def lost_close(*args: Any, **kwargs: Any) -> Any:
            nonlocal closed
            if not unhealthy and not exposure and not closed:
                closed = True
                raise NativeSourceUnavailable("fixture lost CP close after host seal")
            result = close(*args, **kwargs)
            if lost_rollback and kwargs.get("rollback") and not closed:
                closed = True
                self.worker.fail_public = False
                raise NativeSourceUnavailable("fixture lost original rollback closure reply")
            return result

        def remote(**kwargs: Any) -> None:
            nonlocal defer
            payload = kwargs["payload"]
            if defer and payload["mode"] == "qualify-retirement":
                defer = False
                kwargs["dispatch_guard"](lambda: None)
                raise NativeSourceUnavailable("fixture original payload never reached host")
            if kwargs.get("dispatch_guard") is not None:
                kwargs["dispatch_guard"](lambda: None)
            if late_shared_exposure and payload["mode"] == "qualify-retirement":
                # Match the real emitted shared guard through its final privacy
                # check. The exposure appears after the CP pre-inspection.
                namespace = payload["generation"]["namespace"]
                old_namespace = host.fixture.gen.namespace
                names = {name: name.replace(old_namespace, namespace) for name in host.volumes}
                host.volumes = {
                    names[name]: {
                        **value,
                        "Name": names[name],
                        "Labels": {
                            **value["Labels"],
                            "io.launchplane.testing-resources": payload["generation"][
                                "resources_binding_sha256"
                            ],
                        },
                    }
                    for name, value in host.volumes.items()
                }
                for member in host.containers.values():
                    member["Config"]["Labels"]["com.docker.compose.project"] = payload[
                        "compose_project"
                    ]
                    for mount in member["Mounts"]:
                        mount["Name"] = names[mount["Name"]]
                    if member["Id"] != "database":
                        member["Config"]["Image"] = payload["image_reference"]
                        environment = dict(
                            value.split("=", 1) for value in member["Config"]["Env"] if "=" in value
                        )
                        environment.update(
                            ODOO_DB_NAME=payload["database_name"],
                            LAUNCHPLANE_RUNTIME_IDENTITY_JSON=json.dumps(
                                payload["runtime_identity"]
                            ),
                        )
                        member["Config"]["Env"] = [
                            key + "=" + value for key, value in environment.items()
                        ]
                root = host.root / "fixture-home/.local/state/launchplane-testing-disposal"
                host.containers["foreign"] = {
                    "Id": "foreign",
                    "Config": {"Labels": {}},
                    "Mounts": [{"Source": str(root), "RW": True}],
                }
                frame = host.framed_mode(payload, default_layout=True)
                del host.containers["foreign"]
                # The previously writable, same-inode namespace can now carry
                # a forged empty WAL. Only CP's observed loss may stop adoption.
                receipt = (
                    root
                    / payload["identity"]["namespace"]
                    / ("native-" + payload["identity_sha256"] + ".json")
                )
                data = json.loads(receipt.read_text())
                data.update(protocol=2, state="retirement", retirement={}, resources={})
                receipt.write_text(json.dumps(data))
            else:
                frame = host.framed_mode(payload, default_layout=late_shared_exposure)
            kwargs["receive"](frame)

        with (
            patch.object(
                maintenance,
                "native_maintenance_operation",
                side_effect=transport.native_maintenance_operation,
            ),
            patch.object(transport, "_read_remote", side_effect=remote),
            patch.object(maintenance, "close_native_admissions", side_effect=lost_close),
        ):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.worker.complete_second_rebuild(
                    scheduled=True, scheduled_at=tick + DRILL_INTERVAL_SECONDS
                )
            stop = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.worker.operation.operation_id + ":retire-old",
            )
            assert stop is not None
            self.assertEqual(
                bool(stop.provider_effect_started_at), not exposure or late_shared_exposure
            )
            self.assertIsNone(maintenance._qualification(self.store, original_copy.namespace))
            effects = list(self.worker.effects)
            if exposure:
                host.containers.pop("foreign", None)
                calls = list(host.calls)
                with self.assertRaises(service.NativeTestingRebuildPending):
                    self.worker.execute()
                self.assertEqual(host.calls, calls)
                self.assertEqual(self.worker.effects, effects)
                self.assertIsNone(maintenance._qualification(self.store, original_copy.namespace))
                return
            self.worker.fail_public = unhealthy
            if lost_rollback:
                with self.assertRaises(service.NativeTestingRebuildPending):
                    self.worker.execute()
                self.assertFalse(self.worker.fail_public)
            result = self.worker.execute()
            self.assertEqual(result.deploy_status, "fail" if unhealthy else "pass")
            self.assertFalse(any(call[0] == "stop" for call in host.calls))
            parent = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE,
                idempotency_key=self.worker.operation.operation_id,
            )
            assert parent is not None
            intent = service.NativeTestingRebuildIntent.model_validate(
                self.store.read_original_mutation_intent(parent)
            )
            record = RecordTestingRebuildStore(self.store).load_operation(
                product=intent.profile.product,
                context=self.context,
                operation_id=self.worker.operation.operation_id,
            )
            if not unhealthy:
                assert record.retirement is not None
                self.assertEqual(record.retirement.status, "preserved")
                self.assertEqual(self.worker.effects, effects)
            else:
                self.assertEqual(
                    self.worker.current().target_id.target_id, original_copy.compose_id
                )
                self.assertIsNotNone(record.retirement_recovery)
                self.store.write_odoo_stable_target_replacement_operation_record(
                    _target_replacement_terminal_operation(
                        operation=self.worker.operation,
                        result=result,
                        lease_owner=self.worker.operation.lease_owner,
                    )
                )
                self.worker.fail_public = False
                original_spec = self.store.read_original_mutation_intent(stop)["native_retirement"]
                admission = self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=REBUILD_ROUTE + ":retirement-admission",
                    idempotency_key=stop.idempotency_key + ":1",
                )
                assert admission is not None
                self.worker.fixture.candidate = original_copy
                self.worker.app = original_app
                with patch.object(
                    maintenance, "native_maintenance_operation", side_effect=self.native_maintenance
                ):
                    self.worker.complete_second_rebuild(
                        operation_id="manual-after-original-exclusion", previous_status="fail"
                    )
                qualified = maintenance._qualification(self.store, original_copy.namespace)
                assert qualified is not None
                self.assertEqual(
                    qualified["identity"]["retiring_operation_id"],
                    "manual-after-original-exclusion",
                )
                self.assertEqual(
                    self.store.read_original_mutation_intent(stop)["native_retirement"],
                    original_spec,
                )
                late = host.framed_mode(
                    {
                        **original_spec,
                        **admission.response_payload,
                        "mode": "qualify-retirement",
                        "receipt_namespaces": [admission.response_payload["namespace_sha256"]],
                    }
                )
                self.assertEqual(late["status"], "preserved")
                self.assertFalse(any(call[0] == "stop" for call in host.calls))

    def test_stale_scheduler_snapshot_keeps_fresh_refusal_original_operation_blocked(self) -> None:
        self.refuse_after_writer_dispatch()
        scheduler = OdooTestingDrillScheduler(self.store)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        stale = receipt.model_copy(
            update={
                "response_payload": {
                    key: value
                    for key, value in receipt.response_payload.items()
                    if key != "native_refusal"
                }
            }
        )
        self.assertTrue(
            scheduler._observe(
                self.profile.product,
                self.context,
                stale,
                datetime.now(UTC).timestamp() + DRILL_TIMEOUT_SECONDS,
            )
        )

    def test_later_skip_keeps_an_earlier_genuine_failure_alert_pending(self) -> None:
        self.refuse_after_writer_dispatch()
        self.store.update_odoo_testing_drill_receipt(
            self.worker.operation.idempotency_key,
            datetime.now(UTC).timestamp(),
            {
                "status": "skipped",
                "failure_step": DRILL_DEADLINE_STEP,
                "failure_trace": self.worker.operation.operation_id,
                "alert_delivered": False,
                "next_alert_at": 0,
            },
        )
        notify = Mock(return_value=False)
        scheduler = OdooTestingDrillScheduler(self.store, notify=notify)
        scheduler._deliver_alerts(self.profile.product, self.context, datetime.now(UTC).timestamp())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[2]["failure_step"], DRILL_DEADLINE_STEP)

    def test_native_cleanup_continuation_preserves_the_original_slot_without_refusal(self) -> None:
        from control_plane.odoo_testing_drill import DrillPrerequisite, native_drill_refusal

        with (
            patch(
                "control_plane.odoo_testing_native_maintenance.reconcile_native_drill_storage",
                side_effect=DrillPrerequisite("native_testing_copy_cleanup_pending"),
            ),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.complete_second_rebuild(scheduled=True)
        self.assertIsNone(native_drill_refusal(self.store, self.worker.operation))
        scheduler = OdooTestingDrillScheduler(self.store)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        self.assertTrue(
            scheduler._observe(
                self.profile.product, self.context, receipt, datetime.now(UTC).timestamp()
            )
        )

    def test_cleanup_larger_than_one_pass_resumes_without_allocating_another_slot(self) -> None:
        from types import SimpleNamespace
        from control_plane import odoo_testing_native_maintenance as maintenance
        from control_plane.odoo_testing_drill import DRILL_SCOPE, DrillPrerequisite

        candidate = self.worker.fixture.candidate
        operation = self.worker.operation.model_copy(update={"idempotency_scope": DRILL_SCOPE})
        executor = SimpleNamespace(operation=operation, store=self.store)
        with (
            patch.object(
                maintenance,
                "native_drill_maintenance_candidates",
                side_effect=[(candidate,) * MAX_DISPOSALS_PER_PASS, (candidate,)],
            ),
            patch.object(maintenance, "_qualification", return_value={}),
            patch.object(maintenance, "_reconcile_copy") as reconcile,
        ):
            with self.assertRaises(DrillPrerequisite) as refused:
                maintenance.reconcile_native_drill_storage(executor)
            self.assertEqual(refused.exception.code, "native_testing_copy_cleanup_pending")
            self.assertEqual(reconcile.call_count, MAX_DISPOSALS_PER_PASS)
        with patch.object(maintenance, "native_drill_maintenance_candidates", return_value=()):
            maintenance.reconcile_native_drill_storage(executor)
        self.assertEqual(executor.operation.operation_id, operation.operation_id)

    def seed_failed_native_allocation(self) -> tuple[TestingCandidateAllocation, float]:
        from control_plane.contracts.testing_candidate import testing_candidate_namespace
        from control_plane.odoo_testing_drill import drill_receipt, lane_key

        tick = datetime.now(UTC).timestamp()
        self.worker.complete_second_rebuild(scheduled=True, scheduled_at=tick)
        operation = self.worker.operation
        namespace = testing_candidate_namespace(
            operation.product, operation.context, "failed-original-allocation"
        )
        key = lane_key(operation.product, operation.context) + ":failed-original-allocation"
        failed = operation.model_copy(
            update={
                "operation_id": "failed-original-allocation",
                "idempotency_key": key,
                "status": "fail",
                "phase": "failed",
                "result": None,
                "finished_at": datetime.now(UTC).isoformat(),
                "error_message": "Fixture original storage result unknown",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(failed)
        candidate = self.worker.fixture.candidate.model_copy(
            update={
                "namespace": namespace,
                "operation_id": failed.operation_id,
                "compose_id": namespace + "-compose",
                "project_id": namespace + "-project",
                "environment_id": namespace + "-environment",
                "effect": "storage",
                "resources_binding_sha256": "",
                "application": None,
                "serving_target_id": self.worker.current().target_id.target_id,
            }
        )
        self.store.compare_and_write_testing_candidate(None, candidate)
        self.store.write_idempotency_record(
            drill_receipt(
                key,
                tick,
                {"native_rebuild": True, "operation_id": failed.operation_id, "status": "fail"},
            )
        )
        return candidate, tick

    def test_unknown_failed_allocation_blocks_another_physical_capture(self) -> None:
        candidate, tick = self.seed_failed_native_allocation()
        before = self.worker.current()
        with patch.object(service, "prepare_candidate_storage") as prepare:
            self.worker.complete_second_rebuild(
                scheduled=True, scheduled_at=tick + DRILL_INTERVAL_SECONDS, expected_status="fail"
            )
        prepare.assert_not_called()
        self.assertEqual(self.worker.current().target_id, before.target_id)
        self.assertIn(candidate.namespace, self.evidence().retained_candidate_namespaces)
        self.assertEqual(
            self.worker.execute().error_message,
            "native_testing_copy_recovery_or_retention_required",
        )

    def test_explicit_original_namespace_retention_supports_continuing_after_failure(self) -> None:
        candidate, tick = self.seed_failed_native_allocation()
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "odoo_testing_drill_storage": self.budget.model_copy(
                                    update={"retained_candidate_namespaces": (candidate.namespace,)}
                                )
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in profile.lanes
                    )
                }
            )
        )
        self.worker.complete_second_rebuild(
            scheduled=True, scheduled_at=tick + DRILL_INTERVAL_SECONDS
        )
        self.assertEqual(self.worker.execute().deploy_status, "pass")
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)

    def test_lost_original_inverse_reply_finishes_recovery_under_sticky_refusal(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport

        self.refuse_after_writer_dispatch()
        route = self.worker.fixture.route
        self.worker.switch(
            route,
            route.new_backend,
            expected_backend=route.old_backend,
            dispatch_guard=lambda send: send(),
            desired_runtime_identity=self.worker.app.plan.runtime_identity,
        )
        self.worker.fail_public = True

        def lost(*args: Any, **kwargs: Any) -> Any:
            result = self.worker.switch(*args, **kwargs)
            if args[1] == route.old_backend:
                raise NativeSourceUnavailable(
                    "Fixture inverse reply lost after physical application"
                )
            return result

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=lost),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.execute()
        effects = list(self.worker.effects)
        result = self.worker.execute()
        self.assertEqual(result.error_code, "candidate_public_health_failed")
        self.assertEqual(self.worker.effects, effects)

    def test_native_capacity_refusal_precedes_preparation_and_uses_no_legacy_receiver(self) -> None:
        self.worker.capacity = False
        before = self.worker.current()
        with (
            patch.object(service, "prepare_candidate_storage") as prepare,
            patch("control_plane.odoo_testing_storage.execute_maintenance") as legacy,
        ):
            self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        prepare.assert_not_called()
        legacy.assert_not_called()
        self.assertEqual(self.worker.current(), before)
        self.assertEqual(self.evidence().storage_status, "insufficient")
        self.assertEqual(self.worker.fixture.fixture.effects, [])
        scheduler = OdooTestingDrillScheduler(self.store)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        self.assertFalse(
            scheduler._observe(
                self.profile.product, self.context, receipt, datetime.now(UTC).timestamp()
            )
        )
        self.assertEqual(self.evidence().status, "fail")
        self.assertEqual(self.evidence().step, "testing_storage_capacity_insufficient")

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

    def test_retention_edit_during_preparation_does_not_invalidate_native_admission(self) -> None:
        prepare = service.NativeTestingRebuildExecution.prepare

        def retain_copy(executor: Any, plan: Any) -> Any:
            result = prepare(executor, plan)
            profile = self.store.read_product_profile_record(self.profile.product)
            self.store.write_product_profile_record(
                profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(
                                update={
                                    "odoo_testing_drill_storage": self.budget.model_copy(
                                        update={"retained_copy_bindings": ("a" * 64,)}
                                    )
                                }
                            )
                            if lane.instance == "testing"
                            else lane
                            for lane in profile.lanes
                        )
                    }
                )
            )
            return result

        with patch.object(service.NativeTestingRebuildExecution, "prepare", retain_copy):
            self.worker.complete_second_rebuild(scheduled=True)
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"] * 2)

    def test_custody_history_has_truthful_totals_and_bounded_database_reads(self) -> None:
        from control_plane.odoo_testing_drill import drill_receipt, lane_key
        from control_plane.contracts.testing_candidate import testing_candidate_namespace
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            OdooStableTargetReplacementOperationRecord,
        )

        self.worker.complete_second_rebuild(scheduled=True)
        reads: list[object] = []

        def read(*_args: Any) -> None:
            reads.append(None)

        def observed() -> tuple[OdooTestingDrillEvidence, int]:
            reads.clear()
            event.listen(self.store._engine, "before_cursor_execute", read)
            try:
                result = self.evidence()
            finally:
                event.remove(self.store._engine, "before_cursor_execute", read)
            return result, len(reads)

        initial, initial_reads = observed()
        namespaces = set()
        now = datetime.now(UTC)
        for number in range(MAX_DISPOSALS_PER_PASS * 5):
            operation_id = f"custody-query-failed-{number}"
            key = lane_key(self.profile.product, self.context) + f":custody-query-{number}"
            operation = OdooStableTargetReplacementOperationRecord.model_validate(
                self.worker.operation.model_copy(
                    update={
                        "operation_id": operation_id,
                        "idempotency_key": key,
                        "status": "fail",
                        "phase": "failed",
                        "finished_at": now.isoformat(),
                        "error_message": "Fixture refusal",
                        "result": None,
                    }
                ).model_dump()
            )
            self.store.write_odoo_stable_target_replacement_operation_record(operation)
            namespace = testing_candidate_namespace(
                self.profile.product, self.context, operation_id
            )
            candidate = TestingCandidateAllocation.model_validate(
                self.worker.fixture.candidate.model_copy(
                    update={
                        "operation_id": operation_id,
                        "namespace": namespace,
                        "serving_target_id": self.worker.current().target_id.target_id,
                        "project_id": namespace + "-project",
                        "environment_id": "",
                        "compose_id": "",
                        "resources_binding_sha256": "",
                        "effect": "project_id",
                        "application": None,
                    }
                ).model_dump()
            )
            self.store.compare_and_write_testing_candidate(None, candidate)
            self.store.write_idempotency_record(
                drill_receipt(
                    key,
                    now.timestamp(),
                    {"native_rebuild": True, "operation_id": operation_id, "status": "fail"},
                )
            )
            namespaces.add(namespace)
        evidence, history_reads = observed()
        self.assertEqual(evidence.disposal_attention, initial.disposal_attention + len(namespaces))
        self.assertEqual(len(evidence.retained_candidate_namespaces), MAX_DISPOSALS_PER_PASS)
        self.assertTrue(set(evidence.retained_candidate_namespaces) <= namespaces)
        self.assertEqual(history_reads, initial_reads)

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
        self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        self.assertEqual(observed, [self.worker.fixture.candidate.namespace])
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])
        self.assertIsNotNone(self.signins.call_args.kwargs["since"])
        self.assert_skipped_refusal("recent_successful_signin")

    def assert_skipped_refusal(self, code: str) -> None:
        notifications = Mock()
        scheduler = OdooTestingDrillScheduler(self.store, notify=notifications)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        self.assertFalse(
            scheduler._observe(
                self.profile.product,
                self.context,
                receipt,
                datetime.now(UTC).timestamp() + DRILL_TIMEOUT_SECONDS,
            )
        )
        self.assertEqual(self.evidence().status, "skipped")
        self.assertEqual(self.evidence().step, code)
        scheduler._deliver_alerts(self.profile.product, self.context, datetime.now(UTC).timestamp())
        notifications.assert_not_called()
        effects = list(self.worker.effects)
        self.signins.return_value = False
        self.assertEqual(self.worker.execute().deploy_status, "fail")
        self.assertEqual(self.worker.effects, effects)

    def test_staff_hold_after_allocation_ends_without_a_new_forward_write(self) -> None:
        def held_after_allocation(**kwargs: Any) -> Any:
            allocation = prepare_candidate_storage(**kwargs)
            self.worker.apply_staff_hold(True, "Fixture staff start testing during preparation")
            return allocation

        with patch.object(service, "prepare_candidate_storage", side_effect=held_after_allocation):
            self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        self.assert_skipped_refusal("staff_testing_hold")
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])

    def test_bounded_storage_reconciliation_continues_without_a_terminal_refusal(self) -> None:
        from control_plane.odoo_testing_drill import DrillPrerequisite, native_drill_refusal

        with (
            patch(
                "control_plane.odoo_testing_storage.prepare_drill_storage",
                side_effect=DrillPrerequisite("testing_storage_reconciliation_pending"),
            ),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.complete_second_rebuild(scheduled=True)
        self.assertIsNone(native_drill_refusal(self.store, self.worker.operation))
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])

    def test_staff_hold_during_route_preparing_ends_without_a_forward_write(self) -> None:
        fence = self.worker.route_fence_ensure.return_value

        def held(_custody: Any) -> Any:
            self.worker.apply_staff_hold(True, "Fixture staff hold after route preparation")
            return fence

        self.worker.route_fence_ensure.side_effect = held
        self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        self.assert_skipped_refusal("staff_testing_hold")
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])

    def test_snapshot_refusal_during_route_preparation_excludes_the_original_parent(self) -> None:
        from control_plane.odoo_testing_drill import DrillSkipped
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE

        preparers = []

        def refused(_custody: Any) -> Any:
            preparers.append(
                self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=REBUILD_ROUTE + ":effect",
                    idempotency_key=self.worker.operation.operation_id
                    + ":route:"
                    + self.worker.fixture.candidate.namespace,
                )
            )
            raise DrillSkipped("drill_admission_snapshot_changed")

        self.worker.route_fence_ensure.side_effect = refused
        self.worker.complete_second_rebuild(scheduled=True, expected_status="fail")
        route = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.worker.operation.operation_id
            + ":route:"
            + self.worker.fixture.candidate.namespace,
        )
        assert route is not None
        self.assertEqual(route.state, "completed")
        self.assertTrue(route.response_payload["route_write_not_started"])
        self.assertGreater(route.attempt, 1)
        assert preparers[0] is not None
        self.assertNotEqual(
            self.store.checkpoint_mutation_provider_effect(
                reservation=preparers[0], effect_phase="late-fixture-writer", lease_seconds=300
            ).status,
            "updated",
        )
        self.assert_skipped_refusal("drill_admission_snapshot_changed")

    def refuse_after_writer_dispatch(self) -> None:
        from control_plane.odoo_testing_drill import DrillSkipped
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport

        def uncertain(*_: Any, **kwargs: Any) -> None:
            def lost() -> None:
                raise DrillSkipped("recent_successful_signin")

            kwargs["dispatch_guard"](lost)

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=uncertain),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.complete_second_rebuild(scheduled=True)

    def test_refusal_with_a_writing_route_stays_pending_with_its_concrete_skip_reason(
        self,
    ) -> None:
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport

        self.refuse_after_writer_dispatch()
        notifications = Mock()
        scheduler = OdooTestingDrillScheduler(self.store, notify=notifications)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        later = datetime.now(UTC).timestamp() + DRILL_TIMEOUT_SECONDS
        self.assertTrue(scheduler._observe(self.profile.product, self.context, receipt, later))
        self.assertEqual(self.evidence().status, "skipped")
        self.assertEqual(self.evidence().step, "recent_successful_signin")
        scheduler._deliver_alerts(self.profile.product, self.context, later)
        notifications.assert_not_called()
        effects = list(self.worker.effects)
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.worker.execute()
        self.assertEqual(self.worker.effects, effects)
        # An eventual physical exclusion is observed on the original writer;
        # the sticky skip must not prevent that observation or create a retry.
        with patch.object(DokployTestingRouteTransport, "settle_attempt", return_value="excluded"):
            result = self.worker.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "recent_successful_signin")
        self.assertEqual(self.worker.effects, effects)

    def test_refusal_preserves_observation_and_inverse_of_an_applied_original_writer(self) -> None:
        self.refuse_after_writer_dispatch()
        route = self.worker.fixture.route
        self.worker.switch(
            route,
            route.new_backend,
            expected_backend=route.old_backend,
            dispatch_guard=lambda send: send(),
            desired_runtime_identity=self.worker.app.plan.runtime_identity,
        )
        self.worker.fail_public = True
        self.signins.return_value = True
        effects = list(self.worker.effects)
        result = self.worker.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_code, "candidate_public_health_failed")
        self.assertEqual(self.worker.effects, effects + ["switch-old"])
        self.assertEqual(self.worker.current().target_id, self.worker.fixture.before.target_id)
        from control_plane.workflows.odoo_stable_operation_worker import (
            _target_replacement_terminal_operation,
        )

        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=_target_replacement_terminal_operation(
                    operation=self.worker.operation,
                    result=result,
                    lease_owner=self.worker.operation.lease_owner,
                ),
                lease_owner=self.worker.operation.lease_owner,
            )
        )
        notifications = Mock(return_value=True)
        scheduler = OdooTestingDrillScheduler(self.store, notify=notifications)
        receipt = scheduler._read(self.worker.operation.idempotency_key)
        assert receipt is not None
        scheduler._observe(
            self.profile.product, self.context, receipt, datetime.now(UTC).timestamp()
        )
        self.assertEqual(self.evidence().status, "fail")
        self.assertEqual(self.evidence().step, result.error_code)
        scheduler._deliver_alerts(self.profile.product, self.context, datetime.now(UTC).timestamp())
        notifications.assert_called_once()

    def test_recorded_refusal_survives_a_lost_reply_and_cleared_login(self) -> None:
        finish = service.NativeTestingRebuildExecution.finish_admission_refusal
        original_read = service.NativeTestingRebuildExecution.current_route
        reads = []

        def unavailable(executor: Any, plan: Any) -> Any:
            if self.signins.return_value:
                reads.append(plan)
                raise service.NativeTestingRebuildPending("Fixture original route read unavailable")
            return original_read(executor, plan)

        def denied(_custody: Any) -> Any:
            from control_plane.odoo_testing_drill import DrillSkipped

            self.signins.return_value = True
            raise DrillSkipped("recent_successful_signin")

        self.worker.route_fence_ensure.side_effect = denied
        with (
            patch.object(service.NativeTestingRebuildExecution, "current_route", unavailable),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.complete_second_rebuild(scheduled=True)
        self.assertTrue(reads)
        self.signins.return_value = False
        effects = list(self.worker.effects)
        with patch.object(
            service.NativeTestingRebuildExecution, "finish_admission_refusal", autospec=True
        ) as ending:
            ending.side_effect = finish
            result = self.worker.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "recent_successful_signin")
        self.assertEqual(self.worker.effects, effects)
