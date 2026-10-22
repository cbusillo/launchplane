"""Retirement settles only an idle held execute and never invents recovery copies."""

import unittest
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

from control_plane.odoo_upgrade_rehearsal import (
    OdooRehearsalAdapter,
    RehearsalRefused,
    rehearsal_credential_bindings,
)
from control_plane.testing_local_credentials import prepare_rehearsal_local_credentials
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE,
    RehearsalPrerequisite,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from tests import test_odoo_upgrade_rehearsal as rehearsal


def native_fixture(record: Any, key: str, digest: str, disposed: bool = False) -> dict[str, object]:
    from control_plane.contracts.odoo_upgrade_rehearsal import IncompleteRehearsalHostResult
    from control_plane.contracts.canonical_json import canonical_json_sha256

    return IncompleteRehearsalHostResult(
        qualified=True,
        disposed=disposed,
        binding_sha256=digest,
        intent_sha256=record.intent_sha256,
        attempt_sha256=canonical_json_sha256({"operation_key": key}),
        containers_absent=disposed,
        volumes_absent=disposed,
        network_absent=disposed,
        raw_source_absent=disposed,
        public_route=False,
        business_host_frozen=False,
    ).model_dump(mode="json")


class IncompleteRehearsalRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.prepared = self.fixture.record.model_copy(
            update={
                "state": "prepared",
                "compose_id": "own-compose",
                "project_id": "own-project",
                "environment_id": "own-environment",
            }
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.fixture.record, self.prepared)
        prepare_rehearsal_local_credentials(
            self.store, self.fixture.identifier, actor=self.fixture.principal
        )
        self.execute = self.fixture.adapter("execute")
        self.key = "original-held-execute"
        with patch.object(OdooRehearsalAdapter, "host", side_effect=TimeoutError("held")):
            with self.assertRaises(TimeoutError):
                self.fixture.run_operation(self.execute, self.key)

    def retirement(self, key: str = "") -> Any:
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement

        return IncompleteRehearsalRetirement(
            self.fixture.adapter("retire-incomplete"), key or self.key
        )

    def run_retirement(self, operation: Any) -> Any:
        from control_plane.provider_operations import run_durable_provider_operation

        return run_durable_provider_operation(
            store=self.store,
            scope=operation.reservation_scope(),
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key=operation.original_key,
            request_fingerprint=operation.request_fingerprint(),
            lease_owner="retirement-worker",
            response_trace_id="retirement-trace",
            adapter=operation,
        )

    def test_held_incomplete_operation_retires_and_replays_with_readback(self) -> None:
        import click

        # Apply also qualifies through dry-run before any disposal checkpoint.
        # A refused qualification must not strand a second hold on its key.
        for unavailable in ({"log_available": "true"}, "script-unmaterializable"):
            refused_operation = self.retirement()
            held = self.store.list_held_provider_target_reservations()
            original = refused_operation.original(self.prepared)
            with (
                self.subTest(unavailable=unavailable),
                patch.object(type(refused_operation), "placement", return_value=False),
                patch.object(
                    OdooRehearsalAdapter,
                    "run_script",
                    return_value=unavailable if isinstance(unavailable, dict) else {},
                    side_effect=refused_operation.adapter.run_script
                    if isinstance(unavailable, str)
                    else None,
                ) as dispatch,
                patch(
                    "control_plane.dokploy.api.validate_dokploy_schedule_script",
                    side_effect=click.ClickException("Script cannot be materialized")
                    if isinstance(unavailable, str)
                    else None,
                ),
            ):
                with self.assertRaises(RehearsalRefused):
                    self.run_retirement(refused_operation)
                if isinstance(unavailable, str):
                    dispatch.assert_not_called()
            self.assertEqual(refused_operation.original(self.prepared), original)
            self.assertEqual(self.store.list_held_provider_target_reservations(), held)
            self.assertEqual(
                self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), self.prepared
            )
            self.assertIsNone(
                self.store.read_idempotency_record(
                    scope=refused_operation.reservation_scope(),
                    route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                    idempotency_key=self.key,
                )
            )
        from control_plane.storage.postgres import LaunchplaneOdooUpgradeRehearsalRow

        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOdooUpgradeRehearsalRow, self.prepared.rehearsal_id)
            assert row is not None
            row.payload = {
                key: value for key, value in row.payload.items() if key != "incomplete_retirement"
            }
            session.commit()
        operation = self.retirement()
        native = native_fixture(self.prepared, self.key, "a" * 64)
        initial = self.fixture.copy("source").model_dump(mode="json")
        native["initial_source_snapshot"] = initial
        with (
            patch.object(
                type(operation),
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ) as host,
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            outcome = self.run_retirement(operation)
            self.assertEqual(outcome.status, "completed")
            result = outcome.response_payload["result"]
            self.assertTrue(result["disposed"])
            self.assertEqual(result["state"], "retired")
            self.assertEqual(result["copies"], [])
            self.assertTrue(result["original_operation_released"])
            self.assertEqual(result["host_disposal"]["initial_source_snapshot"], initial)
            count = host.call_count
            self.assertEqual(operation.readback()["state"], "retired")
            replay = self.run_retirement(operation)
            self.assertEqual(replay.status, "replayed")
            self.assertEqual(replay.response_payload["result"], result)
            with self.assertRaises(RehearsalRefused):
                self.run_retirement(self.retirement("different-key-after-retirement"))
            self.assertEqual(host.call_count, count)
        original = self.store.read_idempotency_record(
            scope=self.fixture.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key=self.key,
        )
        assert original is not None
        self.assertEqual(original.state, "completed")
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_failed_publication_keeps_original_hold_record_and_target_registration(self) -> None:
        operation = self.retirement()
        native = native_fixture(self.prepared, self.key, "b" * 64)
        owned = self.store.read_provider_target_record(
            context_name=self.fixture.identifier, instance_name="rehearsal"
        )
        with (
            patch.object(
                type(operation),
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ),
            patch.object(type(operation), "placement", return_value=True),
        ):
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": operation.plan()["plan_sha256"]}
            )
            with (
                patch.object(
                    self.store,
                    "_sync_idempotency_row",
                    side_effect=RuntimeError("publication failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                operation.run(
                    type(
                        "Lease",
                        (),
                        {
                            "assert_current": lambda self: None,
                            "checkpoint_effect": lambda self, phase: None,
                        },
                    )()
                )
        current = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        self.assertEqual(current.state, self.prepared.state)
        self.assertEqual(current.binding_sha256, self.prepared.binding_sha256)
        self.assertIsNotNone(current.incomplete_retirement)
        self.assertEqual(
            self.store.read_provider_target_record(
                context_name=self.fixture.identifier, instance_name="rehearsal"
            ),
            owned,
        )
        self.assertEqual(operation.original(self.prepared).state, "reconcile_required")
        with patch.object(type(operation), "placement", return_value=True):
            plan = operation.plan()
        self.assertNotIn("resources_preserved", plan)
        self.assertTrue(plan["host_disposal"]["disposed"])
        with self.assertRaises(RehearsalRefused):
            operation.readback()

    def test_same_key_recovers_each_interrupted_retirement_phase(self) -> None:
        for phase in (
            "precheck",
            "partial_host",
            "lost_host_reply",
            "lost_compose_reply",
            "publication",
        ):
            with self.subTest(phase=phase):
                scenario = IncompleteRehearsalRetirementTests()
                scenario.setUp()
                self.addCleanup(scenario.doCleanups)
                operation = scenario.retirement()
                flags = {
                    "failed": False,
                    "disposed": False,
                    "compose_absent": False,
                    "project_absent": False,
                    "planned": False,
                }

                def host(mode: str, check: Any) -> dict[str, object]:
                    if flags["compose_absent"]:
                        raise AssertionError("Host transport cannot use a removed compose")
                    if mode == "dry-run":
                        if flags["planned"] and phase == "precheck" and not flags["failed"]:
                            flags["failed"] = True
                            raise RuntimeError("Transient precheck failure")
                        return native_fixture(
                            scenario.prepared, scenario.key, "e" * 64, flags["disposed"]
                        )
                    if phase in {"partial_host", "lost_host_reply"} and not flags["failed"]:
                        flags["failed"] = True
                        flags["disposed"] = phase == "lost_host_reply"
                        raise RuntimeError("Host response lost after native progress")
                    flags["disposed"] = True
                    return native_fixture(scenario.prepared, scenario.key, "e" * 64, True)

                def placement(apply: bool = False, check: Any = None) -> bool:
                    if apply:
                        flags["compose_absent"] = True
                        if phase == "lost_compose_reply" and not flags["failed"]:
                            flags["failed"] = True
                            raise RuntimeError("Compose removal acknowledged by provider only")
                        flags["project_absent"] = True
                    return flags["project_absent"]

                publish = scenario.store.complete_incomplete_rehearsal_retirement

                def publication(**kwargs: Any) -> None:
                    if phase == "publication" and not flags["failed"]:
                        flags["failed"] = True
                        raise RuntimeError("Transient publication failure")
                    publish(**kwargs)

                with (
                    patch.object(type(operation), "host", side_effect=host),
                    patch.object(type(operation), "placement", side_effect=placement),
                    patch.object(
                        scenario.store,
                        "complete_incomplete_rehearsal_retirement",
                        side_effect=publication,
                    ),
                ):
                    plan = operation.plan()
                    flags["planned"] = True
                    operation.adapter.request = operation.adapter.request.model_copy(
                        update={"reviewed_plan_sha256": plan["plan_sha256"]}
                    )
                    with self.assertRaises(RuntimeError):
                        scenario.run_retirement(operation)
                    self.assertEqual(
                        operation.original(operation.record()).state, "reconcile_required"
                    )
                    if phase != "precheck":
                        self.assertNotIn("resources_preserved", operation.plan())
                        with self.assertRaises(RehearsalRefused):
                            operation.readback()
                    resumed = scenario.run_retirement(operation)
                    self.assertIn(resumed.status, {"completed", "adopted"})
                    self.assertTrue(resumed.response_payload["result"]["disposed"])
                    self.assertFalse(scenario.store.list_held_provider_target_reservations())

    def test_native_refusal_and_new_review_resume_the_same_original_journal(self) -> None:
        import copy
        from control_plane.contracts.canonical_json import canonical_json_sha256

        for failure in ("lost-refusal", "before-dispatch"):
            with self.subTest(failure=failure):
                scenario = IncompleteRehearsalRetirementTests()
                scenario.setUp()
                self.addCleanup(scenario.doCleanups)
                native = IncompleteRetirementHostTests()
                native.setUp()
                self.addCleanup(native.doCleanups)
                native.spec.update(
                    attempt_sha256=canonical_json_sha256({"operation_key": scenario.key}),
                    record_binding_sha256=scenario.prepared.binding_sha256,
                )
                native.spec["record"]["intent_sha256"] = scenario.prepared.intent_sha256
                original_web = native.fixture.fixture.containers["web"]["Id"]
                operation = scenario.retirement()
                old_script: dict[str, Any] = {}
                first = True

                def host(mode: str, check: Any) -> dict[str, object]:
                    nonlocal first, old_script
                    check()
                    current = scenario.store.read_odoo_upgrade_rehearsal(
                        scenario.fixture.identifier
                    )
                    assert current is not None
                    native.spec["mode"] = mode
                    native.spec["record"]["incomplete_retirement"] = (
                        current.incomplete_retirement.model_dump(mode="json")
                        if current.incomplete_retirement
                        else None
                    )
                    if mode == "apply" and first:
                        first = False
                        old_script = copy.deepcopy(native.spec)
                        native.fixture.fixture.containers["web"]["Id"] = "recreated-private-web"
                        if failure == "before-dispatch":
                            raise RuntimeError("Schedule did not dispatch")
                        # Persist the native no-effect refusal, then lose its reply.
                        try:
                            native.fixture.run_host(native.spec)
                        except RuntimeError:
                            raise TimeoutError("Native refusal reply lost") from None
                        self.fail("Changed inventory must refuse")
                    if mode == "apply" and old_script:
                        saved = copy.deepcopy(native.spec)
                        with self.assertRaises(RuntimeError):
                            native.fixture.run_host(old_script)
                        native.spec = saved
                    return native.fixture.run_host(native.spec)

                with (
                    patch.object(type(operation), "host", side_effect=host),
                    patch.object(type(operation), "placement", return_value=True),
                ):
                    review = operation.plan()["plan_sha256"]
                    operation.adapter.request = operation.adapter.request.model_copy(
                        update={"reviewed_plan_sha256": review}
                    )
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        scenario.run_retirement(operation)
                    self.assertEqual(
                        operation.original(scenario.prepared).state, "reconcile_required"
                    )
                    if failure == "before-dispatch":
                        self.assertTrue(operation.plan()["review_refusal_required"])
                        with self.assertRaises(RuntimeError):
                            scenario.run_retirement(operation)
                    self.assertTrue((native.root / "raw-source/database.dump").exists())
                    self.assertFalse((native.root / "incomplete-retirement.json").exists())
                    # Restoring the original inventory must still generate a new
                    # review, rather than revive the durably refused admission.
                    native.fixture.fixture.containers["web"]["Id"] = original_web
                    fresh = operation.plan()
                    self.assertNotEqual(fresh["plan_sha256"], review)
                    operation.adapter.request = operation.adapter.request.model_copy(
                        update={"reviewed_plan_sha256": fresh["plan_sha256"]}
                    )
                    result = scenario.run_retirement(operation)
                    self.assertEqual(result.status, "completed")
                    self.assertTrue(operation.readback()["disposed"])
                    self.assertEqual(scenario.run_retirement(operation).status, "replayed")
                    with self.assertRaises(RuntimeError):
                        native.fixture.run_host(old_script)

    def test_verified_or_running_operation_is_refused_before_host_effects(self) -> None:
        operation = self.retirement()
        with patch.object(type(operation), "host") as host:
            self.assertEqual(
                operation.observe(
                    "fixture-operation",
                    "dispose_incomplete_rehearsal",
                    operation.reconciliation_key(),
                ).outcome,
                "unknown",
            )
            verified = self.prepared.model_copy(update={"state": "verified"})
            self.store.compare_and_write_odoo_upgrade_rehearsal(self.prepared, verified)
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            self.store.compare_and_write_odoo_upgrade_rehearsal(verified, self.prepared)
            original = operation.original(self.prepared)
            self.store.retry_reconciled_mutation(
                reservation=original, lease_owner="still-running", preserve_effect_checkpoint=True
            )
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()

    def test_expired_running_execute_retires_without_replaying_the_upgrade(self) -> None:
        operation = self.retirement()
        retried = self.store.retry_reconciled_mutation(
            reservation=operation.original(self.prepared),
            lease_owner="lost-caller",
            preserve_effect_checkpoint=True,
        )
        assert retried.record is not None
        expired_at = datetime.fromisoformat(
            retried.record.lease_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with (
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=expired_at.isoformat().replace("+00:00", "Z"),
            ),
        ):
            # Reuse the complete disposal/replay/readback contract, with the
            # same bound checkpoint but a caller that never settled its lease.
            self.test_held_incomplete_operation_retires_and_replays_with_readback()

    def test_plan_binds_original_effect_identity_without_incidental_lease_metadata(self) -> None:
        operation = self.retirement()
        original = operation.original(self.prepared)
        native = native_fixture(self.prepared, self.key, "d" * 64)
        digest = operation.plan_digest(self.prepared, original, native)
        self.assertEqual(
            digest,
            operation.plan_digest(
                self.prepared,
                original.model_copy(
                    update={
                        "lease_owner": "new-observer",
                        "updated_at": "later-observation",
                        "attempt": original.attempt + 1,
                    }
                ),
                native,
            ),
        )
        self.assertNotEqual(
            digest,
            operation.plan_digest(
                self.prepared,
                original.model_copy(
                    update={
                        "provider_effect_started_at": "different-effect",
                    }
                ),
                native,
            ),
        )

    def test_placement_retirement_refuses_public_or_populated_projects_and_reads_absence(
        self,
    ) -> None:
        import copy
        from control_plane.dokploy.sandbox_placement import REHEARSAL_PROJECT_DESCRIPTION

        operation = self.retirement()
        environment = {
            "name": self.prepared.rehearsal_id,
            "environmentId": self.prepared.environment_id,
            **{
                key: []
                for key in (
                    "applications",
                    "postgres",
                    "mysql",
                    "mariadb",
                    "mongo",
                    "redis",
                    "libsql",
                )
            },
            "compose": [
                {"name": self.prepared.rehearsal_id, "composeId": self.prepared.compose_id}
            ],
        }
        project = {
            "name": self.prepared.rehearsal_id,
            "projectId": self.prepared.project_id,
            "description": REHEARSAL_PROJECT_DESCRIPTION,
            "environments": [environment],
        }
        protected = {"name": "production", "projectId": "production-project", "environments": []}
        projects = [project, protected]
        untouched = copy.deepcopy(protected)
        payload = {
            "serverId": self.prepared.server_id,
            "environmentId": self.prepared.environment_id,
            "appName": self.prepared.rehearsal_id,
            "env": "",
            "composeFile": "",
        }
        deletes: list[dict[str, Any]] = []

        def provider(**kwargs: Any) -> dict[str, object]:
            deletes.append(kwargs["payload"])
            if kwargs["path"] == "/api/compose.delete":
                self.assertEqual(kwargs["payload"]["composeId"], self.prepared.compose_id)
                self.assertFalse(kwargs["payload"]["deleteVolumes"])
                environment["compose"] = []
            elif kwargs["path"] == "/api/project.remove":
                self.assertEqual(kwargs["payload"]["projectId"], self.prepared.project_id)
                projects.remove(project)
            else:
                raise AssertionError("Unexpected provider effect")
            return {}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_projects",
                side_effect=lambda **kwargs: tuple(projects),
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload", return_value=payload),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                return_value=[],
            ) as domains,
            patch("control_plane.dokploy.api.dokploy_request", side_effect=provider),
        ):
            self.assertFalse(operation.placement())
            domains.return_value = [{"domainId": "protected-public-route"}]
            with self.assertRaises(RehearsalRefused):
                operation.placement(True)
            domains.return_value = []
            environment["libsql"] = [{"libsqlId": "protected-store"}]
            with self.assertRaises((ValueError, RehearsalRefused)):
                operation.placement(True)
            self.assertFalse(deletes)
            environment["libsql"] = []
            self.assertTrue(operation.placement(True))
            self.assertTrue(operation.placement())
            self.assertEqual(projects, [untouched])
        self.assertEqual(operation.original(self.prepared).state, "reconcile_required")

    def test_production_alias_and_another_key_are_refused(self) -> None:
        operation = self.retirement()
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        prod = next(lane for lane in self.fixture.profile.lanes if lane.instance == "prod")
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=prod.context,
                instance="prod",
                target_id=self.prepared.compose_id,
                updated_at=self.prepared.updated_at.isoformat(),
            )
        )
        with patch.object(type(operation), "host") as host:
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()
        with self.assertRaises(RehearsalRefused):
            self.retirement("replacement-key").plan()


