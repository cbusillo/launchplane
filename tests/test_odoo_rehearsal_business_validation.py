"""Fixed product invocation preserves isolation, original custody and value-free evidence."""

import json
import unittest
from typing import Any, cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane._odoo_rehearsal_business_validation import (
    BUSINESS_VALIDATION_ENTRYPOINT,
    BUSINESS_VALIDATION_RECEIPT_MAX_BYTES,
)
from control_plane.contracts.odoo_rehearsal_business_validation import BusinessValidationReceipt
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRequest
from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter, RehearsalRefused
from tests import test_odoo_upgrade_rehearsal as rehearsal_tests


def identity(record: Any) -> dict[str, str]:
    return {
        "product": record.product,
        "rehearsal_id": record.rehearsal_id,
        "artifact_id": record.target_artifact.artifact_id,
        "source_commit": record.target_artifact.source_commit,
        "database": record.rehearsal_id.replace("-", "_") + "_final_upgrade",
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

    def test_prepare_checks_the_candidate_without_network_or_credentials(self) -> None:
        self.host["product_command"] = call = Mock(return_value=b"")
        self.host["check_business_entrypoint"](self.spec)
        arguments = call.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn(self.spec["target_image"], arguments)
        self.assertIn(BUSINESS_VALIDATION_ENTRYPOINT, arguments)
        self.assertIn("--check", arguments)
        self.assertFalse(
            set(arguments) & {"--env", "-e", "--env-file", "-v", "--volume", "--mount"}
        )
        call.side_effect = self.host["RehearsalCommandFailure"](
            {
                "step": "business_validation",
                "exit_status": 2,
                "error_text": "Product validation unavailable",
            }
        )
        self.spec.update(action="prepare", source_image=self.spec["target_image"])
        self.spec["record"]["state"] = "planned"
        self.host["compose"] = compose = Mock()
        refused = self.fixture.run_host(self.spec)
        self.assertFalse(refused["completed"])
        self.assertEqual(refused["refusal_code"], "rehearsal_entrypoint_unavailable")
        compose.assert_not_called()
        self.assertFalse((self.root / "prepare.started").exists())
        calls = call.call_count
        self.assertEqual(self.fixture.run_host({**self.spec, "mode": "observe"}), refused)
        self.assertEqual(call.call_count, calls)

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

    def test_failed_noisy_or_hung_product_does_not_expose_output_or_hold_host_forever(self) -> None:
        import sys

        helper = rehearsal_tests.RehearsalHostSafetyTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        cases: tuple[tuple[str, dict[str, int]], ...] = (
            (
                "import sys; print('copied customer'); print('private traceback', file=sys.stderr); sys.exit(9)",
                {},
            ),
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
            self.assertNotIn("private traceback", str(failure.exception.diagnostic))
        self.assertEqual(
            helper.host["product_command"]([sys.executable, "-c", "print('receipt')"]), b"receipt\n"
        )


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
