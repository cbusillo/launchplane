"""Fixed product invocation preserves isolation, original custody and value-free evidence."""

import json
import unittest
from typing import Any, cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane._odoo_rehearsal_business_validation import (
    BUSINESS_VALIDATION_ENTRYPOINT,
    BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS,
    BUSINESS_VALIDATION_RECEIPT_MAX_BYTES,
    BUSINESS_ENTRYPOINT_CHECK_LIBRARY,
    final_business_database,
    business_entrypoint_check_script,
)
from control_plane.contracts.odoo_rehearsal_business_validation import BusinessValidationReceipt
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_upgrade_rehearsal import (
    OdooUpgradeRehearsalRequest,
    RehearsalPrerequisite,
)
from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter, RehearsalRefused
from tests import test_odoo_upgrade_rehearsal as rehearsal_tests


def identity(record: Any) -> dict[str, str]:
    return {
        "product": record.product,
        "rehearsal_id": record.rehearsal_id,
        "artifact_id": record.target_artifact.artifact_id,
        "source_commit": record.target_artifact.source_commit,
        "database": final_business_database(record.rehearsal_id),
        "attempt_sha256": canonical_json_sha256({"operation_key": "original"}),
    }


def receipt(bound: dict[str, str]) -> dict[str, Any]:
    return {
        **bound,
        "passed": True,
        "committed": True,
        "checks": [
            {
                "key": "delivered",
                "passed": True,
                "records": [
                    {"model": "sale.order", "id": 31},
                    {"model": "stock.picking", "id": 32},
                    {"model": "stock.move", "id": 33},
                ],
            }
        ],
    }