class LaterPhaseRetirementTests(unittest.TestCase):
    """A completed later phase is disposed under the key that published it."""

    def setUp(self) -> None:
        self.fixture = rehearsal.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        prepared = self.fixture.record.model_copy(
            update={
                "state": "prepared",
                "compose_id": "own-compose",
                "project_id": "own-project",
                "environment_id": "own-environment",
            }
        )
        prepare_rehearsal_local_credentials(
            self.store, self.fixture.identifier, actor=self.fixture.principal
        )
        prepared = prepared.model_copy(
            update={
                "credential_bindings_sha256": rehearsal_credential_bindings(
                    self.store, self.fixture.identifier
                )
            }
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.fixture.record, prepared)
        copies: list[Any] = []

        def host(adapter: Any, record: Any, mode: str, checkpoint: Any, **_: Any) -> Any:
            checkpoint()
            action = adapter.request.action
            copies.append(
                self.fixture.copy({"execute": "source", "simulate-final": "final_source"}[action])
            )
            return {
                "intent_sha256": record.intent_sha256,
                "state": {"execute": "upgraded", "simulate-final": "switched"}[action],
                "copies": [copy.model_dump(mode="json") for copy in copies],
                "elapsed_seconds": {**record.elapsed_seconds, action: 0.1},
                "resources_preserved": True,
                "public_route": False,
                "business_host_frozen": False,
            }

        # Both upgrade phases complete under their own keys, as C11's did.
        with patch.object(OdooRehearsalAdapter, "host", autospec=True, side_effect=host):
            for action, key in (("execute", "initial-execute"), ("simulate-final", "final")):
                outcome = self.fixture.run_operation(self.fixture.adapter(action), key)
                self.assertEqual(outcome.status, "completed")
        switched = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert switched is not None and switched.state == "switched"
        self.switched = switched

    def journal(self, key: str) -> Any:
        return self.store.read_idempotency_record(
            scope=self.fixture.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key=key,
        )

    def retirement(self, key: str = "final") -> Any:
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement

        return IncompleteRehearsalRetirement(self.fixture.adapter("retire-incomplete"), key)

    def run_retirement(self, operation: Any) -> Any:
        return IncompleteRehearsalRetirementTests.run_retirement(cast(Any, self), operation)

    def native(self, snapshots: Any = None, key: str = "final") -> dict[str, object]:
        native = native_fixture(self.switched, key, "e" * 64)
        native["recovery_snapshots"] = [
            copy.model_dump(mode="json")
            for copy in (self.switched.copies if snapshots is None else snapshots)
        ]
        return native

    @staticmethod
    def host(native: dict[str, object]) -> Any:
        absence = ("containers_absent", "volumes_absent", "network_absent", "raw_source_absent")
        return lambda mode, check: {
            **native,
            **{key: mode != "dry-run" for key in ("disposed", *absence)},
        }

    def test_completed_switched_rehearsal_disposes_snapshots_and_keeps_custody_records(
        self,
    ) -> None:
        original = self.journal("final")
        operation = self.retirement()
        with (
            patch.object(type(operation), "host", side_effect=self.host(self.native())) as host,
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            self.assertTrue(plan["disposes_recovery_snapshots"])
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            outcome = self.run_retirement(operation)
            self.assertEqual(outcome.status, "completed")
            result = outcome.response_payload["result"]
            recorded = [copy.model_dump(mode="json") for copy in self.switched.copies]
            # The tuples stay recorded; only their bytes and runtime are disposed.
            self.assertEqual(result["copies"], recorded)
            self.assertEqual(result["host_disposal"]["recovery_snapshots"], recorded)
            self.assertIsNone(result["host_disposal"]["initial_source_snapshot"])
            self.assertFalse(result["recovery_tuples_produced"])
            count = host.call_count
            self.assertEqual(operation.readback(), result)
            replay = self.run_retirement(operation)
            self.assertEqual(replay.status, "replayed")
            self.assertEqual(replay.response_payload["result"], result)
            self.assertEqual(host.call_count, count)
        retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert retired is not None
        self.assertEqual(retired.state, "retired")
        self.assertEqual(retired.copies, self.switched.copies)
        # The completed upgrade journal is custody evidence, never rewritten.
        self.assertEqual(self.journal("final"), original)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        with self.assertRaises(FileNotFoundError):
            self.store.read_provider_target_record(
                context_name=self.fixture.identifier, instance_name="rehearsal"
            )

    def hold_uncertain_writer(self) -> None:
        """Leave a started, unsettled operation on the rehearsal's own target."""
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from control_plane.provider_operations import (
            ProviderObservation,
            run_durable_provider_operation,
        )

        identifier = self.fixture.identifier

        class UncertainWriter:
            def target_key(self) -> str:
                return identifier

            def reconciliation_key(self) -> str:
                return canonical_json_sha256(
                    {"rehearsal": identifier, "action": "validate-business"}
                )

            def apply(self, key: str, lease: Any) -> Any:
                lease.checkpoint_effect("validate-business")
                raise TimeoutError("observation lost after the writer started")

            def observe(self, *args: Any) -> Any:
                return ProviderObservation("unknown")

        with self.assertRaises(TimeoutError):
            run_durable_provider_operation(
                store=self.store,
                scope=self.fixture.principal,
                route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                idempotency_key="uncertain-validation",
                request_fingerprint="uncertain-validation",
                lease_owner="validation-worker",
                response_trace_id="validation-trace",
                adapter=cast(Any, UncertainWriter()),
            )
        self.assertTrue(self.store.list_held_provider_target_reservations())

    def test_unattributed_or_active_later_phase_refuses_before_host_effects(self) -> None:
        for case in ("earlier-key", "unknown-key", "verified", "held-writer"):
            with self.subTest(case=case):
                record = self.switched
                if case == "verified":
                    record = record.model_copy(update={"state": "verified"})
                    self.store.compare_and_write_odoo_upgrade_rehearsal(self.switched, record)
                if case == "held-writer":
                    self.hold_uncertain_writer()
                operation = self.retirement(
                    {"earlier-key": "initial-execute", "unknown-key": "never-sent"}.get(
                        case, "final"
                    )
                )
                with patch.object(type(operation), "host") as host:
                    with self.assertRaises(RehearsalRefused):
                        operation.plan()
                    host.assert_not_called()
                if case == "verified":
                    self.store.compare_and_write_odoo_upgrade_rehearsal(record, self.switched)
        self.assertEqual(
            self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), self.switched
        )

    def test_publication_refuses_an_operation_that_became_unsettled(self) -> None:
        # Publication rechecks inside its transaction, after every earlier check.
        self.hold_uncertain_writer()
        with self.assertRaises(ValueError):
            self.store.complete_incomplete_rehearsal_retirement(
                expected=self.switched,
                replacement=self.switched.model_copy(update={"state": "retired"}),
                original=self.journal("final"),
                trace_id="retirement-trace",
                result={"action": "retire-incomplete", "disposed": True},
            )
        self.assertEqual(
            self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), self.switched
        )
        self.store.read_provider_target_record(
            context_name=self.fixture.identifier, instance_name="rehearsal"
        )

    def test_host_must_bind_exactly_the_recorded_recovery_snapshots(self) -> None:
        import json

        operation = self.retirement()
        for snapshots in (self.switched.copies[:1], ()):
            evidence = {"rehearsal_result": json.dumps(self.native(snapshots)).encode().hex()}
            with (
                self.subTest(count=len(snapshots)),
                patch.object(OdooRehearsalAdapter, "host_script", return_value="fixture"),
                patch.object(OdooRehearsalAdapter, "validate_apply_script"),
                patch.object(OdooRehearsalAdapter, "run_script", return_value=evidence),
            ):
                with self.assertRaises(RehearsalRefused):
                    operation.host("dry-run", lambda: None)

    def test_completed_business_validation_key_retires_with_its_validations(self) -> None:
        from tests.test_odoo_rehearsal_business_validation import identity, receipt

        validation = receipt({**identity(self.switched), "attempt_sha256": "c" * 64})

        def host(adapter: Any, record: Any, mode: str, checkpoint: Any, **_: Any) -> Any:
            checkpoint()
            return {
                "intent_sha256": record.intent_sha256,
                "state": "switched",
                "copies": [copy.model_dump(mode="json") for copy in record.copies],
                "elapsed_seconds": {**record.elapsed_seconds, "validate-business": 0.1},
                "resources_preserved": True,
                "public_route": False,
                "business_host_frozen": False,
                "business_validation": validation,
            }

        with patch.object(OdooRehearsalAdapter, "host", autospec=True, side_effect=host):
            outcome = self.fixture.run_operation(
                self.fixture.adapter("validate-business"), "validation"
            )
        self.assertEqual(outcome.status, "completed")
        validated = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert validated is not None and validated.business_validations
        self.switched = validated
        operation = self.retirement("validation")
        with (
            patch.object(
                type(operation), "host", side_effect=self.host(self.native(key="validation"))
            ),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            result = self.run_retirement(operation).response_payload["result"]
            self.assertEqual(result["business_validations"], [validation])
            self.assertEqual(operation.readback(), result)
        with self.assertRaises(RehearsalRefused):
            # The earlier simulate-final key no longer published this phase.
            self.retirement("final").plan()

    def test_production_alias_refuses_a_later_phase_retirement(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        prod = next(lane for lane in self.fixture.profile.lanes if lane.instance == "prod")
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=prod.context,
                instance="prod",
                target_id=self.switched.compose_id,
                updated_at=self.switched.updated_at.isoformat(),
            )
        )
        operation = self.retirement()
        with patch.object(type(operation), "host") as host:
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()

    def test_unknown_validation_retires_working_copy_and_archives_original(self) -> None:
        self.hold_uncertain_writer()
        original = self.journal("uncertain-validation")
        operation = self.retirement("uncertain-validation")
        operation.adapter.request = operation.adapter.request.model_copy(
            update={"action": "abandon-validation"}
        )
        native = self.native(key="uncertain-validation")
        native["recovery_snapshots_preserved"] = True
        with (
            patch.object(type(operation), "host", side_effect=self.host(native)),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            self.assertFalse(plan["disposes_recovery_snapshots"])
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            publish = self.store.complete_incomplete_rehearsal_retirement

            def lost_reply(**kwargs: Any) -> None:
                publish(**kwargs)
                raise TimeoutError("Abandonment publication reply was lost")

            with patch.object(
                self.store, "complete_incomplete_rehearsal_retirement", side_effect=lost_reply
            ):
                with self.assertRaises(TimeoutError):
                    self.run_retirement(operation)
            result = self.run_retirement(operation)
            self.assertEqual(result.status, "adopted")
            self.assertEqual(result.response_payload["result"]["original_outcome"], "unknown")
            retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
            assert retired is not None
            self.assertEqual(retired.abandoned_validations[-1].original, original)
            self.assertEqual(retired.copies, self.switched.copies)
            self.assertFalse(self.store.list_held_provider_target_reservations())
            self.assertEqual(operation.readback(), result.response_payload["result"])
        with self.assertRaises(RehearsalRefused):
            self.retirement("uncertain-validation").record()

    def test_abandonment_refuses_other_phases_and_production_aliases(self) -> None:
        self.hold_uncertain_writer()
        operation = self.retirement("uncertain-validation")
        operation.adapter.request = operation.adapter.request.model_copy(
            update={"action": "abandon-validation"}
        )
        operation.original_key = "initial-execute"
        with self.assertRaises(RehearsalRefused):
            operation.plan()
        operation.original_key = "uncertain-validation"
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        prod = next(lane for lane in self.fixture.profile.lanes if lane.instance == "prod")
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=prod.context,
                instance="prod",
                target_id=self.switched.compose_id,
                updated_at=self.switched.updated_at.isoformat(),
            )
        )
        with patch.object(type(operation), "host") as host:
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()

    def test_rebuild_uses_new_resources_and_the_retained_final_tuple(self) -> None:
        from pathlib import Path
        from control_plane.odoo_upgrade_rehearsal import (
            require_rehearsal_transition,
            rehearsal_resource,
        )

        with self.assertRaises(RehearsalRefused):
            require_rehearsal_transition(self.switched, "rebuild-final")
        self.test_unknown_validation_retires_working_copy_and_archives_original()
        retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert retired is not None
        before = self.fixture.authority()
        adapter = self.fixture.adapter("rebuild-final")

        def create(root: Path, **kwargs: Any) -> None:
            kwargs["checkpoint"]()
            for field in ("project_id", "environment_id", "compose_id"):
                kwargs["before_create"](field)
                kwargs["record_identity"](field, "rebuilt-" + field)

        def resources(record: Any, script: str, checkpoint: Any) -> dict[str, str]:
            checkpoint()
            self.assertEqual(record.state, "rebuilding")
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": rehearsal_resource(record).binding_sha256,
            }

        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch(
                "control_plane.dokploy.sandbox_placement.create_isolated_placement",
                side_effect=create,
            ) as allocation,
            patch("control_plane.odoo_upgrade_rehearsal.require_bridge_network_capacity"),
            patch.object(OdooRehearsalAdapter, "check_business_entrypoint"),
            patch.object(adapter, "run_script", side_effect=resources),
            patch.object(
                adapter,
                "host",
                side_effect=[
                    {"refusal_code": "rehearsal_rebuild_capacity_insufficient"},
                    {"refusal_code": "rehearsal_rebuild_precheck_refused"},
                    {
                        "state": "switched",
                        "copies": [copy.model_dump(mode="json") for copy in retired.copies],
                        "elapsed_seconds": retired.elapsed_seconds,
                    },
                ],
            ),
            patch(
                "control_plane.odoo_upgrade_rehearsal._source_values",
                side_effect=AssertionError("No production recapture"),
            ),
        ):
            refused = self.fixture.run_operation(adapter, "capacity-refusal")
            self.assertEqual(refused.response_status_code, 409)
            prepared = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
            assert prepared is not None
            self.assertEqual(prepared.state, "rebuilding")
            self.assertFalse(self.store.list_held_provider_target_reservations())
            with patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=(prepared.project_id, prepared.environment_id, prepared.compose_id),
            ):
                refused = self.fixture.run_operation(adapter, "reuse-precheck")
                self.assertEqual(refused.response_status_code, 409)
                self.assertFalse(self.store.list_held_provider_target_reservations())
                outcome = self.fixture.run_operation(adapter, "new-final-copy")
            allocation.assert_called_once()
        self.assertEqual(outcome.status, "completed")
        rebuilt = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert rebuilt is not None
        self.assertEqual(rebuilt.state, "switched")
        self.assertEqual(rebuilt.compose_id, "rebuilt-compose_id")
        self.assertEqual(rebuilt.copies, retired.copies)
        self.assertEqual(rebuilt.abandoned_validations, retired.abandoned_validations)
        self.assertIsNone(rebuilt.incomplete_retirement)
        self.assertEqual(self.fixture.authority(), before)
        abandonment = self.retirement("uncertain-validation")
        abandonment.adapter.request = abandonment.adapter.request.model_copy(
            update={"action": "abandon-validation"}
        )
        with patch.object(
            type(abandonment),
            "placement",
            side_effect=AssertionError("Historical readback must not inspect the replacement"),
        ):
            self.assertEqual(abandonment.readback()["original_outcome"], "unknown")

    def test_retired_abandonment_history_does_not_skip_current_ownership(self) -> None:
        from types import SimpleNamespace
        from control_plane.odoo_upgrade_rehearsal import require_unaliased_rehearsal

        self.test_unknown_validation_retires_working_copy_and_archives_original()
        retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert retired is not None
        ordinarily_retired = retired.model_copy(update={"incomplete_retirement": None})
        with patch.object(
            self.store,
            "read_provider_target_record",
            return_value=SimpleNamespace(
                target_id=retired.compose_id, source_label="foreign-owner"
            ),
        ):
            with self.assertRaises(RehearsalRefused):
                require_unaliased_rehearsal(self.store, ordinarily_retired)

    def test_rebuild_determinate_prechecks_leave_retired_record_and_no_hold(self) -> None:
        from control_plane.dokploy.network_capacity import BridgeNetworkCapacityRefused

        self.test_unknown_validation_retires_working_copy_and_archives_original()
        retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        for target, error in (
            (
                "control_plane.odoo_upgrade_rehearsal.source.read_dokploy_config",
                ValueError("missing bootstrap"),
            ),
            (
                "control_plane.odoo_upgrade_rehearsal.require_bridge_network_capacity",
                BridgeNetworkCapacityRefused("host_bridge_network_capacity_short"),
            ),
            (
                "control_plane.odoo_upgrade_rehearsal.OdooRehearsalAdapter.check_business_entrypoint",
                RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE),
            ),
        ):
            with (
                self.subTest(target=target),
                patch(
                    "control_plane.dokploy.sandbox_placement.created_placement",
                    return_value=("", "", ""),
                ),
                patch("control_plane.odoo_upgrade_rehearsal.require_bridge_network_capacity"),
                patch.object(OdooRehearsalAdapter, "check_business_entrypoint"),
                patch(target, side_effect=error),
                patch(
                    "control_plane.dokploy.sandbox_placement.create_isolated_placement"
                ) as create,
            ):
                with self.assertRaises((RehearsalRefused, BridgeNetworkCapacityRefused)):
                    self.fixture.run_operation(self.fixture.adapter("rebuild-final"), target)
                create.assert_not_called()
                self.assertEqual(
                    self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), retired
                )
                self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_empty_rebuild_can_reconcile_only_before_provider_effects(self) -> None:
        self.test_unknown_validation_retires_working_copy_and_archives_original()
        retired = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert retired is not None
        adapter = self.fixture.adapter("rebuild-final")
        for state in ("retired", "rebuilding"):
            record = retired.model_copy(
                update={"state": state, "project_id": "", "environment_id": "", "compose_id": ""}
            )
            previous = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
            assert previous is not None
            self.store.compare_and_write_odoo_upgrade_rehearsal(previous, record)
            with (
                patch(
                    "control_plane.dokploy.sandbox_placement.created_placement",
                    return_value=("", "", ""),
                ),
                patch(
                    "control_plane.odoo_upgrade_rehearsal.source.read_dokploy_config",
                    return_value=("host", "token"),
                ),
                patch(
                    "control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None
                ),
                patch.object(adapter, "host", return_value={"completed": False}),
            ):
                self.assertTrue(adapter.observe("key", "", adapter.reconciliation_key()).retry_safe)
                self.assertEqual(
                    adapter.observe("key", "project_id", adapter.reconciliation_key()).outcome,
                    "unknown",
                )

    def test_verify_requires_validation_of_the_rebuilt_working_copy(self) -> None:
        from tests.test_odoo_rehearsal_business_validation import identity, receipt
        from control_plane.contracts.odoo_rehearsal_business_validation import (
            BusinessValidationReceipt,
        )
        from control_plane.odoo_upgrade_rehearsal import rehearsal_inputs

        old = self.switched
        validation = BusinessValidationReceipt.model_validate(receipt(identity(old)))
        self.switched = old.model_copy(update={"business_validations": (validation,)})
        self.store.compare_and_write_odoo_upgrade_rehearsal(old, self.switched)
        self.test_rebuild_uses_new_resources_and_the_retained_final_tuple()
        verify = self.fixture.adapter("verify")
        with self.assertRaises(RehearsalRefused):
            rehearsal_inputs(
                verify.root, self.store, verify.product, verify.request, verify.principal
            )
        rebuilt = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert rebuilt is not None
        fresh = receipt(
            {**identity(rebuilt), "attempt_sha256": canonical_json_sha256({"new": "copy"})}
        )
        self.fixture.adapter("validate-business").publish(
            rebuilt,
            {
                "state": "switched",
                "copies": [copy.model_dump(mode="json") for copy in rebuilt.copies],
                "elapsed_seconds": rebuilt.elapsed_seconds,
                "business_validation": fresh,
            },
        )
        rehearsal_inputs(verify.root, self.store, verify.product, verify.request, verify.principal)


