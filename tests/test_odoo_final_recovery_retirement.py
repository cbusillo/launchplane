import json
import unittest
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.odoo_upgrade_rehearsal import (
    OdooUpgradeRehearsalRequest,
    RehearsalPrerequisite,
)
from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter, RehearsalRefused
from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement

from tests import test_odoo_incomplete_rehearsal_retirement as retirement


class FinalRecoveryRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.later = retirement.LaterPhaseRetirementTests()
        self.later.setUp()
        self.addCleanup(self.later.doCleanups)
        self.fixture = self.later.fixture
        self.store = self.fixture.store
        self.copies = (*self.later.switched.copies, self.fixture.copy("final_upgrade"))

        def host(adapter: Any, record: Any, mode: str, check: Any, **_: Any) -> Any:
            check()
            return {
                "intent_sha256": record.intent_sha256,
                "state": "verified" if adapter.request.action == "verify" else "retired",
                "copies": [copy.model_dump(mode="json") for copy in self.copies],
                "elapsed_seconds": record.elapsed_seconds,
                "resources_preserved": True,
                "public_route": False,
                "business_host_frozen": False,
            }

        with patch.object(OdooRehearsalAdapter, "host", autospec=True, side_effect=host):
            self.fixture.run_operation(self.fixture.adapter("verify"), "verified-final")
            self.preserving = self.fixture.adapter("retire")
            self.preserved = self.fixture.run_operation(self.preserving, "preserving-retire")
        record = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert record is not None
        self.record = record
        self.confirmation = {
            "evidence": "https://example.invalid/consumer/accepted-finals",
            "preserving_retire_key": "preserving-retire",
        }

    def operation(self, key: str = "free-initial") -> Any:
        adapter = self.fixture.adapter("retire")
        adapter.request = OdooUpgradeRehearsalRequest.model_validate(
            {**adapter.request.model_dump(mode="json"), "consumer_confirmation": self.confirmation}
        )
        return IncompleteRehearsalRetirement(adapter, key)

    def native(self) -> dict[str, object]:
        result = retirement.native_fixture(self.record, "free-initial", "e" * 64)
        result["recovery_snapshots"] = [self.copies[0].model_dump(mode="json")]
        result["retained_snapshots"] = [copy.model_dump(mode="json") for copy in self.copies[1:]]
        return result

    def run_retirement(self, operation: Any) -> Any:
        return retirement.IncompleteRehearsalRetirementTests.run_retirement(
            cast(Any, self), operation
        )

    def host(self, mode: str, check: Any, **kwargs: Any) -> dict[str, object]:
        disposed = mode != "dry-run" or kwargs.get("record") is not None
        return {
            **self.native(),
            **{
                key: disposed
                for key in (
                    "disposed",
                    "containers_absent",
                    "volumes_absent",
                    "network_absent",
                    "raw_source_absent",
                )
            },
        }

    def test_new_cleanup_operation_keeps_old_preserving_journal_and_all_metadata(self) -> None:
        original = self.later.journal("preserving-retire")
        operation = self.operation()
        with (
            patch.object(type(operation), "host", side_effect=self.host) as host,
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            outcome = self.run_retirement(operation)
            self.assertEqual(outcome.status, "completed")
            result = outcome.response_payload["result"]
            self.assertFalse(result["resources_preserved"])
            self.assertEqual(
                result["host_disposal"]["retained_snapshots"], self.native()["retained_snapshots"]
            )
            self.assertEqual(operation.readback(), result)
            self.assertEqual(host.call_args.kwargs["record"].rehearsal_id, self.record.rehearsal_id)
            calls = host.call_count
            self.assertEqual(self.run_retirement(operation).status, "replayed")
            self.assertEqual(host.call_count, calls)
        self.assertEqual(self.later.journal("preserving-retire"), original)
        current = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        for field in ("copies", "business_validations", "elapsed_seconds", "abandoned_validations"):
            self.assertEqual(getattr(current, field), getattr(self.record, field))
        replay = self.fixture.run_operation(self.preserving, "preserving-retire")
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(replay.response_payload, self.preserved.response_payload)
        replay_result = replay.response_payload["result"]
        assert isinstance(replay_result, dict)
        self.assertTrue(replay_result["resources_preserved"])

    def test_independent_readback_refuses_changed_final_custody(self) -> None:
        operation = self.operation()

        def host(mode: str, check: Any, **kwargs: Any) -> dict[str, object]:
            if kwargs.get("record") is not None:
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            return self.host(mode, check, **kwargs)

        with (
            patch.object(type(operation), "host", side_effect=host),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            self.assertEqual(self.run_retirement(operation).status, "completed")
            with self.assertRaises(RehearsalRefused):
                operation.readback()

    def test_confirmation_is_bound_to_review_without_changing_legacy_intent(self) -> None:
        operation = self.operation()
        self.assertEqual(operation.adapter.request.intent(), self.preserving.request.intent())
        legacy = self.preserving.request.model_dump(mode="json")
        self.assertNotIn("consumer_confirmation", legacy)
        self.assertEqual(
            OdooUpgradeRehearsalRequest.model_validate(
                {**legacy, "consumer_confirmation": None}
            ).model_dump(mode="json"),
            legacy,
        )
        with (
            patch.object(type(operation), "host", side_effect=self.later.host(self.native())),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            changed = self.operation()
            changed.adapter.request = OdooUpgradeRehearsalRequest.model_validate(
                {
                    **changed.adapter.request.model_dump(mode="json"),
                    "reviewed_plan_sha256": plan["plan_sha256"],
                    "consumer_confirmation": {
                        **self.confirmation,
                        "evidence": "https://example.invalid/other",
                    },
                }
            )
            with self.assertRaises(RehearsalRefused):
                self.run_retirement(changed)
        current = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        self.assertIsNone(current.incomplete_retirement)

    def test_preserving_key_cannot_be_reinterpreted_as_cleanup(self) -> None:
        operation = self.operation("preserving-retire")
        with patch.object(type(operation), "host") as host:
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()

    def test_started_cleanup_requires_its_new_original_key(self) -> None:
        operation = self.operation()

        def lose_reply(mode: str, check: Any, **kwargs: Any) -> dict[str, object]:
            if mode == "apply":
                raise TimeoutError("cleanup outcome unknown")
            return self.host(mode, check, **kwargs)

        with (
            patch.object(type(operation), "host", side_effect=lose_reply),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            with self.assertRaises(TimeoutError):
                self.run_retirement(operation)
            with self.assertRaises(RehearsalRefused):
                self.operation("replacement-key").plan()
        with (
            patch.object(type(operation), "host", side_effect=self.host),
            patch.object(type(operation), "placement", return_value=True),
        ):
            self.assertEqual(self.run_retirement(operation).status, "completed")

    def test_other_actions_cannot_use_final_consumer_confirmation(self) -> None:
        with self.assertRaises(ValueError):
            OdooUpgradeRehearsalRequest.model_validate(
                {
                    **self.preserving.request.model_dump(mode="json"),
                    "action": "retire-incomplete",
                    "consumer_confirmation": self.confirmation,
                }
            )

    def test_retained_readback_cannot_mask_a_changed_host_binding(self) -> None:
        operation = self.operation()
        with (
            patch.object(type(operation), "host", side_effect=self.host),
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            self.assertEqual(self.run_retirement(operation).status, "completed")
        current = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        with self.assertRaises(RehearsalRefused):
            operation.adapter.host_script(
                current.model_copy(update={"server_id": "another-server"}), "dry-run"
            )


class FinalRecoveryRetirementHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = retirement.IncompleteRetirementHostTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.spec = self.native.spec
        self.root = self.native.root
        copies = self.native.later_phase()
        copies.append(self.native.recovery_snapshot("final_upgrade"))
        self.spec.update(action="retire", retain_final_recovery=True)
        self.spec["record"].update(state="retired", copies=copies)
        for container in self.native.fixture.fixture.containers.values():
            container["State"]["Running"] = False
        (self.root / "retire.started").touch()
        self.retire_receipt = self.root / "retire.json"
        self.retire_receipt.write_text(
            json.dumps(
                {
                    "intent_sha256": self.spec["record"]["intent_sha256"],
                    "state": "retired",
                    "copies": copies,
                }
            )
        )
        self.copies = copies

    def run_host(self) -> dict[str, Any]:
        return self.native.fixture.run_host(self.spec)

    def test_reclaims_only_initial_bytes_and_owned_working_resources(self) -> None:
        metadata = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        plan = self.run_host()
        self.assertEqual(plan["recovery_snapshots"], self.copies[:1])
        self.assertEqual(plan["retained_snapshots"], self.copies[1:])
        self.native.admit(plan)
        self.spec["mode"] = "apply"
        result = self.run_host()
        self.assertTrue(result["disposed"])
        self.assertFalse(self.native.fixture.fixture.containers)
        self.assertFalse(self.native.fixture.fixture.volumes)
        self.assertFalse((self.root / self.copies[0]["database"]).exists())
        for copy in self.copies[1:]:
            self.assertTrue((self.root / copy["database"] / "database.dump").is_file())
            self.assertTrue((self.root / copy["database"] / "filestore.tar").is_file())
        for name, contents in metadata.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertEqual(self.run_host(), result)
        self.assertEqual((self.native.outside / "keep").read_bytes(), b"production fixture")

    def test_incomplete_retirement_cannot_adopt_a_verified_or_retired_final_tuple(self) -> None:
        self.spec["action"] = "retire-incomplete"
        for state in ("verified", "retired"):
            with self.subTest(state=state):
                self.spec["record"]["state"] = state
                with self.assertRaises(RuntimeError):
                    self.run_host()
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.native.calls))

    def test_an_uncertain_effect_refuses_before_deletion(self) -> None:
        (self.root / "validate-business-unsettled.started").touch()
        with self.assertRaises(RuntimeError):
            self.run_host()
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.native.calls))

    def test_readonly_mount_of_retained_custody_refuses_before_deletion(self) -> None:
        self.native.fixture.fixture.containers["web"]["Mounts"].append(
            {"Source": str(self.root / self.copies[-1]["database"]), "RW": False}
        )
        with self.assertRaises(RuntimeError):
            self.run_host()
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.native.calls))

    def test_aliased_final_directory_refuses_before_deletion(self) -> None:
        final = self.root / self.copies[-1]["database"]
        saved = self.native.outside / "final-recovery"
        final.rename(saved)
        final.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.run_host()
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.native.calls))
        self.assertTrue((saved / "database.dump").is_file())

    def test_resumes_after_source_deletion_reply_is_lost(self) -> None:
        plan = self.run_host()
        self.native.admit(plan)
        self.spec["mode"] = "apply"
        shutil = self.native.fixture.host["shutil"]
        remove = shutil.rmtree

        def lost_reply(path: Any, **kwargs: Any) -> None:
            remove(path, **kwargs)
            if path.name == self.copies[0]["database"]:
                raise RuntimeError("lost source deletion reply")

        with patch.object(shutil, "rmtree", side_effect=lost_reply):
            with self.assertRaises(RuntimeError):
                self.run_host()
        result = self.run_host()
        self.assertTrue(result["disposed"])
        self.assertEqual(result["retained_snapshots"], self.copies[1:])

    def test_another_attempt_cannot_adopt_started_cleanup(self) -> None:
        plan = self.run_host()
        self.native.admit(plan)
        self.spec["mode"] = "apply"
        original = self.native.fixture.host["docker"]

        def lose_reply(*args: str, **kwargs: Any) -> bytes:
            result = original(*args, **kwargs)
            if args[0] == "rm":
                raise RuntimeError("lost deletion reply")
            return cast(bytes, result)

        self.native.fixture.host["docker"] = lose_reply
        with self.assertRaises(RuntimeError):
            self.run_host()
        self.native.fixture.host["docker"] = original
        attempt = self.spec["attempt_sha256"]
        self.spec["attempt_sha256"] = "b" * len(attempt)
        with self.assertRaises(RuntimeError):
            self.run_host()
        for copy in self.copies:
            self.assertTrue((self.root / copy["database"] / "database.dump").is_file())
        self.spec["attempt_sha256"] = attempt
        self.assertTrue(self.run_host()["disposed"])