class BusinessValidationContractTests(unittest.TestCase):
    def test_receipt_bounds_and_success_requirements(self) -> None:
        bound = {
            "product": "fixture",
            "rehearsal_id": "lp-rehearsal-" + "a" * 32,
            "artifact_id": "candidate",
            "source_commit": "b" * 40,
            "database": "lp_rehearsal_" + "a" * 32 + "_final_upgrade",
            "attempt_sha256": "c" * 64,
        }
        valid = receipt(bound)
        cases: tuple[dict[str, Any], ...] = (
            {"committed": False},
            {"passed": "true"},
            {"checks": []},
            {"traceback": "private"},
            {"checks": [{"key": "delivered", "passed": False, "records": []}]},
            {"checks": valid["checks"] * 2},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                BusinessValidationReceipt.model_validate({**valid, **changes})
        # Generic product models and more than 100 identified records are valid within the byte budget.
        valid["checks"][0]["records"] = [{"model": "repair.order", "id": n} for n in range(1, 102)]
        self.assertTrue(BusinessValidationReceipt.model_validate(valid).passed)
        while (
            len(json.dumps(valid, separators=(",", ":"), ensure_ascii=True).encode())
            <= BUSINESS_VALIDATION_RECEIPT_MAX_BYTES
        ):
            valid["checks"][0]["records"] *= 2
        with self.assertRaises(ValidationError):
            BusinessValidationReceipt.model_validate(valid)


class BusinessEntrypointPreflightTests(unittest.TestCase):
    def test_probe_distinguishes_product_and_provider_failure_and_cleans_its_container(
        self,
    ) -> None:
        import subprocess
        from pathlib import Path
        from types import SimpleNamespace

        namespace: dict[str, Any] = {}
        exec(BUSINESS_ENTRYPOINT_CHECK_LIBRARY, namespace)
        for outcome in (
            "available",
            "missing",
            "python",
            "docker",
            "pull",
            "timeout",
            "empty-cid",
            "bad-cid",
        ):
            calls: list[list[str]] = []

            def run(arguments: list[str], **kwargs: Any) -> Any:
                calls.append(arguments)
                self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
                self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
                if arguments[1:3] == ["image", "inspect"]:
                    return SimpleNamespace(returncode=1 if outcome == "pull" else 0)
                if arguments[1] == "pull":
                    return SimpleNamespace(returncode=125)
                if arguments[1] == "run":
                    self.assertIn("--rm", arguments)
                    self.assertNotIn("--name", arguments)
                    self.assertEqual(arguments[arguments.index("--network") + 1], "none")
                    self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
                    scratch = arguments[arguments.index("--tmpfs") + 1]
                    self.assertTrue(scratch.startswith("/tmp:"))
                    self.assertTrue(
                        {"noexec", "nosuid", "nodev"} <= set(scratch.split(":", 1)[1].split(","))
                    )
                    self.assertTrue(
                        any(option.startswith("size=") for option in scratch.split(","))
                    )
                    self.assertTrue(
                        {"--read-only", "--cap-drop", "--security-opt"} <= set(arguments)
                    )
                    self.assertFalse(
                        {"-e", "--env", "--env-file", "-v", "--volume", "--mount"} & set(arguments)
                    )
                    self.assertIn(BUSINESS_VALIDATION_ENTRYPOINT, arguments)
                    self.assertIn(spec["image"], arguments)
                    if outcome != "empty-cid":
                        Path(arguments[arguments.index("--cidfile") + 1]).write_text(
                            "bad" if outcome == "bad-cid" else "a" * 64
                        )
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired("fixed check", 1)
                    return SimpleNamespace(
                        returncode={"missing": 2, "python": 127, "docker": 125}.get(outcome, 0)
                    )
                self.assertEqual(arguments[1:], ["rm", "-f", "a" * 64])
                return SimpleNamespace(returncode=0)

            spec: dict[str, Any] = {
                "image": "fixture@sha256:" + "b" * 64,
                "entrypoint": BUSINESS_VALIDATION_ENTRYPOINT,
                "binding": {"attempt": "fixture"},
            }
            with self.subTest(outcome=outcome), patch.object(subprocess, "run", side_effect=run):
                result = namespace["check_business_entrypoint"](spec)
            expected = (
                "available"
                if outcome == "available"
                else "entrypoint_unavailable"
                if outcome in {"missing", "python"}
                else "provider_unavailable"
            )
            self.assertEqual(result, {**spec["binding"], "status": expected})
            removals = [call for call in calls if call[1] == "rm"]
            self.assertEqual(bool(removals), outcome == "timeout")

    def test_probe_uses_one_deadline_across_image_pull_and_invocation(self) -> None:
        import subprocess
        import time
        from types import SimpleNamespace

        namespace: dict[str, Any] = {}
        exec(BUSINESS_ENTRYPOINT_CHECK_LIBRARY, namespace)
        limit = BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS
        elapsed = 0.0

        def advance(arguments: list[str], **kwargs: Any) -> Any:
            nonlocal elapsed
            if arguments[1] == "pull":
                elapsed += limit
                return SimpleNamespace(returncode=0)
            elapsed += limit / 3
            return SimpleNamespace(returncode=1)

        with (
            patch.object(time, "monotonic", side_effect=lambda: elapsed),
            patch.object(subprocess, "run", side_effect=advance) as run,
        ):
            result = namespace["check_business_entrypoint"](
                {"image": "fixture", "entrypoint": BUSINESS_VALIDATION_ENTRYPOINT, "binding": {}}
            )
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertLess(
            run.call_args_list[-1].kwargs["timeout"], run.call_args_list[0].kwargs["timeout"]
        )
        self.assertFalse(any(call.args[0][1] == "run" for call in run.call_args_list))

    def test_rejected_probe_precedes_allocation_and_a_new_key_can_retry(self) -> None:
        from control_plane import odoo_upgrade_rehearsal as rehearsal_module
        from control_plane.dokploy import post_deploy

        implementation = OdooRehearsalAdapter.check_business_entrypoint
        fixture = rehearsal_tests.RehearsalScenario()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = fixture.adapter("prepare")
        binding: dict[str, str] = {}
        renderer = business_entrypoint_check_script

        def render(image: str, expected: dict[str, str]) -> str:
            binding.clear()
            binding.update(expected)
            return renderer(image, expected)

        outcome = "entrypoint_unavailable"

        def schedule(**kwargs: Any) -> dict[str, str]:
            self.assertEqual(kwargs["schedule_lookup_id"], fixture.record.server_id)
            return {"rehearsal_result": json.dumps({**binding, "status": outcome}).encode().hex()}

        with (
            patch.object(
                adapter,
                "check_business_entrypoint",
                side_effect=lambda record, key, check: implementation(adapter, record, key, check),
            ),
            patch.object(rehearsal_module, "business_entrypoint_check_script", side_effect=render),
            patch(
                "control_plane.odoo_upgrade_rehearsal.api.find_matching_dokploy_schedule",
                return_value=None,
            ),
            patch.object(post_deploy, "run_manual_data_schedule", side_effect=schedule) as probe,
            patch(
                "control_plane.testing_local_credentials.prepare_rehearsal_local_credentials"
            ) as credentials,
            patch("control_plane.dokploy.sandbox_placement.create_isolated_placement") as placement,
            patch.object(adapter, "run_script") as storage,
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
        ):
            for outcome in ("entrypoint_unavailable", "provider_unavailable"):
                with self.subTest(outcome=outcome), self.assertRaises(RehearsalRefused):
                    fixture.run_operation(adapter, outcome)
                with self.assertRaises(RehearsalRefused) as readback:
                    adapter.prepare_readback(fixture.record, outcome)
                self.assertEqual(
                    readback.exception.code,
                    RehearsalPrerequisite.OPERATION_UNAVAILABLE,
                )
                credentials.assert_not_called()
                placement.assert_not_called()
                storage.assert_not_called()
                self.assertFalse(fixture.store.list_held_provider_target_reservations())
                self.assertEqual(
                    fixture.store.read_odoo_upgrade_rehearsal(fixture.identifier), fixture.record
                )
            self.assertEqual(probe.call_count, 2)
            outcome = "available"
            credentials.side_effect = RuntimeError("next allocation stage")
            with self.assertRaisesRegex(RuntimeError, "next allocation stage"):
                fixture.run_operation(adapter, "available")
            credentials.assert_called_once()
            placement.assert_not_called()
            storage.assert_not_called()

    def test_interrupted_preflight_recovers_only_empty_idle_original(self) -> None:
        fixture = rehearsal_tests.RehearsalScenario()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = fixture.adapter("prepare")
        with (
            patch.object(adapter, "check_business_entrypoint", side_effect=SystemExit),
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch(
                "control_plane.odoo_upgrade_rehearsal.api.find_matching_dokploy_schedule",
                return_value=None,
            ) as schedule,
            patch.object(adapter, "host") as host,
        ):
            with self.assertRaises(SystemExit):
                fixture.run_operation(adapter, "interrupted")
            self.assertEqual(adapter.observe("original", "", "").outcome, "absent")
            readback = adapter.prepare_readback(fixture.record, "interrupted")
            self.assertEqual(readback["recovery_disposition"], "continue_original_prepare")
            from control_plane.dokploy.post_deploy import DOKPLOY_RUNNING_DEPLOYMENT_STATUSES

            schedule.return_value = {
                "deployments": [{"status": next(iter(DOKPLOY_RUNNING_DEPLOYMENT_STATUSES))}]
            }
            self.assertEqual(adapter.observe("original", "", "").outcome, "unknown")
            schedule.return_value = None
            with patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("unexpected", "", ""),
            ):
                self.assertEqual(adapter.observe("original", "", "").outcome, "unknown")
            host.assert_not_called()
            with (
                patch.object(adapter, "check_business_entrypoint"),
                patch(
                    "control_plane.testing_local_credentials.prepare_rehearsal_local_credentials",
                    side_effect=RuntimeError("retry reached allocation"),
                ) as credentials,
                self.assertRaisesRegex(RuntimeError, "retry reached allocation"),
            ):
                fixture.run_operation(adapter, "interrupted")
            credentials.assert_called_once()

    def test_probe_receipt_must_match_its_candidate_and_attempt(self) -> None:
        from control_plane.dokploy import post_deploy

        implementation = OdooRehearsalAdapter.check_business_entrypoint
        fixture = rehearsal_tests.RehearsalScenario()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = fixture.adapter("prepare")
        binding = {
            "intent_sha256": fixture.record.intent_sha256,
            "record_binding_sha256": fixture.record.binding_sha256,
            "attempt_sha256": canonical_json_sha256({"operation_key": "original"}),
            "image": fixture.record.target_artifact.image.repository
            + "@"
            + fixture.record.target_artifact.image.digest,
        }
        for field in (*binding, "extra"):
            payload = {**binding, "status": "available", field: "wrong"}
            with (
                self.subTest(field=field),
                patch(
                    "control_plane.odoo_upgrade_rehearsal.api.find_matching_dokploy_schedule",
                    return_value=None,
                ),
                patch.object(
                    post_deploy,
                    "run_manual_data_schedule",
                    return_value={"rehearsal_result": json.dumps(payload).encode().hex()},
                ),
                self.assertRaises(RehearsalRefused),
            ):
                implementation(adapter, fixture.record, "original", lambda: None)


class BusinessValidationAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.record
        self.record = old.model_copy(
            update={
                "state": "switched",
                "compose_id": "own-compose",
                "copies": (self.fixture.copy("source"), self.fixture.copy("final_source")),
            }
        )
        self.fixture.store.compare_and_write_odoo_upgrade_rehearsal(old, self.record)
        self.request = OdooUpgradeRehearsalRequest.model_validate(
            {**self.fixture.request.model_dump(mode="json"), "action": "validate-business"}
        )
        self.plan = identity(self.record)
        self.adapter = OdooRehearsalAdapter(
            self.fixture.root,
            self.fixture.store,
            self.fixture.product,
            self.request,
            self.fixture.principal,
            "fixture-trace",
        )

    def test_plan_binds_copy_candidate_without_a_business_payload(self) -> None:
        self.assertEqual(self.request.intent(), self.fixture.request.intent())
        self.assertEqual(self.adapter.inputs()[0], self.record)
        with self.assertRaises(ValidationError):
            OdooUpgradeRehearsalRequest.model_validate(
                {**self.request.model_dump(mode="json"), "business_validation": {"steps": []}}
            )

    def test_apply_and_original_key_observation_retain_sanitized_evidence_and_snapshot_lineage(
        self,
    ) -> None:
        authority = self.fixture.authority()
        self.adapter.request = self.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.adapter.reviewed_binding(self.record),
            }
        )
        validation = BusinessValidationReceipt.model_validate(receipt(self.plan))
        result: dict[str, Any] = {
            "intent_sha256": self.record.intent_sha256,
            "state": "switched",
            "copies": [item.model_dump(mode="json") for item in self.record.copies],
            "elapsed_seconds": {},
            "resources_preserved": True,
            "public_route": False,
            "business_host_frozen": False,
            "business_validation": validation.model_dump(mode="json"),
        }
        with patch.object(OdooRehearsalAdapter, "host", return_value=result):
            applied = self.fixture.run_operation(self.adapter, "business-key")
        self.assertEqual(applied.status, "completed")
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.record.rehearsal_id)
        assert current is not None
        self.assertEqual(current.business_validations, (validation,))
        self.assertEqual(self.fixture.authority(), authority)
        self.assertEqual(
            self.adapter.operation_readback(current, "business-key")["operation_state"], "completed"
        )
        with self.assertRaises(RehearsalRefused):
            self.adapter.operation_readback(current, "other-key")
        # A committed failed scenario stays identified when a corrected one passes.
        failed = validation.model_copy(
            update={
                "passed": False,
                "attempt_sha256": canonical_json_sha256({"operation_key": "failed"}),
            }
        )
        self.adapter.publish(
            current, {**result, "business_validation": failed.model_dump(mode="json")}
        )
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.record.rehearsal_id)
        assert current is not None
        with self.assertRaises(RehearsalRefused):
            self.fixture.adapter("verify").inputs()
        corrected = validation.model_copy(
            update={"attempt_sha256": canonical_json_sha256({"operation_key": "corrected"})}
        )
        self.adapter.publish(
            current, {**result, "business_validation": corrected.model_dump(mode="json")}
        )
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.record.rehearsal_id)
        assert current is not None
        self.assertEqual(current.business_validations, (validation, failed, corrected))
        lineage = canonical_json_sha256(
            [item.model_dump(mode="json") for item in current.business_validations]
        )
        verify = self.fixture.adapter("verify")
        final = self.fixture.copy("final_upgrade")
        snapshot = {
            **result,
            "state": "verified",
            "copies": [*result["copies"], final.model_dump(mode="json")],
        }
        with self.assertRaises(RehearsalRefused):
            verify.publish(current, snapshot)
        snapshot["copies"][-1]["business_validation_sha256"] = lineage
        published = verify.publish(current, snapshot)
        self.assertEqual(
            cast(list[dict[str, Any]], published["copies"])[-1]["business_validation_sha256"],
            lineage,
        )

    def test_pending_evidence_is_private_bound_and_cannot_publish_before_verified(self) -> None:
        evidence_receipt = receipt(self.plan)
        pending = {
            "intent_sha256": self.record.intent_sha256,
            "record_binding_sha256": self.record.binding_sha256,
            "attempt_sha256": evidence_receipt["attempt_sha256"],
            "receipt": evidence_receipt,
        }
        result = {
            "intent_sha256": self.record.intent_sha256,
            "state": "switched",
            "copies": [copy.model_dump(mode="json") for copy in self.record.copies],
            "elapsed_seconds": {},
            "resources_preserved": True,
            "public_route": False,
            "business_host_frozen": False,
            "completed": False,
            "pending_business_validation": pending,
            "business_validation_postcheck_verified": False,
        }

        def read(payload: dict[str, Any], key: str = "original") -> dict[str, object]:
            evidence = {"rehearsal_result": json.dumps(payload).encode().hex()}
            with patch.object(OdooRehearsalAdapter, "run_script", return_value=evidence):
                return self.adapter.host(self.record, "observe", lambda: None, operation_key=key)

        observed = read(result)
        self.assertTrue(observed["diagnostics_private"])
        with self.assertRaises(RehearsalRefused):
            self.adapter.publish(self.record, observed)
        with self.assertRaises(RehearsalRefused):
            read(result, "another-key")
        for field in ("intent_sha256", "record_binding_sha256", "attempt_sha256"):
            with self.subTest(field=field), self.assertRaises(RehearsalRefused):
                read({**result, "pending_business_validation": {**pending, field: "e" * 64}})
        with patch.object(OdooRehearsalAdapter, "host", return_value=observed):
            self.assertEqual(
                self.adapter.observe("original", "host", "reconcile").outcome, "unknown"
            )
        completed = {
            key: value
            for key, value in result.items()
            if key not in {"pending_business_validation", "business_validation_postcheck_verified"}
        }
        completed.update(completed=True, business_validation=evidence_receipt)
        with patch.object(OdooRehearsalAdapter, "host", return_value=read(completed)):
            self.assertEqual(
                self.adapter.observe("original", "host", "reconcile").outcome, "present"
            )
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.record.rehearsal_id)
        assert current is not None
        self.assertEqual(current.business_validations[0].checks[0].records[0].id, 31)

    def test_stale_review_cannot_dispatch_product_code(self) -> None:
        from control_plane.provider_operations import (
            ProviderMutationRejectedError,
            ProviderOperationLease,
        )

        self.adapter.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": "f" * 64}
        )
        with (
            patch.object(OdooRehearsalAdapter, "host") as host,
            self.assertRaises(ProviderMutationRejectedError),
        ):
            self.adapter.apply("original", Mock(spec=ProviderOperationLease))
        host.assert_not_called()


class BusinessValidationHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalHostSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host = self.fixture.host
        self.spec, self.root, self.containers = self.fixture.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        self.plan = {
            "product": "fixture",
            "rehearsal_id": self.fixture.identifier,
            "artifact_id": "fixture-candidate",
            "source_commit": "b" * 40,
            "database": self.fixture.identifier.replace("-", "_") + "_final_upgrade",
            "attempt_sha256": self.spec["attempt_sha256"],
        }
        self.spec.update(action="validate-business", business_validation_identity=self.plan)
        self.spec["record"].update(
            target_artifact={"source_commit": self.plan["source_commit"]}, source_odoo_version="1.0"
        )
        self.host["inspection"] = lambda *args: self.containers
        self.host["private_health"] = Mock()
        self.host["sql"] = lambda *args: self.host["witness"](self.spec, self.plan["database"])
        self.spec["readback_spec"] = "fixed-policy"
        self.spec["readback_program"] = "fixed-readback"
        self.executions = 0

        def docker(*args: str, stdin: bytes | None = None) -> bytes:
            if stdin == b"fixed-readback":
                return b"integration_readback_ok=true\n"
            raise AssertionError("Product calls must discard child diagnostics")

        self.host["docker"] = docker

        def product_command(arguments: list[str], *, stdin: bytes | None = None) -> bytes:
            self.executions += 1
            self.last_command = arguments
            supplied = json.loads(stdin or b"{}")
            return (
                b"business_validation_result="
                + json.dumps(receipt(supplied)).encode().hex().encode()
                + b"\n"
            )

        self.native_product_command = self.host["product_command"]
        self.host["product_command"] = product_command

    def test_native_entrypoint_retains_original_receipt_and_never_replays_a_lost_reply(
        self,
    ) -> None:
        result = self.fixture.run_host(self.spec)
        self.assertTrue(result["business_validation"]["passed"])
        self.assertEqual(result["business_validation"]["database"], self.plan["database"])
        self.assertEqual(self.executions, 1)
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.spec["mode"] = "observe"
        observed = self.fixture.run_host(self.spec)
        self.assertEqual(observed, result)
        self.assertEqual(self.executions, 1)
        self.spec["attempt_sha256"] = canonical_json_sha256({"operation_key": "different"})
        missing = self.fixture.run_host(self.spec)
        self.assertFalse(missing["completed"])
        self.assertEqual(self.executions, 1)

    def test_committed_pending_outcome_survives_failed_postcheck_and_recovers_without_replay(
        self,
    ) -> None:
        precheck = self.host["business_validation_precheck"]
        calls = 0

        def fail_after_write(*args: Any) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("isolation unverified")
            precheck(*args)

        with patch.dict(self.host, {"business_validation_precheck": fail_after_write}):
            with self.assertRaises(RuntimeError):
                self.fixture.run_host(self.spec)
            pending = self.fixture.run_host({**self.spec, "mode": "observe"})
        self.assertFalse(pending["completed"])
        self.assertFalse(pending["business_validation_postcheck_verified"])
        self.assertEqual(
            pending["pending_business_validation"]["receipt"]["checks"][0]["records"][0]["id"], 31
        )
        self.assertEqual(self.executions, 1)
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(
                {**self.spec, "mode": "observe", "record_binding_sha256": "e" * 64}
            )
        wrong = self.fixture.run_host({**self.spec, "mode": "observe", "attempt_sha256": "f" * 64})
        self.assertNotIn("pending_business_validation", wrong)
        completed = self.fixture.run_host({**self.spec, "mode": "observe"})
        self.assertTrue(completed["business_validation"]["committed"])
        self.assertEqual(self.executions, 1)
        self.assertEqual(self.fixture.run_host({**self.spec, "mode": "observe"}), completed)

    def test_host_snapshot_seals_complete_history_and_refuses_failed_latest_before_capture(
        self,
    ) -> None:
        self.spec["versions"] = {self.spec["target_image"]: "19.0"}
        self.host["sql"] = lambda *args: "19.0"
        (self.root / "capture.json").write_text("{}")
        history = [
            {"passed": True, "steps": [{"key": "order", "record_ids": [31], "passed": True}]}
        ]
        self.spec["record"]["business_validations"] = history

        def capture(args: list[str], path: Any, bound: int) -> None:
            path.write_bytes(b"snapshot fixture")

        self.host["bounded_capture"] = Mock(side_effect=capture)
        result = self.host["snapshot"](self.spec, self.root, self.containers, self.plan["database"])
        self.assertEqual(result["business_validation_sha256"], canonical_json_sha256(history))
        import shutil

        shutil.rmtree(self.root / self.plan["database"])
        history.append({"passed": False})
        self.host["bounded_capture"].reset_mock()
        with self.assertRaises(RuntimeError):
            self.host["snapshot"](self.spec, self.root, self.containers, self.plan["database"])
        self.host["bounded_capture"].assert_not_called()
        self.assertFalse((self.root / self.plan["database"]).exists())

    def test_changed_witness_or_connection_isolation_refuses_before_business_writes(self) -> None:
        for failure in ("witness", "connections", "runtime"):
            with self.subTest(failure=failure):
                spec = {**self.spec, "attempt_sha256": canonical_json_sha256({"failure": failure})}
                with (
                    patch.dict(
                        self.host,
                        {
                            "sql": (lambda *args: "wrong")
                            if failure == "witness"
                            else self.host["sql"],
                            "docker": (lambda *args, **kwargs: b"unverified")
                            if failure == "connections"
                            else self.host["docker"],
                            "inspection": Mock(side_effect=RuntimeError("public network"))
                            if failure == "runtime"
                            else self.host["inspection"],
                        },
                    ),
                ):
                    refused = self.fixture.run_host(spec)
                self.assertFalse(refused["completed"])
                self.assertEqual(self.executions, 0)
                observed = self.fixture.run_host({**spec, "mode": "observe"})
                self.assertFalse(observed["completed"])
                self.assertEqual(self.executions, 0)

    def test_each_interrupted_scenario_retains_its_own_bound_child_diagnostic(self) -> None:
        def fail(*args: Any, **kwargs: Any) -> bytes:
            raise self.host["RehearsalCommandFailure"](
                {
                    "step": "business_validation",
                    "exit_status": 9,
                    "error_text": "Product validation unavailable",
                }
            )

        for key in ("first", "second"):
            spec = {**self.spec, "attempt_sha256": canonical_json_sha256({"operation_key": key})}
            spec["business_validation_identity"] = {
                **self.plan,
                "attempt_sha256": spec["attempt_sha256"],
            }
            with (
                patch.dict(self.host, {"product_command": fail}),
                self.assertRaises(self.host["RehearsalCommandFailure"]),
            ):
                self.fixture.run_host(spec)
            observed = self.fixture.run_host({**spec, "mode": "observe", "inspect_capture": True})
            self.assertFalse(observed["completed"])
            self.assertTrue(observed["custody"]["effect_started"])
            self.assertEqual(observed["child_failure"]["attempt_sha256"], spec["attempt_sha256"])

    def test_receipt_identities_and_shape_are_checked_before_host_custody(self) -> None:
        for field, value in (
            ("product", "other"),
            ("rehearsal_id", "lp-rehearsal-" + "c" * 32),
            ("artifact_id", "other"),
            ("source_commit", "c" * 40),
            ("database", "production"),
            ("attempt_sha256", "c" * 64),
            ("traceback", "copied Client value"),
            ("passed", "true"),
        ):
            changed = {**receipt(self.plan), field: value}
            output = b"business_validation_result=" + json.dumps(changed).encode().hex().encode()
            with (
                self.subTest(field=field),
                patch.dict(self.host, {"product_command": Mock(return_value=output)}),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                self.host["business_validation"](self.spec, self.containers)
        valid = self.host["business_validation"](self.spec, self.containers)
        self.assertEqual(valid, receipt(self.plan))
        self.assertIn(BUSINESS_VALIDATION_ENTRYPOINT, self.last_command)

    def test_invalid_or_missing_reply_preserves_unknown_original_without_replay(self) -> None:
        valid = json.dumps(receipt(self.plan)).encode().hex().encode()
        for index, output in enumerate(
            (
                b"copied customer",
                b"business_validation_result=zz",
                b"business_validation_result=" + valid + b"\nbusiness_validation_result=" + valid,
            )
        ):
            attempt = canonical_json_sha256({"invalid_reply": index})
            spec = {
                **self.spec,
                "attempt_sha256": attempt,
                "business_validation_identity": {**self.plan, "attempt_sha256": attempt},
            }
            child = Mock(return_value=output)
            with patch.dict(self.host, {"product_command": child}):
                with self.assertRaises(RuntimeError):
                    self.fixture.run_host(spec)
                with self.assertRaises(RuntimeError):
                    self.fixture.run_host(spec)
                observed = self.fixture.run_host({**spec, "mode": "observe"})
            child.assert_called_once()
            self.assertFalse(observed["completed"])
            self.assertTrue(observed["custody"]["effect_started"])
            self.assertNotIn("copied customer", str(observed))

    def test_failed_product_retains_redacted_stderr_under_original_custody(self) -> None:
        import sys

        self.spec["environment"] = {"ODOO_DB_PASSWORD": "fixture-private-password"}

        def fail(arguments: list[str], *, stdin: bytes | None = None) -> bytes:
            script = (
                "import sys; print('copied customer'); sys.stderr.write('x' * 200000 + '\\n'); "
                "print('ValidationError: ODOO_FILESTORE_PATH is required; "
                "fixture-private-password; TOKEN=fixture-token', file=sys.stderr); "
                "print('DETAIL: copied row', file=sys.stderr); sys.exit(9)"
            )
            return cast(bytes, self.native_product_command([sys.executable, "-c", script]))

        with patch.dict(self.host, {"product_command": fail}):
            with self.assertRaises(self.host["RehearsalCommandFailure"]):
                self.fixture.run_host(self.spec)
        observed = self.fixture.run_host({**self.spec, "mode": "observe", "inspect_capture": True})
        diagnostic = observed["child_failure"]
        self.assertIn("ValidationError: ODOO_FILESTORE_PATH is required", diagnostic["error_text"])
        self.assertEqual(diagnostic["exit_status"], 9)
        self.assertEqual(diagnostic["step"], "business_validation")
        self.assertEqual(diagnostic["attempt_sha256"], self.spec["attempt_sha256"])
        for private in (
            "copied customer",
            "copied row",
            "fixture-private-password",
            "fixture-token",
        ):
            self.assertNotIn(private, diagnostic["error_text"])
        self.assertFalse(observed["completed"])

    def test_noisy_or_hung_product_does_not_hold_host_forever(self) -> None:
        import sys

        helper = rehearsal_tests.RehearsalHostSafetyTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        cases: tuple[tuple[str, dict[str, int]], ...] = (
            (
                "print('copied customer' * "
                + str(helper.host["PRODUCT_COMMAND_OUTPUT_MAX_BYTES"])
                + ")",
                {},
            ),
            ("import time; time.sleep(60)", {"PRODUCT_COMMAND_TIMEOUT_SECONDS": 0}),
        )
        for script, limits in cases:
            with (
                self.subTest(script=script),
                patch.dict(helper.host, limits),
                self.assertRaises(helper.host["RehearsalCommandFailure"]) as failure,
            ):
                helper.host["product_command"]([sys.executable, "-c", script])
            self.assertNotIn("copied customer", str(failure.exception.diagnostic))
        self.assertEqual(
            helper.host["product_command"](
                [
                    sys.executable,
                    "-c",
                    "import sys; print('trace', file=sys.stderr); print('receipt')",
                ]
            ),
            b"receipt\n",
        )

    def test_product_stderr_does_not_hide_host_observation_failure(self) -> None:
        import sys

        cases: tuple[tuple[str, dict[str, int]], ...] = (
            ("import time; time.sleep(60)", {"PRODUCT_COMMAND_TIMEOUT_SECONDS": 1}),
            ("print('x' * 100000)", {}),
        )
        for command, limits in cases:
            script = "import sys; print('startup notice', file=sys.stderr, flush=True); " + command
            with (
                self.subTest(command=command),
                patch.dict(self.host, limits),
                self.assertRaises(self.host["RehearsalCommandFailure"]) as failure,
            ):
                self.native_product_command([sys.executable, "-c", script])
            lines = failure.exception.diagnostic["error_text"].splitlines()
            self.assertIn("startup notice", lines)
            self.assertNotEqual(lines[-1], "startup notice")


class BusinessValidationClientTests(unittest.TestCase):
    def test_a_prior_failed_scenario_cannot_make_an_inert_review_or_other_operation_fail(
        self,
    ) -> None:
        from tests import test_odoo_upgrade_rehearsal_client as clients

        fixture = clients.RehearsalClientTests()
        self.addCleanup(fixture.doCleanups)
        for mode in ("dry-run", "readback"):
            with self.subTest(mode=mode):
                code, result, _ = fixture.call(
                    mode,
                    response_result={
                        "action": "prepare",
                        "applied": False,
                        "completed": True,
                        "plan_sha256": "a" * 64,
                        "public_route": False,
                        "business_host_frozen": False,
                        "business_validation": {"passed": False},
                    },
                )
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "verified")


class BusinessValidationHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scenario = BusinessValidationAdapterTests()
        self.scenario.setUp()
        self.addCleanup(self.scenario.doCleanups)
        self.fixture = self.scenario.fixture

    async def test_read_identity_can_review_but_cannot_start_business_writes(self) -> None:
        from tests.support.http import request
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE

        app = rehearsal_tests.RehearsalHttpTests.read_only_app(cast(Any, self))
        route = ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product)
        before = self.fixture.authority()
        payload = self.scenario.request.model_dump(mode="json")
        response = await request(app, "POST", route, payload=payload)
        self.assertEqual(response.status_code, 202, response.text)
        reviewed = response.json()["result"]
        self.assertEqual(
            reviewed["plan_sha256"], self.scenario.adapter.reviewed_binding(self.scenario.record)
        )
        self.assertEqual(self.fixture.authority(), before)
        response = await request(
            app,
            "POST",
            route,
            payload={**payload, "mode": "apply", "reviewed_plan_sha256": reviewed["plan_sha256"]},
            headers={"Idempotency-Key": "business-key"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        response = await request(
            app,
            "POST",
            route,
            payload={**payload, "mode": "readback"},
            headers={"Idempotency-Key": "business-key"},
        )
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
