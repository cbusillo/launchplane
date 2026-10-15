"""Storage admission and receipt recovery without live provider effects."""

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.odoo_testing_drill import (
    DRILL_SCOPE,
    DRILL_ROUTE,
    DrillPrerequisite,
    DrillSkipped,
    drill_receipt,
    lane_key,
)
from control_plane.odoo_testing_storage import prepare_drill_storage, storage_receipt
from tests.test_odoo_testing_drill import TestingDrillScenario


class TestingStorageTests(TestingDrillScenario):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.seed(f"sqlite+pysqlite:///{root / 'state.db'}")
        self.scheduler.run_once()
        worker = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="storage-fixture",
            claimed_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            lease_expires_at=datetime.fromtimestamp(self.now + 300, timezone.utc).isoformat(),
        )
        assert worker is not None
        self.worker = worker

    def prepare(self, execute: Any = None) -> None:
        def guarded(
            root: Path, target: str, spec: dict[str, object], dispatch: Any
        ) -> dict[str, object]:
            dispatch(lambda: None)
            return cast(dict[str, object], execute(root, target, spec))

        prepare_drill_storage(
            self.store,
            self.worker,
            Path("."),
            execute=guarded if execute is not None else None,
            now=self.now,
            checkpoint=lambda: None,
        )

    def old_receipt(self, suffix: str = "old") -> Any:
        planned = self.gen.model_copy(
            update={
                "discard_drill_database": "lp_drill_" + "c" * 32,
                "discard_copy_binding": "a" * 64,
            }
        )
        receipt = drill_receipt(
            lane_key(self.profile.product, self.context) + ":" + suffix,
            self.now - 1,
            {
                "planned_generation": planned.model_dump(mode="json"),
                "status": "pass",
                "disposal_status": "deferred",
            },
        )
        self.store.write_idempotency_record(receipt)
        return receipt

    def read_slot(self) -> Any:
        return self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=self.worker.idempotency_key
        )

    def test_insufficient_current_capacity_keeps_both_selection_and_previous_copy(self) -> None:
        with self.assertRaisesRegex(DrillPrerequisite, "testing_storage_capacity_insufficient"):
            self.prepare(Mock(return_value={"capacity": False, "disposals": []}))
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="instance", context_name=self.context, instance_name="testing"
            )[0],
            self.runtime,
        )
        self.assertEqual(self.read_slot().response_payload["storage_status"], "insufficient")
        with self.assertRaisesRegex(DrillPrerequisite, "testing_storage_capacity_unverified"):
            self.store.activate_odoo_testing_drill(self.worker, self.now)

    def test_budget_absence_and_changed_authority_never_select_a_new_database(self) -> None:
        profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"odoo_testing_drill_storage": None})
                    if lane.instance == "testing"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(profile)
        execute = Mock()
        with self.assertRaisesRegex(DrillPrerequisite, "testing_storage_budget_required"):
            self.prepare(execute)
        execute.assert_not_called()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_deferred_disposal_recovers_without_erasing_original_identity(self) -> None:
        original = self.old_receipt()
        with self.assertRaisesRegex(DrillPrerequisite, "testing_disposal_deferred"):
            self.prepare(Mock(return_value={"capacity": True, "disposals": ["deferred"]}))
        self.assertEqual(self.evidence().disposal_pending, 1)
        self.scheduler.run_once()
        self.assertEqual(
            self.notifications.call_args.args[2]["failure_step"], "testing_disposal_deferred"
        )
        self.prepare(Mock(return_value={"capacity": True, "disposals": ["disposed"]}))
        self.assertEqual(self.evidence().disposal_pending, 0)
        recovered = self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=original.idempotency_key
        )
        assert recovered is not None
        self.assertEqual(
            recovered.response_payload["planned_generation"],
            original.response_payload["planned_generation"],
        )
        self.assertEqual(recovered.response_payload["disposal_status"], "disposed")
        self.store.activate_odoo_testing_drill(self.worker, self.now)

    def test_unknown_transport_keeps_original_receipt_set_on_retry(self) -> None:
        original = self.old_receipt()
        with self.assertRaises(TimeoutError):
            self.prepare(Mock(side_effect=TimeoutError))
        intent = storage_receipt(self.store, self.worker.operation_id)
        assert intent is not None
        self.assertEqual(intent.response_payload["phase"], "dispatching")
        self.old_receipt("later")
        execute = Mock(return_value={"capacity": True, "disposals": ["disposed"]})
        with self.assertRaisesRegex(DrillPrerequisite, "testing_disposal_deferred"):
            self.prepare(execute)
        self.assertEqual(len(execute.call_args.args[2]["storage_maintenance"]["copies"]), 1)
        reconciled = storage_receipt(self.store, self.worker.operation_id)
        assert reconciled is not None
        self.assertEqual(reconciled.response_payload["receipts"], [original.idempotency_key])

    def test_current_and_immediate_predecessor_are_not_cleanup_candidates(self) -> None:
        receipt = self.old_receipt()
        current = self.gen.model_copy(
            update={
                "retained_drill_database": "lp_drill_" + "c" * 32,
                "retained_copy_binding": "a" * 64,
            }
        )
        from control_plane.storage.postgres import LaunchplaneFreshOdooTestingGenerationRow

        with self.store._session_factory() as session:
            row = session.get(
                LaunchplaneFreshOdooTestingGenerationRow, (self.profile.product, self.context)
            )
            assert row is not None
            row.payload = current.model_dump(mode="json")
            session.commit()
        from tests.support.disposable_lanes import record_safety

        record_safety(
            self.store,
            self.profile.product,
            self.context,
            "testing",
            observed_at=datetime.fromtimestamp(self.now, timezone.utc),
        )
        self.prepare()
        self.assertEqual(self.evidence().disposal_attention, 1)
        copies = self.storage_executor.call_args.args[2]["storage_maintenance"]["copies"]
        self.assertEqual(copies, [])
        self.assertEqual(
            self.store.list_odoo_testing_disposal_receipts(
                self.profile.product, self.context, include_retained=True
            )[0].idempotency_key,
            receipt.idempotency_key,
        )

    def test_capacity_evidence_is_bound_to_authority_and_worker_attempt(self) -> None:
        self.prepare()
        profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_testing_drill_storage": OdooTestingDrillStorageBudget(
                                database_bytes=200,
                                filestore_bytes=200,
                                staging_bytes=200,
                                reserve_bytes=200,
                            )
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(profile)
        with self.assertRaisesRegex(DrillPrerequisite, "testing_storage_capacity_unverified"):
            self.store.activate_odoo_testing_drill(self.worker, self.now)

    def test_use_report_before_locked_maintenance_refuses_provider_dispatch(self) -> None:
        from tests.support.disposable_lanes import record_safety

        record_safety(self.store, self.profile.product, self.context, "testing", usage="in_use")
        execute = Mock()
        with self.assertRaises(DrillSkipped):
            self.prepare(execute)
        execute.assert_not_called()

    def test_hold_arriving_during_handshake_is_checked_before_dispatch(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        sent = Mock()

        def execute(
            root: Path, target_id: str, spec: dict[str, object], dispatch: Any
        ) -> dict[str, object]:
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            self.store.write_dokploy_target_record(
                target.model_copy(
                    update={
                        "policies": target.policies.model_copy(
                            update={
                                "staff_testing_hold": DokployTargetStaffTestingHold(
                                    reason="staff arrived",
                                    recorded_by="fixture",
                                    recorded_at=self.profile.updated_at,
                                )
                            }
                        )
                    }
                )
            )
            dispatch(sent)
            return {"capacity": True, "disposals": []}

        with self.assertRaises(DrillSkipped):
            prepare_drill_storage(
                self.store,
                self.worker,
                Path("."),
                execute=execute,
                now=self.now,
                checkpoint=lambda: None,
            )
        sent.assert_not_called()


class HostCapacityTests(unittest.TestCase):
    def test_shared_backing_demands_are_summed_and_unknown_overlay_is_refused(self) -> None:
        namespace: dict[str, Any] = {"Path": Path, "os": os, "json": json}
        tree = ast.parse(STORAGE_MAINTENANCE_LIBRARY)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "storage_capacity"
        )
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<capacity>", "exec"), namespace)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for suffix in ("-data", "-db", "-staging"):
                (root / suffix).mkdir()
            # Explicit fake volume paths; actual filesystem device identity groups them.
            namespace["docker"] = lambda *arguments: json.dumps(
                [{"Mountpoint": str(root / ("-db" if arguments[-1].endswith("-db") else "-data"))}]
            ).encode()
            spec = {"generation": {"namespace": "fixture", "source_mode": "container-export"}}
            containers = {
                "script-runner": (
                    "runner",
                    {"GraphDriver": {"Data": {"UpperDir": str(root / "-staging")}}},
                )
            }
            budget = {
                "database_bytes": 40,
                "filestore_bytes": 40,
                "staging_bytes": 40,
                "reserve_bytes": 1,
            }
            with patch.object(
                os, "statvfs", return_value=SimpleNamespace(f_bavail=100, f_frsize=1)
            ):
                self.assertFalse(namespace["storage_capacity"](spec, containers, budget))
                budget["staging_bytes"] = 10
                self.assertTrue(namespace["storage_capacity"](spec, containers, budget))
                spec["generation"]["source_mode"] = "ssh"
                namespace["tempfile"] = SimpleNamespace(gettempdir=lambda: root / "-staging")
                self.assertFalse(namespace["storage_capacity"](spec, containers, budget))
                containers["script-runner"][1]["GraphDriver"] = {}
                with self.assertRaises(RuntimeError):
                    namespace["storage_capacity"](spec, containers, budget)
