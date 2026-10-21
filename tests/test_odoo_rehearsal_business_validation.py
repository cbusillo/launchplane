"""Product-owned batches exercise only synthetic records on the bound copy."""

import unittest
import json
import sqlite3
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.odoo_rehearsal_business_validation import BusinessValidation
from control_plane._odoo_rehearsal_business_validation import BUSINESS_VALIDATION_LIBRARY
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRequest
from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter, RehearsalRefused
from tests import test_odoo_upgrade_rehearsal as rehearsal_tests


def batch(database: str = "lp_rehearsal_" + "a" * 32 + "_final_upgrade") -> BusinessValidation:
    return BusinessValidation.model_validate(
        {
            "database": database,
            "source_commit": "b" * 40,
            "steps": [
                {
                    "op": "create",
                    "key": "customer",
                    "model": "res.partner",
                    "values": {"name": "Synthetic rehearsal customer"},
                },
                {
                    "op": "create",
                    "key": "order",
                    "model": "sale.order",
                    "values": {"partner_id": {"$ref": "customer"}},
                },
                {"op": "call", "key": "confirm", "record": "order", "method": "action_confirm"},
                {
                    "op": "assert",
                    "key": "confirmed",
                    "record": "order",
                    "field": "state",
                    "expected": "sale",
                },
            ],
        }
    )


class BusinessValidationContractTests(unittest.TestCase):
    def test_batch_requires_the_exact_final_copy_and_only_prior_synthetic_references(self) -> None:
        plan = batch()
        for database in ("production", plan.database.replace("final_upgrade", "upgrade")):
            with self.subTest(database=database), self.assertRaises(ValidationError):
                batch(database)
        for change in (
            {"model": "ir.actions.server"},
            {"values": {"parent_id": {"$ref": "missing"}}},
        ):
            data = plan.model_dump(mode="json")
            data["steps"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BusinessValidation.model_validate(data)
        data = plan.model_dump(mode="json")
        data["steps"][2]["method"] = "unlink"
        with self.assertRaises(ValidationError):
            BusinessValidation.model_validate(data)
        for command in (
            [0, 0, {"name": "nested"}],
            [1, 7, {"name": "changed"}],
            [2, 7],
            [6, 0, [7]],
        ):
            data = plan.model_dump(mode="json")
            data["steps"][0]["values"]["child_ids"] = [command]
            with self.subTest(command=command), self.assertRaises(ValidationError):
                BusinessValidation.model_validate(data)

    def test_decimal_quantities_and_quoted_strings_are_data_in_the_reviewed_digest(self) -> None:
        data = batch().model_dump(mode="json")
        data["steps"][0]["values"]["name"] = "'\n__import__('os')"
        data["steps"][1]["values"]["price_unit"] = 12.75
        plan = BusinessValidation.model_validate(data)
        changed = plan.model_dump(mode="json")
        changed["steps"][1]["values"]["price_unit"] += 0.01
        self.assertNotEqual(plan.sha256, BusinessValidation.model_validate(changed).sha256)


class SqliteBusinessEnvironment:
    """Small transactional ORM stand-in; the actual Odoo image is qualified separately."""

    def __init__(self, database: str) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, data TEXT)")
        self.cr = SimpleNamespace(
            dbname=database, commit=self.connection.commit, rollback=self.connection.rollback
        )

    def __getitem__(self, model: str) -> Any:
        from collections import defaultdict

        environment = self

        class Record:
            def __init__(self, identifier: int) -> None:
                self.id = identifier
                self.ids = [identifier]
                self._name = model

            def __getitem__(self, field: str) -> Any:
                data = environment.connection.execute(
                    "SELECT data FROM records WHERE id=?", (self.id,)
                ).fetchone()
                return json.loads(data[0])[field]

            def action_confirm(self) -> None:
                data = environment.connection.execute(
                    "SELECT data FROM records WHERE id=?", (self.id,)
                ).fetchone()
                values = {**json.loads(data[0]), "state": "sale"}
                environment.connection.execute(
                    "UPDATE records SET data=? WHERE id=?", (json.dumps(values), self.id)
                )

        class Model:
            _fields = defaultdict(
                lambda: SimpleNamespace(type="char", comodel_name=""),
                partner_id=SimpleNamespace(type="many2one", comodel_name="res.partner"),
            )

            def with_context(self, **kwargs: object) -> "Model":
                return self

            def create(self, values: dict[str, object]) -> Record:
                if values.get("name") == "raise-private-error":
                    raise ValueError("private copied Client value")
                cursor = environment.connection.execute(
                    "INSERT INTO records(data) VALUES (?)", (json.dumps(values),)
                )
                return Record(cast(int, cursor.lastrowid))

        return Model()

    def flush_all(self) -> None:
        pass

    def invalidate_all(self) -> None:
        pass


