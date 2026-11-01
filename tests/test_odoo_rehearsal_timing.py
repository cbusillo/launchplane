from datetime import datetime, timedelta, timezone
from typing import Any, cast
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from control_plane.contracts.odoo_rehearsal_timing import (
    RehearsalPhaseTiming,
    measured_rehearsal_window,
)
from tests import test_odoo_upgrade_rehearsal as rehearsal_tests
from tests import test_odoo_rehearsal_business_validation as business_tests
from control_plane.contracts.odoo_rehearsal_business_validation import BusinessValidationReceipt
from control_plane.contracts.odoo_rehearsal_timing import REHEARSAL_TIMING_MARKER
from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRecord
from control_plane.odoo_upgrade_rehearsal import RehearsalRefused


class RehearsalTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.started = datetime.now(timezone.utc)

    def timing(self, action: str, attempt: str, observed: dict[str, int]) -> dict[str, Any]:
        return {
            "action": action,
            "attempt_sha256": attempt * 64,
            "observed": {
                key: (self.started + timedelta(seconds=value)).isoformat()
                for key, value in observed.items()
            },
        }

    def test_legacy_serialization_and_binding_survive_optional_measurements(self) -> None:
        record = self.fixture.record
        legacy = record.model_dump(mode="json")
        self.assertNotIn("phase_timings", legacy)
        timing = RehearsalPhaseTiming.model_validate(
            self.timing("simulate-final", "a", {"simulated_quiescent": 0})
        )
        measured = record.model_copy(update={"phase_timings": (timing,)})
        self.assertEqual(measured.binding_sha256, record.binding_sha256)
        self.assertEqual(type(record).model_validate(legacy).model_dump(mode="json"), legacy)
        self.assertIsNone(measured_rehearsal_window(record)["upper_bound_seconds"])

    def test_invalid_utc_is_refused_but_clock_steps_remain_observable(self) -> None:
        payload = self.timing("simulate-final", "a", {"simulated_quiescent": 3, "before_web": 1})
        self.assertTrue(RehearsalPhaseTiming.model_validate(payload).observed)
        for stamp in ("2026-01-01T12:00:00", "2026-01-01T12:00:00+01:00", "unknown"):
            with self.subTest(stamp=stamp), self.assertRaises(ValidationError):
                RehearsalPhaseTiming.model_validate({**payload, "observed": {"before_web": stamp}})

    def test_evidence_only_readback_preserves_original_timing_without_matching_the_callers_key(
        self,
    ) -> None:
        import json
        from unittest.mock import patch

        record = self.fixture.record.model_copy(
            update={
                "state": "upgraded",
                "compose_id": "own-compose",
                "copies": (self.fixture.copy("source"),),
            }
        )
        adapter = self.fixture.adapter("execute")
        timing = self.timing("execute", "a", {"source_captured": 0})
        receipt = {
            "intent_sha256": record.intent_sha256,
            "state": record.state,
            "copies": [copy.model_dump(mode="json") for copy in record.copies],
            "elapsed_seconds": {},
            "resources_preserved": True,
            "public_route": False,
            "business_host_frozen": False,
            "phase_timing": timing,
        }
        for key in ("", "earlier-capacity-refusal"):
            with (
                self.subTest(key=key),
                patch.object(
                    adapter,
                    "run_script",
                    return_value={"rehearsal_result": json.dumps(receipt).encode().hex()},
                ),
                patch.object(
                    adapter,
                    "operation_readback",
                    return_value={
                        "_provider_operation_key": "another-operation",
                        "operation_state": "completed",
                    },
                ),
            ):
                observed = adapter.native_readback(
                    record, idempotency_key=key, checkpoint=lambda: None
                )
                self.assertTrue(observed["completed"])
                self.assertEqual(
                    cast(dict[str, Any], observed["phase_timing"])["attempt_sha256"],
                    timing["attempt_sha256"],
                )
        with patch.object(
            adapter,
            "run_script",
            return_value={"rehearsal_result": json.dumps(receipt).encode().hex()},
        ):
            with self.assertRaises(RehearsalRefused):
                adapter.host(record, "observe", lambda: None, operation_key="another-operation")

    def test_rebuild_never_inherits_a_freeze_start_from_an_earlier_attempt(self) -> None:
        record = self.measured_record()
        self.assertIsNotNone(measured_rehearsal_window(record)["upper_bound_seconds"])
        simulation, business = record.phase_timings
        inherited = simulation.model_copy(
            update={"action": "rebuild-final", "attempt_sha256": "b" * 64}
        )
        record = record.model_copy(update={"phase_timings": (simulation, inherited, business)})
        window = measured_rehearsal_window(record)
        self.assertIsNone(window["upper_bound_seconds"])
        self.assertTrue(window["reasons"])

    def measured_record(self) -> OdooUpgradeRehearsalRecord:
        source = self.fixture.copy("final_source").model_copy(
            update={"captured_at": self.started + timedelta(seconds=1)}
        )
        simulation = RehearsalPhaseTiming.model_validate(
            {
                **self.timing(
                    "simulate-final",
                    "a",
                    {
                        "simulated_quiescent": 0,
                        "sql_sanitized": 2,
                        "module_update_completed": 3,
                        "before_web": 4,
                        "web_started": 5,
                        "private_switch_verified": 6,
                    },
                ),
                "final_source_witness_sha256": source.witness_sha256,
            }
        )
        business = RehearsalPhaseTiming.model_validate(
            self.timing("validate-business", "c", {"product_started": 10, "receipt_observed": 12})
        )
        accepted = BusinessValidationReceipt.model_validate(
            business_tests.receipt(
                {
                    **business_tests.identity(self.fixture.record),
                    "attempt_sha256": business.attempt_sha256,
                }
            )
        )
        record = self.fixture.record.model_copy(
            update={
                "copies": (source,),
                "phase_timings": (simulation, business),
                "business_validations": (accepted,),
            }
        )
        return record

    def test_one_lineage_bounds_resume_and_itemizes_dispatch_without_inventing_pre_switch_proof(
        self,
    ) -> None:
        record = self.measured_record()
        simulation, business = record.phase_timings
        accepted = record.business_validations[0]
        measured = measured_rehearsal_window(record)
        self.assertEqual(measured["lower_bound_seconds"], 10)
        self.assertEqual(measured["upper_bound_seconds"], 12)
        self.assertEqual(measured["dispatch_gap_seconds"], 4)
        self.assertTrue(measured["unavailable"])
        for changed in (
            record.model_copy(
                update={
                    "business_validations": (
                        accepted.model_copy(update={"attempt_sha256": "d" * 64}),
                        accepted,
                    )
                }
            ),
            record.model_copy(
                update={"business_validations": (accepted.model_copy(update={"passed": False}),)}
            ),
            record.model_copy(
                update={
                    "phase_timings": (
                        simulation.model_copy(
                            update={
                                "observed": {
                                    **simulation.observed,
                                    "web_started": self.started.isoformat(),
                                }
                            }
                        ),
                        business,
                    )
                }
            ),
        ):
            with self.subTest(changed=changed.phase_timings):
                self.assertIsNone(measured_rehearsal_window(changed)["upper_bound_seconds"])

    def test_complete_window_requires_same_candidate_t4_business_proof_before_switch(self) -> None:
        record = self.measured_record()
        simulation, business = record.phase_timings
        validation = record.business_validations[0].model_copy(
            update={"attempt_sha256": simulation.attempt_sha256}
        )
        pre_switch = (self.started + timedelta(seconds=4.5)).isoformat()
        timing = RehearsalPhaseTiming.model_validate(
            {
                **simulation.model_dump(mode="json"),
                "pre_switch_validation": validation.model_dump(mode="json"),
                "observed": {**simulation.observed, "pre_switch_verified": pre_switch},
            }
        )
        measured = record.model_copy(update={"phase_timings": (timing, business)})
        window = measured_rehearsal_window(measured)
        self.assertTrue(window["complete"])
        self.assertFalse(window["unavailable"])
        self.assertEqual(window["upper_bound_seconds"], 12)
        for changes in (
            {"pre_switch_validation": validation.model_copy(update={"artifact_id": "other"})},
            {"pre_switch_validation": None},
            {
                "observed": {
                    **timing.observed,
                    "pre_switch_verified": business.observed["receipt_observed"],
                }
            },
        ):
            with self.subTest(changes=changes):
                changed = measured.model_copy(
                    update={"phase_timings": (timing.model_copy(update=changes), business)}
                )
                self.assertFalse(measured_rehearsal_window(changed)["complete"])

    def test_pre_switch_receipt_requires_its_own_attempt_and_passing_commit(self) -> None:
        record = self.measured_record()
        simulation = record.phase_timings[0]
        base = {
            **simulation.model_dump(mode="json"),
            "observed": {**simulation.observed, "pre_switch_verified": self.started.isoformat()},
        }
        validation = record.business_validations[0].model_copy(
            update={"attempt_sha256": simulation.attempt_sha256}
        )
        for changes in ({"passed": False}, {"attempt_sha256": "d" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RehearsalPhaseTiming.model_validate(
                    {
                        **base,
                        "pre_switch_validation": validation.model_copy(update=changes).model_dump(
                            mode="json"
                        ),
                    }
                )

    def test_publication_retains_t4_under_original_attempt_and_rejects_another_candidate(
        self,
    ) -> None:
        measured = self.measured_record()
        simulation = measured.phase_timings[0]
        validation = measured.business_validations[0].model_copy(
            update={"attempt_sha256": simulation.attempt_sha256}
        )
        timing = {
            **simulation.model_dump(mode="json"),
            "observed": {
                **simulation.observed,
                "pre_switch_verified": (self.started + timedelta(seconds=4.5)).isoformat(),
            },
            "pre_switch_validation": validation.model_dump(mode="json"),
        }
        result: dict[str, Any] = {
            "state": "switched",
            "copies": [item.model_dump(mode="json") for item in measured.copies],
            "elapsed_seconds": {},
            "phase_timing": timing,
        }
        adapter = self.fixture.adapter("simulate-final")
        adapter.publish(self.fixture.record, result)
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        self.assertEqual(current.phase_timings[0].pre_switch_validation, validation)
        self.assertFalse(current.business_validations)
        adapter.publish(current, result)
        repeated = self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert repeated is not None
        self.assertEqual(repeated.phase_timings, current.phase_timings)
        for field, changed in (
            ("artifact_id", "other"),
            ("source_commit", "f" * 40),
            ("product", "other"),
        ):
            with self.subTest(field=field), self.assertRaises(RehearsalRefused):
                adapter.publish(
                    current,
                    {
                        **result,
                        "phase_timing": {
                            **timing,
                            "pre_switch_validation": {
                                **validation.model_dump(mode="json"),
                                field: changed,
                            },
                        },
                    },
                )
        from control_plane._odoo_rehearsal_business_validation import business_validation_history
        from control_plane.contracts.canonical_json import canonical_json_sha256

        current = repeated
        final = self.fixture.copy("final_upgrade")
        verification: dict[str, Any] = {
            "state": "verified",
            "copies": [item.model_dump(mode="json") for item in (*current.copies, final)],
            "elapsed_seconds": {},
        }
        verifier = self.fixture.adapter("verify")
        with self.assertRaises(RehearsalRefused):
            verifier.publish(current, verification)
        history = business_validation_history(current.model_dump(mode="json"))
        verification["copies"][-1]["business_validation_sha256"] = canonical_json_sha256(history)
        with self.assertRaises(RehearsalRefused):
            verifier.publish(current, verification)
        validated = rehearsal_tests.with_passing_business_validation(current)
        self.fixture.store.compare_and_write_odoo_upgrade_rehearsal(current, validated)
        history = business_validation_history(validated.model_dump(mode="json"))
        verification["copies"][-1]["business_validation_sha256"] = canonical_json_sha256(history)
        self.assertEqual(verifier.publish(validated, verification)["state"], "verified")


class HostTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalHostSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host = self.fixture.host

    def test_first_sql_boundary_precedes_module_update_despite_post_hook_sanitization(self) -> None:
        started = datetime.now(timezone.utc)
        first, module, second = [
            (started + timedelta(seconds=index)).isoformat() for index in range(3)
        ]
        spec: dict[str, Any] = {
            "action": "rebuild-final",
            "attempt_sha256": "a" * 64,
            "record": {"source_odoo_version": "1.0"},
            "workflow": {},
            "target_image": "candidate",
            "versions": {"candidate": "2.0"},
            "upgrade_runner": "native",
            "readback_spec": "policy",
            "readback_program": "readback",
        }
        self.host["witness"] = lambda *args: "witness"
        self.host["docker"] = Mock(
            return_value=(
                "testing_copy_sanitized=true\ntesting_copy_pre_registry_sanitized=true\n"
                "odoo_module_update_completed=true\nodoo_module_update_modules_configured=true\n"
                f"{REHEARSAL_TIMING_MARKER}sql_sanitized={first}\n"
                f"{REHEARSAL_TIMING_MARKER}module_update_completed={module}\n"
                f"{REHEARSAL_TIMING_MARKER}sql_sanitized={second}\n"
            ).encode()
        )
        self.host["copy_runner"](spec, {"script-runner": ("runner", {})}, "copy", fresh=False)
        observed = spec["phase_timing"]["observed"]
        self.assertEqual(observed["sql_sanitized"], first)
        self.assertEqual(observed["module_update_completed"], module)

    def test_before_web_timestamp_requires_readback_and_precedes_start(self) -> None:
        spec: dict[str, Any] = {
            "action": "simulate-final",
            "attempt_sha256": "a" * 64,
            "readback_spec": "policy",
            "readback_program": "readback",
            "web_gate_path": "/gate",
        }
        calls = []

        def docker(*args: str, **kwargs: Any) -> bytes:
            calls.append(args)
            if args[0] == "start":
                self.assertIn("before_web", spec["phase_timing"]["observed"])
                self.assertNotIn("web_started", spec["phase_timing"]["observed"])
            return b"integration_readback_ok=true\n"

        self.host["docker"] = docker
        containers: dict[str, Any] = {"script-runner": ("runner", {}), "web": ("web", {})}
        self.host["before_web"](spec, containers, "copy")
        self.assertIn("web_started", spec["phase_timing"]["observed"])
        refused = {key: value for key, value in spec.items() if key != "phase_timing"}
        blocked = Mock(return_value=b"integration_readback_ok=false\n")
        self.host["docker"] = blocked
        with self.assertRaises(RuntimeError):
            self.host["before_web"](refused, containers, "copy")
        self.assertNotIn("phase_timing", refused)
        self.assertEqual(blocked.call_count, 1)

    def test_simulation_saves_its_actual_freeze_and_snapshot_lineage_before_a_lost_reply(
        self,
    ) -> None:
        spec, _, containers = self.fixture.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        spec["action"] = "simulate-final"
        spec["record"]["state"] = "upgraded"
        self.host["inspection"] = Mock(return_value=containers)
        self.host["validate_snapshots"] = Mock()
        self.host["rehearsal_storage_capacity"] = Mock(return_value=True)
        self.host["migration_copy_supported"] = Mock(return_value=True)
        self.host["restore_preserved_snapshot"] = Mock()
        self.host["sql"] = Mock()
        self.host["docker"] = Mock()
        self.host["snapshot"] = Mock(
            return_value={"database": "final_source", "witness_sha256": "f" * 64}
        )
        self.host["upgrade_final"] = Mock()
        self.host["private_health"] = Mock()
        result = self.fixture.run_host(spec)
        timing = result["phase_timing"]
        self.assertEqual(timing["attempt_sha256"], spec["attempt_sha256"])
        self.assertEqual(
            timing["final_source_witness_sha256"], result["copies"][-1]["witness_sha256"]
        )
        self.assertTrue(datetime.fromisoformat(timing["observed"]["simulated_quiescent"]).tzinfo)
        observed = self.fixture.run_host({**spec, "mode": "observe"})
        self.assertEqual(observed, result)
        self.assertEqual(self.host["restore_preserved_snapshot"].call_count, 1)
        self.assertEqual(self.host["upgrade_final"].call_count, 1)