class IncompleteRetirementHostTests(unittest.TestCase):
    def recovery_snapshot(self, role: str) -> dict[str, Any]:
        import hashlib
        import json
        from datetime import datetime, timezone

        names = self.fixture.host["database_names"](self.spec)
        snapshot = self.root / names[role]
        snapshot.mkdir()
        (snapshot / "database.dump").write_bytes(b"verified database " + role.encode())
        (snapshot / "filestore.tar").write_bytes(b"verified filestore " + role.encode())
        receipt = {
            "database": names[role],
            "witness_sha256": self.fixture.host["witness"](self.spec, names[role]),
            "database_sha256": hashlib.sha256(
                (snapshot / "database.dump").read_bytes()
            ).hexdigest(),
            "filestore_sha256": hashlib.sha256(
                (snapshot / "filestore.tar").read_bytes()
            ).hexdigest(),
            "source_capture_sha256": hashlib.sha256(
                (self.root / "capture.json").read_bytes()
            ).hexdigest(),
            "sanitized": True,
            "odoo_version": "18.0.1.3",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        (snapshot / "receipt.json").write_text(json.dumps(receipt))
        return receipt

    def later_phase(self) -> list[dict[str, Any]]:
        """Recreate C11's host custody: switched, with both source snapshots."""
        import hashlib
        import json

        names = self.fixture.host["database_names"](self.spec)
        record = self.spec["record"]
        record["source_odoo_version"] = "18.0"
        record["source_binding_sha256"] = hashlib.sha256(b"source").hexdigest()
        (self.root / "capture.json").write_text(
            json.dumps({"source_binding_sha256": record["source_binding_sha256"]})
        )
        # Launchplane serializes the same instant with a Z suffix.
        copies = [
            {**receipt, "captured_at": receipt["captured_at"].replace("+00:00", "Z")}
            for receipt in (
                self.recovery_snapshot("source"),
                self.recovery_snapshot("final_source"),
            )
        ]
        record.update(state="switched", copies=copies)
        for action, state, published in (
            ("prepare", "prepared", []),
            ("execute", "upgraded", copies[:1]),
            ("simulate-final", "switched", copies),
        ):
            (self.root / (action + ".started")).touch()
            (self.root / (action + ".json")).write_text(
                json.dumps(
                    {"intent_sha256": record["intent_sha256"], "state": state, "copies": published}
                )
            )
        for container in self.fixture.fixture.containers.values():
            container["ExecIDs"] = None
            container["Config"]["Env"] = [
                "ODOO_DB_NAME=" + names["final_upgrade"]
                if value.startswith("ODOO_DB_NAME=")
                else value
                for value in container["Config"]["Env"]
            ]
        # The private web served the switched copy; only an exec is a writer.
        self.fixture.fixture.containers["web"]["State"]["Running"] = True
        return copies

    def test_later_phase_disposal_binds_recorded_snapshots_and_stops_private_web(self) -> None:
        copies = self.later_phase()
        plan = self.fixture.run_host(self.spec)
        self.assertFalse(plan["disposed"])
        self.assertEqual(plan["recovery_snapshots"], copies)
        self.assertIsNone(plan["initial_source_snapshot"])
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))
        self.admit(plan)
        self.spec["mode"] = "apply"
        applied = self.fixture.run_host(self.spec)
        self.assertTrue(applied["disposed"])
        self.assertEqual(applied["recovery_snapshots"], copies)
        self.assertFalse(self.fixture.fixture.containers)
        self.assertFalse(self.fixture.fixture.volumes)
        for copy in copies:
            self.assertFalse((self.root / copy["database"]).exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"production fixture")
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])

    def test_interrupted_later_phase_apply_reproves_custody_before_resuming(self) -> None:
        for changed in (
            "none",
            "exec-session",
            "unrecorded-snapshot",
            "corrupt",
            "uncertain-effect",
        ):
            with self.subTest(changed=changed):
                native = IncompleteRetirementHostTests()
                native.setUp()
                self.addCleanup(native.doCleanups)
                copies = native.later_phase()
                names = native.fixture.host["database_names"](native.spec)
                native.admit(native.fixture.run_host(native.spec))
                native.spec["mode"] = "apply"
                docker = native.fixture.host["docker"]

                def interrupted(*args: str, stdin: bytes | None = None) -> bytes:
                    if args[0] == "stop":
                        raise RuntimeError("Interrupted after the disposal manifest")
                    return cast(bytes, docker(*args, stdin=stdin))

                native.fixture.host["docker"] = interrupted
                with self.assertRaises(RuntimeError):
                    native.fixture.run_host(native.spec)
                self.assertTrue((native.root / "incomplete-retirement.json").is_file())
                native.fixture.host["docker"] = docker
                if changed == "exec-session":
                    native.fixture.fixture.containers["script-runner"]["ExecIDs"] = ["active"]
                elif changed == "unrecorded-snapshot":
                    (native.root / names["final_upgrade"]).mkdir()
                elif changed == "corrupt":
                    (native.root / names["final_source"] / "database.dump").write_bytes(b"x")
                elif changed == "uncertain-effect":
                    (native.root / ("validate-business-" + "a" * 64 + ".started")).touch()
                native.calls.clear()
                if changed == "none":
                    self.assertTrue(native.fixture.run_host(native.spec)["disposed"])
                    continue
                with self.assertRaises(RuntimeError):
                    native.fixture.run_host(native.spec)
                self.assertFalse(any(call[0] in {"stop", "rm"} for call in native.calls))
                for copy in copies:
                    self.assertTrue((native.root / copy["database"] / "database.dump").exists())

    def test_later_phase_refuses_uncertain_or_unattributed_custody(self) -> None:
        import fcntl
        import json

        for changed in (
            "uncertain-effect",
            "exec-session",
            "unrecorded-snapshot",
            "corrupt",
            "aliased-snapshot",
            "receipt-differs",
            "no-producer",
            "verified",
            "busy",
        ):
            with self.subTest(changed=changed):
                native = IncompleteRetirementHostTests()
                native.setUp()
                self.addCleanup(native.doCleanups)
                copies = native.later_phase()
                names = native.fixture.host["database_names"](native.spec)
                if changed == "uncertain-effect":
                    (native.root / ("validate-business-" + "a" * 64 + ".started")).touch()
                elif changed == "exec-session":
                    native.fixture.fixture.containers["script-runner"]["ExecIDs"] = ["active"]
                elif changed == "unrecorded-snapshot":
                    (native.root / names["final_upgrade"]).mkdir()
                elif changed == "corrupt":
                    (native.root / names["final_source"] / "database.dump").write_bytes(b"x")
                elif changed == "aliased-snapshot":
                    # Validation through a link would let disposal leave the bytes behind.
                    path = native.root / names["final_source"]
                    path.rename(native.outside / "relocated")
                    path.symlink_to(native.outside / "relocated")
                    copies = copies[:1]
                elif changed == "receipt-differs":
                    native.spec["record"]["copies"][1] = {**copies[1], "odoo_version": "18.0.9"}
                elif changed == "no-producer":
                    (native.root / "simulate-final.json").write_text(
                        json.dumps(
                            {
                                "intent_sha256": native.spec["record"]["intent_sha256"],
                                "state": "switched",
                                "copies": copies[:1],
                            }
                        )
                    )
                elif changed == "verified":
                    native.spec["record"]["state"] = "verified"
                if changed == "busy":
                    with (native.root / ".execution.lock").open("r") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        with self.assertRaises(BlockingIOError):
                            native.fixture.run_host(native.spec)
                else:
                    with self.assertRaises((RuntimeError, OSError)):
                        native.fixture.run_host(native.spec)
                self.assertFalse(any(call[0] in {"stop", "rm"} for call in native.calls))
                for copy in copies:
                    self.assertTrue((native.root / copy["database"] / "database.dump").exists())

    def initial_snapshot(self, *, switched: bool = True) -> dict[str, Any]:
        import hashlib
        import json
        from datetime import datetime, timezone

        names = self.fixture.host["database_names"](self.spec)
        self.spec["record"]["source_odoo_version"] = "18.0"
        self.spec["record"]["source_binding_sha256"] = hashlib.sha256(b"source").hexdigest()
        capture = self.root / "capture.json"
        capture.write_text(
            json.dumps({"source_binding_sha256": self.spec["record"]["source_binding_sha256"]})
        )
        snapshot = self.root / names["source"]
        snapshot.mkdir()
        (snapshot / "database.dump").write_bytes(b"verified initial database")
        (snapshot / "filestore.tar").write_bytes(b"verified initial filestore")
        receipt = {
            "database": names["source"],
            "witness_sha256": self.fixture.host["witness"](self.spec, names["source"]),
            "database_sha256": hashlib.sha256(
                (snapshot / "database.dump").read_bytes()
            ).hexdigest(),
            "filestore_sha256": hashlib.sha256(
                (snapshot / "filestore.tar").read_bytes()
            ).hexdigest(),
            "source_capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "sanitized": True,
            "odoo_version": "18.0.1.3",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        (snapshot / "receipt.json").write_text(json.dumps(receipt))
        if switched:
            for role in ("web", "script-runner"):
                container = self.fixture.fixture.containers[role]
                container["Config"]["Env"] = [
                    "ODOO_DB_NAME=" + names["upgrade"]
                    if value.startswith("ODOO_DB_NAME=")
                    else value
                    for value in container["Config"]["Env"]
                ]
        return receipt

    def test_post_switch_disposal_binds_initial_snapshot_without_inventing_upgrade_proof(
        self,
    ) -> None:
        receipt = self.initial_snapshot()
        plan = self.fixture.run_host(self.spec)
        self.assertEqual(plan["initial_source_snapshot"], receipt)
        self.admit(plan)
        self.spec["mode"] = "apply"
        applied = self.fixture.run_host(self.spec)
        self.assertTrue(applied["disposed"])
        self.assertEqual(applied["initial_source_snapshot"], receipt)
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])
        self.assertFalse((self.root / receipt["database"]).exists())

    def test_unconfirmed_resource_absence_preserves_initial_snapshot_and_raw_capture(self) -> None:
        receipt = self.initial_snapshot(switched=False)
        plan = self.fixture.run_host(self.spec)
        self.admit(plan)
        self.spec["mode"] = "apply"
        docker = self.fixture.host["docker"]

        def unconfirmed(*args: str, stdin: bytes | None = None) -> bytes:
            if args[:2] == ("network", "rm"):
                return b""  # A successful response alone does not prove absence.
            return cast(bytes, docker(*args, stdin=stdin))

        self.fixture.host["docker"] = unconfirmed
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / receipt["database"] / "database.dump").exists())
        self.assertTrue((self.root / "raw-source/database.dump").exists())

    def test_post_switch_requires_verified_initial_custody_and_idle_initial_runtime(self) -> None:
        for changed in (
            "missing",
            "no-snapshot",
            "later-snapshot",
            "corrupt",
            "aliased",
            "lineage",
            "pending",
            "final",
            "writer",
        ):
            with self.subTest(changed=changed):
                native = IncompleteRetirementHostTests()
                native.setUp()
                self.addCleanup(native.doCleanups)
                receipt = native.initial_snapshot()
                path = native.root / receipt["database"]
                if changed == "missing":
                    (path / "receipt.json").unlink()
                elif changed == "no-snapshot":
                    import shutil

                    shutil.rmtree(path)
                elif changed == "later-snapshot":
                    (
                        native.root
                        / native.fixture.host["database_names"](native.spec)["final_upgrade"]
                    ).mkdir()
                elif changed == "corrupt":
                    (path / "database.dump").write_bytes(b"changed snapshot")
                elif changed == "aliased":
                    (path / "database.dump").unlink()
                    (path / "database.dump").symlink_to(native.outside / "keep")
                elif changed == "lineage":
                    (native.root / "capture.json").write_text("{}")
                elif changed == "pending":
                    (native.root / "capture.pending").touch()
                elif changed == "final":
                    for role in ("web", "script-runner"):
                        config = native.fixture.fixture.containers[role]["Config"]
                        config["Env"] = [
                            value.replace("_upgrade", "_final_upgrade") for value in config["Env"]
                        ]
                else:
                    native.fixture.fixture.containers["web"]["State"]["Running"] = True
                with self.assertRaises((RuntimeError, OSError)):
                    native.fixture.run_host(native.spec)
                self.assertFalse(any(call[0] in {"stop", "rm"} for call in native.calls))
                self.assertTrue((native.root / "raw-source/database.dump").exists())

    def test_snapshot_change_after_review_refuses_before_disposal(self) -> None:
        import json
        import hashlib

        receipt = self.initial_snapshot()
        plan = self.fixture.run_host(self.spec)
        self.admit(plan)
        path = self.root / receipt["database"]
        (path / "database.dump").write_bytes(b"replacement snapshot")
        receipt["database_sha256"] = hashlib.sha256(
            (path / "database.dump").read_bytes()
        ).hexdigest()
        (path / "receipt.json").write_text(json.dumps(receipt))
        self.spec["mode"] = "apply"
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))
        self.assertTrue((path / "database.dump").exists())

    def admit(self, plan: dict[str, Any], review: str = "a" * 64) -> None:
        self.spec["record"]["incomplete_retirement"] = {
            "reviewed_plan_sha256": review,
            "host_binding_sha256": plan["binding_sha256"],
        }

    def setUp(self) -> None:
        import hashlib
        from pathlib import Path

        self.fixture = rehearsal.RehearsalHostSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.spec, self.root, _ = self.fixture.storage_fixture(
            {"database_bytes": 1, "filestore_bytes": 1, "staging_bytes": 1, "reserve_bytes": 1}
        )
        self.spec.update(
            action="retire-incomplete",
            mode="dry-run",
            resources_binding_sha256=self.fixture.spec["generation"]["resources_binding_sha256"],
            database_data_directory=self.fixture.spec["database_data_directory"],
        )
        self.spec["record"].update(state="prepared", source_odoo_version="fixture")
        self.spec["source_image"] = self.spec["target_image"]
        (self.root / ".execution.lock").touch()
        (self.root / "execute.started").touch()
        raw = self.root / "raw-source"
        raw.mkdir()
        (raw / "database.dump").write_bytes(b"disposable raw capture")
        (raw / "filestore.tar").write_bytes(b"disposable raw files")
        self.outside = self.fixture.fixture.root / "protected-production"
        self.outside.mkdir()
        (self.outside / "keep").write_bytes(b"production fixture")
        self.fixture.fixture.containers["web"]["State"]["Running"] = False
        self.calls: list[tuple[str, ...]] = []
        self.network = True
        self.original_docker = self.fixture.host["docker"]
        self.network_id = hashlib.sha256(b"isolated fixture network").hexdigest()
        self.fixture.host["docker"] = self.docker
        self.fixture.host["one_inspection"] = lambda name: __import__("json").loads(
            self.docker("inspect", name)
        )[0]
        # The fixture uses actual local backing directories with unique inodes.
        self.backings = {
            name: Path(volume["Mountpoint"])
            for name, volume in self.fixture.fixture.volumes.items()
        }

    def docker(self, *args: str, stdin: bytes | None = None) -> bytes:
        import json

        self.calls.append(args)
        containers = self.fixture.fixture.containers
        if args[:2] == ("inspect", "testing-data"):
            return json.dumps(
                [
                    {
                        "Id": self.network_id,
                        "Internal": True,
                        "Containers": {item["Id"]: {} for item in containers.values()},
                    }
                ]
            ).encode()
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.fixture.fixture.volumes).encode()
        if args[:2] == ("network", "ls"):
            return b"testing-data" if self.network else b""
        if args[0] == "stop":
            for identifier in args[1:]:
                next(item for item in containers.values() if item["Id"] == identifier)["State"][
                    "Running"
                ] = False
            return b""
        if args[0] == "rm":
            for identifier in args[1:]:
                name = next(name for name, item in containers.items() if item["Id"] == identifier)
                del containers[name]
            return b""
        if args[:2] == ("volume", "rm"):
            del self.fixture.fixture.volumes[args[2]]
            return b""
        if args[:2] == ("network", "rm"):
            self.assertEqual(args[2], self.network_id)
            self.network = False
            return b""
        return cast(bytes, self.original_docker(*args, stdin=stdin))

    def test_host_dry_run_apply_and_replay_dispose_only_its_own_custody(self) -> None:
        # A partial source snapshot may exist before its receipt and clone/switch.
        snapshot = self.root / self.fixture.host["database_names"](self.spec)["source"]
        snapshot.mkdir()
        (snapshot / "database.dump").write_bytes(b"partial snapshot")
        plan = self.fixture.run_host(self.spec)
        self.admit(plan)
        self.assertFalse(plan["disposed"])
        self.assertTrue((self.root / "raw-source/database.dump").exists())
        self.assertFalse(
            any(
                call[0] in {"stop", "rm"} or call[:2] in {("volume", "rm"), ("network", "rm")}
                for call in self.calls
            )
        )
        self.spec["mode"] = "apply"
        (self.root / "incomplete-retirement.pending").write_bytes(b"interrupted receipt")
        applied = self.fixture.run_host(self.spec)
        self.assertTrue(applied["disposed"])
        self.assertEqual(applied["binding_sha256"], plan["binding_sha256"])
        self.assertFalse(self.fixture.fixture.containers)
        self.assertFalse(self.fixture.fixture.volumes)
        self.assertFalse((self.root / "raw-source").exists())
        self.assertFalse(snapshot.exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"production fixture")
        self.spec["mode"] = "apply"
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])

    def test_emitted_retirement_wrapper_survives_provider_marker_parsing(self) -> None:
        import copy
        import io
        import subprocess
        import sys
        from pathlib import Path
        from contextlib import redirect_stdout
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
        from control_plane.odoo_upgrade_rehearsal import rehearsal_host_program

        scenario = IncompleteRehearsalRetirementTests()
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        operation = scenario.retirement()
        self.spec["record"]["intent_sha256"] = scenario.prepared.intent_sha256
        spec = {**copy.deepcopy(self.spec), "program": rehearsal_host_program()}

        def scheduler(record: Any, script: str, check: Any) -> dict[str, str]:
            output = io.StringIO()

            def command(args: list[str], **kwargs: Any) -> Any:
                self.assertEqual(args[0], "docker")
                return subprocess.CompletedProcess(
                    args, 0, stdout=self.docker(*args[1:]), stderr=b""
                )

            with (
                patch.object(sys, "argv", ["host"]),
                patch.object(Path, "home", return_value=self.fixture.fixture.root),
                patch.object(subprocess, "run", side_effect=command),
                redirect_stdout(output),
            ):
                exec(compile(rehearsal.python_heredoc(script), "<delivered-wrapper>", "exec"), {})
            return extract_odoo_post_deploy_readback_markers({"logs": output.getvalue()})

        with (
            patch("control_plane.odoo_upgrade_rehearsal.rehearsal_host_spec", return_value=spec),
            patch.object(operation.adapter, "run_script", side_effect=scheduler),
        ):
            result = operation.host("dry-run", lambda: None)
        self.assertTrue(result["qualified"])
        self.assertFalse(result["disposed"])
        self.assertTrue((self.root / "raw-source/database.dump").exists())
        self.assertEqual(operation.original(scenario.prepared).state, "reconcile_required")

    def test_host_refuses_unreviewed_inventory_without_deleting_custody(self) -> None:
        plan = self.fixture.run_host(self.spec)
        self.spec["record"]["incomplete_retirement"] = {
            "reviewed_plan_sha256": "a" * 64,
            "host_binding_sha256": plan["binding_sha256"],
        }
        self.fixture.fixture.containers["web"]["Id"] = "recreated-private-web"
        self.spec["mode"] = "apply"
        (self.root / "incomplete-retirement-refusals.pending").write_bytes(b"interrupted refusal")
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / "incomplete-retirement-refusals.json").is_file())
        self.assertFalse((self.root / "incomplete-retirement.json").exists())
        self.assertTrue((self.root / "raw-source/database.dump").exists())
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))

    def test_host_resumes_its_manifest_after_a_lost_volume_removal_reply(self) -> None:
        plan = self.fixture.run_host(self.spec)
        self.admit(plan)
        self.spec["mode"] = "apply"
        docker = self.fixture.host["docker"]
        failed = False

        def lose_reply(*args: str, stdin: bytes | None = None) -> bytes:
            nonlocal failed
            result = cast(bytes, docker(*args, stdin=stdin))
            if args[:2] == ("volume", "rm") and not failed:
                failed = True
                raise RuntimeError("Volume removed; reply lost")
            return result

        self.fixture.host["docker"] = lose_reply
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / "raw-source/database.dump").exists())
        self.spec["mode"] = "dry-run"
        resumed_plan = self.fixture.run_host(self.spec)
        self.assertEqual(resumed_plan["binding_sha256"], plan["binding_sha256"])
        self.assertTrue(resumed_plan["disposal_started"])
        admitted = self.spec["record"]["incomplete_retirement"]
        self.spec["record"]["incomplete_retirement"] = {
            **admitted,
            "reviewed_plan_sha256": "b" * 64,
        }
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.spec["record"]["incomplete_retirement"] = admitted
        self.spec["mode"] = "apply"
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])
        self.assertEqual((self.outside / "keep").read_bytes(), b"production fixture")

    def test_host_refuses_running_execute_and_shared_storage_before_disposal(self) -> None:
        import fcntl
        import copy

        with (self.root / ".execution.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.spec["mode"] = "apply"
            with self.assertRaises(BlockingIOError):
                self.fixture.run_host(self.spec)
        other = copy.deepcopy(next(iter(self.fixture.fixture.containers.values())))
        other["Id"] = "protected-production-container"
        other["Config"]["Labels"]["com.docker.compose.project"] = "production"
        self.fixture.fixture.containers[other["Id"]] = other
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / "raw-source/database.dump").exists())
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))

    def test_abandonment_preserves_both_tuples_and_original_marker(self) -> None:
        copies = self.later_phase()
        self.spec["action"] = "abandon-validation"
        self.spec["abandoned_attempt_sha256"] = canonical_json_sha256({"attempt": "old"})
        marker = self.root / (
            "validate-business-" + self.spec["abandoned_attempt_sha256"] + ".started"
        )
        marker.touch()
        plan = self.fixture.run_host(self.spec)
        self.assertTrue(plan["recovery_snapshots_preserved"])
        self.admit(plan)
        self.spec["mode"] = "apply"
        result = self.fixture.run_host(self.spec)
        self.assertTrue(result["disposed"])
        self.assertTrue(marker.is_file())
        self.assertFalse(self.fixture.fixture.containers)
        for copy in copies:
            self.assertTrue((self.root / copy["database"] / "database.dump").is_file())
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])

    def test_abandonment_refuses_changed_custody_or_any_validation_outcome(self) -> None:
        copies = self.later_phase()
        self.spec["action"] = "abandon-validation"
        self.spec["abandoned_attempt_sha256"] = "a" * 64
        marker = self.root / (
            "validate-business-" + self.spec["abandoned_attempt_sha256"] + ".started"
        )
        marker.touch()
        snapshot = self.root / copies[-1]["database"] / "database.dump"
        saved = snapshot.read_bytes()
        for changed in ("other-effect", "corrupt", "outcome", "pending", "exec-session"):
            with self.subTest(changed=changed):
                extra = self.root / {
                    "other-effect": "validate-business-" + "b" * 64 + ".started",
                    "outcome": marker.name.removesuffix(".started") + ".outcome.json",
                    "pending": marker.name.removesuffix(".started") + ".outcome.pending",
                }.get(changed, "unused")
                if changed in {"other-effect", "outcome", "pending"}:
                    extra.touch()
                elif changed == "corrupt":
                    snapshot.write_bytes(b"changed")
                else:
                    self.fixture.fixture.containers["script-runner"]["ExecIDs"] = ["active"]
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.fixture.run_host(self.spec)
                self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))
                extra.unlink(missing_ok=True)
                snapshot.write_bytes(saved)
                self.fixture.fixture.containers["script-runner"]["ExecIDs"] = None

    def test_rebuild_host_restores_final_without_capture_or_initial_upgrade(self) -> None:
        from unittest.mock import Mock

        copies = self.later_phase()
        self.spec.update(action="rebuild-final", mode="apply")
        self.spec["record"].update(
            state="rebuilding",
            abandoned_validations=[
                {
                    "host_disposal": {
                        "recovery_snapshots_preserved": True,
                        "recovery_snapshots": copies,
                    }
                }
            ],
        )
        host = self.fixture.host
        restore = Mock()
        upgrade = Mock()
        host.update(
            compose=Mock(),
            command=Mock(),
            sql=Mock(return_value="1"),
            restore_preserved_snapshot=restore,
            clone=Mock(),
            copy_runner=upgrade,
            before_web=Mock(),
            private_health=Mock(),
            rehearsal_storage_capacity=Mock(return_value=True),
            capture=Mock(side_effect=AssertionError("No source recapture")),
        )
        host["rehearsal_storage_capacity"].side_effect = [False, True]
        self.spec["record"]["abandoned_validations"][-1]["attempt_sha256"] = canonical_json_sha256(
            {"old": "attempt"}
        )
        refused = self.fixture.run_host(self.spec)
        self.assertEqual(refused["refusal_code"], "rehearsal_rebuild_capacity_insufficient")
        restore.assert_not_called()
        upgrade.assert_not_called()
        prepared_calls = host["compose"].call_count
        self.spec["attempt_sha256"] = canonical_json_sha256({"refused": "unsafe-stage"})
        prepared = self.root / (
            "rebuild-prepared-"
            + self.spec["record"]["abandoned_validations"][-1]["attempt_sha256"]
            + ".json"
        )
        saved = prepared.read_bytes()
        unknown = self.root / (
            "rebuild-final-" + canonical_json_sha256({"unknown": "attempt"}) + ".started"
        )
        snapshot = self.root / copies[0]["database"] / "database.dump"
        snapshot_bytes = snapshot.read_bytes()
        native_inspection = host["inspection"]
        capacity = host["rehearsal_storage_capacity"]
        for unsafe in (
            "active",
            "unknown-final",
            "binding",
            "unknown-bootstrap",
            "snapshot",
            "inspection",
            "capacity",
        ):
            with self.subTest(unsafe=unsafe):
                if unsafe == "active":
                    self.fixture.fixture.containers["script-runner"]["ExecIDs"] = ["active"]
                elif unsafe == "unknown-final":
                    unknown.touch()
                elif unsafe == "binding":
                    prepared.write_bytes(b"{}")
                elif unsafe == "unknown-bootstrap":
                    prepared.unlink()
                elif unsafe == "snapshot":
                    snapshot.write_bytes(b"changed")
                elif unsafe == "inspection":
                    host["inspection"] = Mock(side_effect=RuntimeError("restarting"))
                else:
                    host["rehearsal_storage_capacity"] = Mock(
                        side_effect=RuntimeError("unavailable")
                    )
                self.spec["attempt_sha256"] = canonical_json_sha256({"refused": unsafe})
                if unsafe in {"active", "binding", "snapshot", "inspection", "capacity"}:
                    refused = self.fixture.run_host(self.spec)
                    self.assertEqual(refused["refusal_code"], "rehearsal_rebuild_precheck_refused")
                    self.spec["mode"] = "observe"
                    self.assertEqual(
                        self.fixture.run_host(self.spec)["refusal_code"], refused["refusal_code"]
                    )
                    self.spec["mode"] = "apply"
                else:
                    with self.assertRaises(RuntimeError):
                        self.fixture.run_host(self.spec)
                restore.assert_not_called()
                upgrade.assert_not_called()
                self.fixture.fixture.containers["script-runner"]["ExecIDs"] = None
                unknown.unlink(missing_ok=True)
                prepared.write_bytes(saved)
                prepared.chmod(0o600)
                snapshot.write_bytes(snapshot_bytes)
                host["inspection"] = native_inspection
                host["rehearsal_storage_capacity"] = capacity
        self.spec["attempt_sha256"] = canonical_json_sha256({"new": "reviewed-key"})
        result = self.fixture.run_host(self.spec)
        self.assertEqual(host["compose"].call_count, prepared_calls + 1)
        self.assertEqual(result["state"], "switched")
        self.assertEqual(result["copies"], copies)
        names = host["database_names"](self.spec)
        self.assertEqual(
            [call.args[3:] for call in restore.call_args_list],
            [(names["source"], names["source"]), (names["final_source"], names["final_source"])],
        )
        upgrade.assert_called_once()
        self.assertEqual(upgrade.call_args.args[2], names["final_upgrade"])
        self.fixture.fixture.containers["web"]["State"]["Running"] = True
        self.spec["mode"] = "observe"
        host["sql"] = lambda containers, statement, database="postgres": host["witness"](
            self.spec, database
        )
        observed = self.fixture.run_host(self.spec)
        self.assertEqual(observed["state"], "switched")
        for copy in copies:
            self.assertTrue((self.root / copy["database"] / "database.dump").is_file())


class IncompleteRetirementHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_director_route_plans_applies_replays_and_reads_original_retirement(
        self,
    ) -> None:
        from unittest.mock import PropertyMock
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.service_auth import (
            configured_local_operator_identity,
            LaunchplaneAuthzPolicy,
        )
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement
        from tests.http_app_test_support import _local_operator_bearer_config
        from tests.support.http import request

        fixture = rehearsal.RehearsalScenario()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        identity = configured_local_operator_identity(
            _local_operator_bearer_config(token_label="local-owner-write")
        )
        assert identity is not None
        fixture.principal = idempotency_scope(identity)
        prepared = fixture.record.model_copy(
            update={
                "principal": fixture.principal,
                "state": "prepared",
                "compose_id": "own-compose",
                "project_id": "own-project",
                "environment_id": "own-environment",
            }
        )
        fixture.store.compare_and_write_odoo_upgrade_rehearsal(fixture.record, prepared)
        prepare_rehearsal_local_credentials(
            fixture.store, fixture.identifier, actor=fixture.principal
        )
        fixture.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="retirement-policy",
                source="test",
                updated_at=prepared.updated_at.isoformat(),
                policy=LaunchplaneAuthzPolicy(schema_version=2),
            )
        )
        with patch.object(OdooRehearsalAdapter, "host", side_effect=TimeoutError("held execute")):
            with self.assertRaises(TimeoutError):
                fixture.run_operation(fixture.adapter("execute"), "original-execute")
        before = fixture.authority()
        native = native_fixture(prepared, "original-execute", "c" * 64)
        headers = {
            "Authorization": "Bearer local-operator-token",
            "Idempotency-Key": "original-execute",
        }
        payload = fixture.request.model_copy(update={"action": "retire-incomplete"}).model_dump(
            mode="json"
        )
        route = ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product)
        # Exercise the real retirement adapter at the provider boundary. A
        # successful schedule status alone never proves a host qualification.
        import json
        import click

        held = fixture.store.list_held_provider_target_reservations()
        for evidence in (
            {"log_available": "false"},
            {"log_available": "true"},
            {"rehearsal_result": "not-hex"},
            {"rehearsal_result": b"not-json".hex()},
            {"rehearsal_result": json.dumps({"qualified": True}).encode().hex()},
        ):
            with (
                self.subTest(evidence=evidence),
                patch.object(IncompleteRehearsalRetirement, "placement", return_value=False),
                patch.object(OdooRehearsalAdapter, "run_script", return_value=evidence),
            ):
                refused = await request(
                    fixture.app, "POST", route, payload=payload, headers=headers
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertTrue(refused.json()["trace_id"])
                from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalPrerequisite

                self.assertEqual(
                    refused.json()["error"]["code"], RehearsalPrerequisite.STATE_UNVERIFIED.value
                )
                self.assertEqual(
                    fixture.store.read_odoo_upgrade_rehearsal(fixture.identifier), prepared
                )
                self.assertEqual(fixture.store.list_held_provider_target_reservations(), held)
        with (
            patch.object(IncompleteRehearsalRetirement, "placement", return_value=False),
            patch(
                "control_plane.dokploy.api.validate_dokploy_schedule_script",
                side_effect=click.ClickException("Script cannot be materialized"),
            ),
        ):
            refused = await request(fixture.app, "POST", route, payload=payload, headers=headers)
            self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(fixture.store.list_held_provider_target_reservations(), held)
        self.assertEqual(fixture.store.read_odoo_upgrade_rehearsal(fixture.identifier), prepared)
        with (
            patch.object(
                IncompleteRehearsalRetirement,
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ) as host,
            patch.object(IncompleteRehearsalRetirement, "placement", return_value=True),
        ):
            plan = await request(fixture.app, "POST", route, payload=payload, headers=headers)
            self.assertEqual(plan.status_code, 202, plan.text)
            payload.update(mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"])
            from control_plane.provider_operations import run_durable_provider_operation

            operation = IncompleteRehearsalRetirement(
                fixture.adapter("retire-incomplete"), "original-execute"
            )
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": payload["reviewed_plan_sha256"]}
            )

            def run() -> Any:
                return run_durable_provider_operation(
                    store=fixture.store,
                    scope=operation.reservation_scope(),
                    route_path=route,
                    idempotency_key="original-execute",
                    request_fingerprint=operation.request_fingerprint(),
                    lease_owner="retirement-http-worker",
                    response_trace_id="retirement-http-trace",
                    adapter=operation,
                )

            from control_plane.contracts.odoo_upgrade_rehearsal import (
                IncompleteRehearsalRetirementCheckpoint,
            )
            from control_plane.contracts.canonical_json import canonical_json_sha256

            admitted = prepared.model_copy(
                update={
                    "incomplete_retirement": IncompleteRehearsalRetirementCheckpoint(
                        original_key_sha256=canonical_json_sha256(
                            {"operation_key": "original-execute"}
                        ),
                        reviewed_plan_sha256=str(payload["reviewed_plan_sha256"]),
                        host_binding_sha256=str(native["binding_sha256"]),
                    )
                }
            )
            fixture.store.compare_and_write_odoo_upgrade_rehearsal(prepared, admitted)
            with patch.object(
                OdooRehearsalAdapter,
                "native_readback",
                return_value={
                    "completed": False,
                    "host_observation_unavailable": True,
                    "host_observation_reason": "provider_transport_unavailable",
                },
            ):
                original_readback = await request(
                    fixture.app,
                    "POST",
                    route,
                    payload={
                        **payload,
                        "action": "execute",
                        "mode": "readback",
                        "reviewed_plan_sha256": "",
                    },
                    headers=headers,
                )
            self.assertEqual(original_readback.status_code, 202, original_readback.text)
            self.assertFalse(original_readback.json()["result"]["completed"])
            self.assertNotIn("resources_preserved", original_readback.json()["result"])
            fixture.store.compare_and_write_odoo_upgrade_rehearsal(admitted, prepared)

            outcome, replay_outcome = run(), run()
            # The real local journal/adapter ran above. Bypass only HTTP's
            # PostgreSQL admission while mapping those completed outcomes.
            with (
                patch.object(
                    PostgresRecordStore,
                    "database_dialect_name",
                    new_callable=PropertyMock,
                    return_value="postgresql",
                ),
                patch(
                    "control_plane.http_app.run_durable_provider_operation",
                    side_effect=[outcome, replay_outcome],
                ) as route_runner,
            ):
                applied = await request(
                    fixture.app, "POST", route, payload=payload, headers=headers
                )
                self.assertEqual(applied.status_code, 202, applied.text)
                self.assertTrue(applied.json()["result"]["original_operation_released"])
                replay = await request(fixture.app, "POST", route, payload=payload, headers=headers)
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertTrue(replay.json()["replayed"])
                routed = route_runner.call_args.kwargs
                self.assertEqual(routed["scope"], operation.reservation_scope())
                self.assertNotEqual(routed["scope"], fixture.principal)
                self.assertIsNotNone(
                    fixture.store.read_idempotency_record(
                        scope=routed["scope"],
                        route_path=route,
                        idempotency_key=headers["Idempotency-Key"],
                    )
                )
                self.assertEqual(routed["request_fingerprint"], operation.request_fingerprint())
                self.assertEqual(routed["idempotency_key"], headers["Idempotency-Key"])
                self.assertNotEqual(routed["adapter"].target_key(), fixture.identifier)
            calls = host.call_count
            payload.update(mode="readback", reviewed_plan_sha256="")
            readback = await request(fixture.app, "POST", route, payload=payload, headers=headers)
            self.assertEqual(readback.status_code, 202, readback.text)
            self.assertEqual(readback.json()["result"]["state"], "retired")
            self.assertEqual(readback.json()["result"]["copies"], [])
            self.assertEqual(host.call_count, calls)
        self.assertEqual(fixture.authority(), before)
        self.assertFalse(fixture.store.list_held_provider_target_reservations())