class BusinessValidationEngineTests(unittest.TestCase):
    def test_fixed_program_commits_reads_back_and_rolls_back_failed_assertions_without_values(
        self,
    ) -> None:
        namespace: dict[str, Any] = {}
        exec(BUSINESS_VALIDATION_LIBRARY, namespace)
        run = namespace["run_business_validation"]
        for outcome in (
            "pass",
            "assertion",
            "exception",
            "wrong_copy",
            "post_commit",
            "post_commit_assertion",
            "wrong_relation",
            "commit_uncertain",
        ):
            with self.subTest(outcome=outcome):
                plan = batch().model_dump(mode="json")
                env = SqliteBusinessEnvironment(
                    plan["database"] if outcome != "wrong_copy" else "production"
                )
                self.addCleanup(env.connection.close)
                if outcome == "assertion":
                    plan["steps"][-1]["expected"] = "wrong"
                if outcome == "exception":
                    plan["steps"][0]["values"]["name"] = "raise-private-error"
                if outcome == "wrong_relation":
                    plan["steps"][1]["values"]["partner_id"] = 1
                if outcome == "commit_uncertain":
                    env.cr.commit = Mock(side_effect=RuntimeError("connection lost"))
                    with self.assertRaises(RuntimeError):
                        run(env, plan)
                    continue
                if outcome == "post_commit":
                    cast(Any, env).invalidate_all = Mock(
                        side_effect=RuntimeError("private copied Client value")
                    )
                if outcome == "post_commit_assertion":

                    def change_after_commit() -> None:
                        env.connection.execute(
                            "UPDATE records SET data=? WHERE id=2",
                            (json.dumps({"state": "draft"}),),
                        )

                    cast(Any, env).invalidate_all = change_after_commit
                result = run(env, plan)
                self.assertEqual(result["passed"], outcome == "pass")
                self.assertEqual(
                    result["committed"], outcome in {"pass", "post_commit", "post_commit_assertion"}
                )
                count = env.connection.execute("SELECT count(*) FROM records").fetchone()[0]
                self.assertEqual(count, 2 if result["committed"] else 0)
                self.assertEqual(bool(result["steps"][0]["record_ids"]), result["committed"])
                if outcome == "post_commit_assertion":
                    self.assertFalse(result["steps"][-1]["passed"])
                if outcome == "pass":
                    identifier = result["steps"][1]["record_ids"][0]
                    values = json.loads(
                        env.connection.execute(
                            "SELECT data FROM records WHERE id=?", (identifier,)
                        ).fetchone()[0]
                    )
                    self.assertEqual(values["state"], plan["steps"][-1]["expected"])
                    self.assertEqual(values["partner_id"], result["steps"][0]["record_ids"][0])
                self.assertNotIn("private copied Client value", str(result))
                self.assertNotIn("Synthetic rehearsal customer", str(result))

    def test_composed_runner_uses_copy_settings_and_releases_lock_without_raw_output(self) -> None:
        import io
        import sys
        from contextlib import redirect_stdout
        from types import ModuleType
        from control_plane._odoo_rehearsal_business_validation import business_validation_runner

        plan = batch().model_dump(mode="json")
        env = SqliteBusinessEnvironment(plan["database"])
        self.addCleanup(env.connection.close)
        settings = SimpleNamespace(
            host="database", platform_instance="rehearsal", db_name=plan["database"]
        )
        module = ModuleType("run_odoo_data_workflows")
        module.__dict__["LocalServerSettings"] = lambda: settings
        acquired = Mock()
        released = Mock()

        class Runner:
            os_env: dict[str, str] = {}

            def __init__(self, *args: object) -> None:
                pass

            acquire_data_workflow_lock = acquired
            release_data_workflow_lock = released

            def _ensure_odoo_shell_preflight(self) -> None:
                pass

            def _odoo_shell_command(self) -> list[str]:
                return ["synthetic-shell"]

        module.__dict__["OdooDataWorkflowRunner"] = Runner

        def shell(*args: object, **kwargs: Any) -> Any:
            output = io.StringIO()
            with redirect_stdout(output):
                exec(kwargs["input"], {"env": env})
            return SimpleNamespace(
                returncode=0, stdout=output.getvalue().encode(), stderr=b"private copied value"
            )

        output = io.StringIO()
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch("subprocess.run", side_effect=shell),
            redirect_stdout(output),
        ):
            exec(business_validation_runner(plan), {})
        observed = json.loads(bytes.fromhex(output.getvalue().strip().split("=", 1)[1]))
        self.assertTrue(observed["passed"])
        self.assertTrue(observed["committed"])
        acquired.assert_called_once()
        released.assert_called_once()
        self.assertNotIn("private copied value", output.getvalue())
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch("subprocess.run", side_effect=RuntimeError("shell unavailable")),
            self.assertRaises(RuntimeError),
        ):
            exec(business_validation_runner(plan), {})
        self.assertEqual(released.call_count, acquired.call_count)
        settings.host = "production"
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch("subprocess.run") as invoke,
            self.assertRaises(RuntimeError),
        ):
            exec(business_validation_runner(plan), {})
        invoke.assert_not_called()


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
        data = batch(self.record.rehearsal_id.replace("-", "_") + "_final_upgrade").model_dump(
            mode="json"
        )
        data["source_commit"] = self.record.target_artifact.source_commit
        self.plan = BusinessValidation.model_validate(data)
        self.request = OdooUpgradeRehearsalRequest.model_validate(
            {
                **self.fixture.request.model_dump(mode="json"),
                "action": "validate-business",
                "business_validation": self.plan.model_dump(mode="json"),
            }
        )
        self.adapter = OdooRehearsalAdapter(
            self.fixture.root,
            self.fixture.store,
            self.fixture.product,
            self.request,
            self.fixture.principal,
            "fixture-trace",
        )

    def test_plan_binds_batch_copy_candidate_and_preserves_legacy_intent(self) -> None:
        self.assertEqual(self.request.intent(), self.fixture.request.intent())
        self.assertEqual(self.adapter.inputs()[0], self.record)
        binding = self.adapter.reviewed_binding(self.record)
        changed = self.plan.model_copy(update={"source_commit": "e" * 40})
        self.adapter.request = self.request.model_copy(update={"business_validation": changed})
        self.assertNotEqual(binding, self.adapter.reviewed_binding(self.record))
        with self.assertRaises(RehearsalRefused):
            self.adapter.inputs()
        for invalid in ("prod", self.fixture.context):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                OdooUpgradeRehearsalRequest.model_validate(
                    {**self.request.model_dump(mode="json"), "rehearsal_id": invalid}
                )

    def test_apply_and_original_key_observation_retain_sanitized_evidence_and_snapshot_lineage(
        self,
    ) -> None:
        from control_plane.contracts.odoo_rehearsal_business_validation import (
            BusinessValidationReceipt,
            BusinessStepEvidence,
        )

        authority = self.fixture.authority()
        self.adapter.request = self.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.adapter.reviewed_binding(self.record),
            }
        )
        validation = BusinessValidationReceipt(
            validation_sha256=self.plan.sha256,
            database=self.plan.database,
            source_commit=self.plan.source_commit,
            attempt_sha256=canonical_json_sha256({"operation_key": "original"}),
            passed=True,
            committed=True,
            steps=[
                BusinessStepEvidence(key=step.key, record_ids=[11], passed=True)
                for step in self.plan.steps
            ],
        )
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
        # A committed failed batch stays identified when a corrected one passes.
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
        receipt = {
            "validation_sha256": self.plan.sha256,
            "attempt_sha256": canonical_json_sha256({"operation_key": "original"}),
            "database": self.plan.database,
            "source_commit": self.plan.source_commit,
            "passed": True,
            "committed": True,
            "steps": [
                {"key": step.key, "record_ids": [31], "passed": True} for step in self.plan.steps
            ],
        }
        pending = {
            "intent_sha256": self.record.intent_sha256,
            "record_binding_sha256": self.record.binding_sha256,
            "attempt_sha256": receipt["attempt_sha256"],
            "receipt": receipt,
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
        completed.update(completed=True, business_validation=receipt)
        with patch.object(OdooRehearsalAdapter, "host", return_value=read(completed)):
            self.assertEqual(
                self.adapter.observe("original", "host", "reconcile").outcome, "present"
            )
        current = self.fixture.store.read_odoo_upgrade_rehearsal(self.record.rehearsal_id)
        assert current is not None
        self.assertEqual(current.business_validations[0].steps[0].record_ids, [31])

    def test_stale_review_cannot_dispatch_a_changed_batch(self) -> None:
        from control_plane.provider_operations import (
            ProviderMutationRejectedError,
            ProviderOperationLease,
        )

        self.adapter.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": self.record.binding_sha256}
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
        self.plan = batch(self.fixture.identifier.replace("-", "_") + "_final_upgrade")
        self.spec.update(
            {
                "action": "validate-business",
                "business_validation": self.plan.model_dump(mode="json"),
                "business_validation_sha256": self.plan.sha256,
                "business_validation_runner": "fixed runner",
            }
        )
        self.spec["record"].update(
            {
                "target_artifact": {"source_commit": self.plan.source_commit},
                "source_odoo_version": "1.0",
            }
        )
        self.host["inspection"] = lambda *args: self.containers
        self.host["private_health"] = Mock()
        self.host["sql"] = lambda *args: self.host["witness"](self.spec, self.plan.database)
        self.spec["readback_spec"] = "fixed-policy"
        self.spec["readback_program"] = "fixed-readback"
        self.executions = 0

        def docker(*args: str, stdin: bytes | None = None) -> bytes:
            if stdin == b"fixed-readback":
                return b"integration_readback_ok=true\n"
            self.executions += 1
            payload = {
                "passed": True,
                "committed": True,
                "steps": [
                    {"key": step.key, "record_ids": [31], "passed": True}
                    for step in self.plan.steps
                ],
            }
            return (
                b"business_validation_result=" + json.dumps(payload).encode().hex().encode() + b"\n"
            )

        self.host["docker"] = docker

    def test_native_batch_retains_original_receipt_and_never_replays_a_lost_reply(self) -> None:
        result = self.fixture.run_host(self.spec)
        self.assertTrue(result["business_validation"]["passed"])
        self.assertEqual(result["business_validation"]["database"], self.plan.database)
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
            pending["pending_business_validation"]["receipt"]["steps"][0]["record_ids"], [31]
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
        result = self.host["snapshot"](self.spec, self.root, self.containers, self.plan.database)
        self.assertEqual(result["business_validation_sha256"], canonical_json_sha256(history))
        import shutil

        shutil.rmtree(self.root / self.plan.database)
        history.append({"passed": False})
        self.host["bounded_capture"].reset_mock()
        with self.assertRaises(RuntimeError):
            self.host["snapshot"](self.spec, self.root, self.containers, self.plan.database)
        self.host["bounded_capture"].assert_not_called()
        self.assertFalse((self.root / self.plan.database).exists())

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

    def test_each_interrupted_batch_retains_its_own_bound_child_diagnostic(self) -> None:
        original = self.host["docker"]

        def fail(*args: str, stdin: bytes | None = None) -> bytes:
            if stdin == b"fixed runner":
                raise self.host["RehearsalCommandFailure"](
                    {
                        "step": "business_validation",
                        "exit_status": 9,
                        "error_text": "fixed runner unavailable",
                    }
                )
            return cast(bytes, original(*args, stdin=stdin))

        for key in ("first", "second"):
            with self.subTest(key=key):
                spec = {
                    **self.spec,
                    "attempt_sha256": canonical_json_sha256({"operation_key": key}),
                }
                with (
                    patch.dict(self.host, {"docker": fail}),
                    self.assertRaises(self.host["RehearsalCommandFailure"]),
                ):
                    self.fixture.run_host(spec)
                observed = self.fixture.run_host(
                    {**spec, "mode": "observe", "inspect_capture": True}
                )
                self.assertFalse(observed["completed"])
                self.assertTrue(observed["custody"]["effect_started"])
                self.assertEqual(
                    observed["child_failure"]["attempt_sha256"], spec["attempt_sha256"]
                )


class BusinessValidationClientTests(unittest.TestCase):
    def test_a_prior_failed_batch_cannot_make_an_inert_review_or_other_operation_fail(self) -> None:
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
