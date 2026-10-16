from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from control_plane.contracts.odoo_walkthrough import (
    WALKTHROUGH_ACTION,
    WALKTHROUGH_ROUTE,
    OdooWalkthroughRequest,
    WalkthroughStep,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.odoo_walkthrough import ACCOUNT_ODOO_PROGRAM
from control_plane.http_routes.odoo_walkthrough import (
    WalkthroughDependencies,
    register_walkthrough_route,
)
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    OdooRuntimeEvidence,
    OdooRuntimeSelection,
)
from control_plane.odoo_walkthrough import WalkthroughPlan
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import request as http_request


def task_request(**changes: Any) -> OdooWalkthroughRequest:
    return OdooWalkthroughRequest.model_validate(
        {
            "product": "example-odoo",
            "context": "example",
            "task_id": "1" * 32,
            "operation": "provision",
            **changes,
        }
    )


class TaskContractTests(unittest.TestCase):
    def test_only_testing_and_bounded_declarative_browser_input(self) -> None:
        for instance in ("prod", "production", "preview-1", ""):
            with self.subTest(instance=instance), self.assertRaises(ValidationError):
                task_request(instance=instance)
        for value in (
            "https://elsewhere.example/web",
            "//elsewhere/web",
            "/web?password=x",
            "/web/login",
            "/web\\evil",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                WalkthroughStep(action="open", value=value)
        for value in ("staff password", "1234"):
            with self.assertRaises(ValidationError):
                WalkthroughStep(action="fill", selector="input", value=value)
        self.assertEqual(WalkthroughStep(action="open", value="/web#action=example").action, "open")
        with self.assertRaises(ValidationError):
            task_request(groups=("base.group_system",))


class TaskRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = PostgresRecordStore(
            database_url="sqlite+pysqlite:///" + str(self.root / "state.db")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_SECRET_KEYS_JSON": json.dumps(
                        {
                            "active_key_id": "test",
                            "keys": {"test": Fernet.generate_key().decode()},
                        }
                    ),
                },
            )
        )
        identity = RuntimeIdentity(
            environment_kind="stable",
            product="example-odoo",
            context="example",
            instance="testing",
            deployment_record_id="deployment-fixture",
            artifact_id="artifact-fixture",
            source_git_ref="b" * 40,
            image_reference="ghcr.io/example/odoo@sha256:" + "c" * 64,
        )
        connection = OdooRuntimeConnection(
            selection=OdooRuntimeSelection(identity, "https://testing.example"),
            evidence=OdooRuntimeEvidence(
                identity=identity,
                target_id="testing-target",
                target_name="testing",
                container_id="d" * 64,
                observed_at="2026-10-15T00:00:00Z",
            ),
            host="https://provider.example",
            token="provider-secret",
            server_id="host-fixture",
            app_name="testing-fixture",
            credentials={"ODOO_DB_NAME": "testing"},
        )
        self.plan = WalkthroughPlan(
            "a" * 64,
            "b" * 64,
            "testing",
            "c" * 64,
            "2026-10-15T00:00:00Z",
            "task-fixture",
            connection,
        )
        self.planner = self.stack.enter_context(
            patch(
                "control_plane.http_routes.odoo_walkthrough.plan_walkthrough",
                return_value=self.plan,
            )
        )
        self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.plan_walkthrough", return_value=self.plan)
        )
        self.stack.enter_context(patch("control_plane.odoo_walkthrough.assert_selection_current"))
        self.account = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.testing_account_command",
                return_value={"account_id": 17, "status": "pass"},
            )
        )
        self.browser = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.run_walkthrough_browser", return_value=(0, ()))
        )
        self.allowed = True
        self.actions: list[str] = []
        self.app: Any = FastAPI()

        def read_identity() -> LocalOperatorIdentity:
            return LocalOperatorIdentity(subject="test-agent", token_label="test-token")

        def allows(**arguments: Any) -> bool:
            self.actions.append(arguments["action"])
            self.assertEqual(arguments["target"].instances, ("testing",))
            return self.allowed

        def error(**arguments: Any) -> HTTPException:
            return HTTPException(
                status_code=arguments["status_code"],
                detail={
                    "trace_id": arguments["trace_id"],
                    "code": arguments["code"],
                    "message": arguments["message"],
                },
            )

        register_walkthrough_route(
            cast(ApiRouteRegistrar, self.app),
            dependencies=WalkthroughDependencies(
                read_identity=read_identity,
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "trace-fixture",
                authorization_allows=allows,
                http_error=error,
                control_plane_root=self.root,
                error_response_model=BaseModel,
            ),
        )

    def invoke(self, request: OdooWalkthroughRequest, key: str = "") -> tuple[int, dict[str, Any]]:
        response = asyncio.run(
            http_request(
                self.app,
                "POST",
                WALKTHROUGH_ROUTE,
                payload=request.model_dump(mode="json"),
                headers={"Idempotency-Key": key},
            )
        )
        return response.status_code, response.json()

    def test_denial_happens_before_any_provider_or_secret_operation(self) -> None:
        self.allowed = False
        status, result = self.invoke(
            task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "key"
        )
        self.assertEqual(status, 403)
        self.assertEqual(result["detail"]["code"], "authorization_denied")
        self.assertEqual(self.actions, [WALKTHROUGH_ACTION])
        self.planner.assert_not_called()
        self.account.assert_not_called()
        self.assertEqual(self.store.list_secret_records(), ())

    def test_dry_run_and_stale_plan_create_nothing(self) -> None:
        status, result = self.invoke(task_request())
        self.assertEqual((status, result["result"]["status"]), (200, "ready"))
        self.assertEqual(self.store.list_secret_records(), ())
        stale = task_request(mode="apply", reviewed_plan_sha256="d" * 64)
        self.assertEqual(self.invoke(stale, "stale")[0], 409)
        self.account.assert_not_called()

    def test_provision_replay_browser_and_cleanup_keep_password_internal(self) -> None:
        provision = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        status, receipt = self.invoke(provision, "provision")
        self.assertEqual((status, receipt["result"]["status"]), (200, "pass"))
        password = self.account.call_args.kwargs["payload"]["password"]
        self.assertTrue(password)
        self.assertNotIn(password, json.dumps(receipt))
        record = self.store.list_secret_records()[0]
        version = self.store.read_secret_version(record.current_version_id)
        self.assertNotIn(password, version.ciphertext)
        self.assertEqual(self.invoke(provision, "provision")[1], receipt)
        self.assertEqual(self.account.call_count, 1)
        run = task_request(operation="run", mode="apply", reviewed_plan_sha256=self.plan.digest)
        status, signed_in = self.invoke(run, "browser")
        self.assertTrue(signed_in["result"]["signed_in"])
        self.assertEqual(self.browser.call_args.kwargs["password"], password)
        self.assertNotIn(password, json.dumps(signed_in))
        cleanup = task_request(
            operation="cleanup", mode="apply", reviewed_plan_sha256=self.plan.digest
        )
        self.assertEqual(self.invoke(cleanup, "cleanup")[1]["result"]["status"], "pass")
        self.assertEqual(self.store.read_secret_record(record.secret_id).status, "disabled")
        self.assertTrue(all(b.status == "disabled" for b in self.store.list_secret_bindings()))

    def test_browser_exception_is_value_free_and_never_replays_transactions(self) -> None:
        provision = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        self.invoke(provision, "provision")
        password = self.account.call_args.kwargs["payload"]["password"]
        self.browser.side_effect = RuntimeError("cookie=session-secret; password=" + password)
        run = task_request(operation="run", mode="apply", reviewed_plan_sha256=self.plan.digest)
        status, result = self.invoke(run, "failed-browser")
        self.assertEqual((status, result["result"]["status"]), (200, "unknown"))
        self.assertNotIn(password, json.dumps(result))
        self.assertNotIn("session-secret", json.dumps(result))
        self.invoke(run, "failed-browser")
        self.assertEqual(self.browser.call_count, 1)

    def test_changed_task_cannot_reuse_a_key(self) -> None:
        first = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        self.invoke(first, "same-key")
        second = first.model_copy(update={"task_id": "2" * 32})
        self.assertEqual(self.invoke(second, "same-key")[0], 409)
        self.assertEqual(self.account.call_count, 1)


class AccountProgramTests(unittest.TestCase):
    """Execute the shipped consumer against a tiny ORM, including staff negatives."""

    def setUp(self) -> None:
        self.addCleanup(logging.disable, logging.root.manager.disable)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parameters: dict[str, str] = {TESTING_COPY_COMPLETION_PARAMETER: "testing-copy"}
        self.user = MagicMock()
        self.user.id, self.user.login, self.user.active = 17, "lp_walk_" + "1" * 32, True
        self.user.__len__.return_value = 1
        self.user.has_group.return_value = False
        self.users = MagicMock()
        self.users.with_context.return_value = self.users
        self.users.search_count.return_value = 0
        self.users.create.return_value = self.user
        self.users.browse.return_value.exists.return_value = self.user
        parameters = MagicMock()
        parameters.get_param.side_effect = lambda key: self.parameters.get(key)
        parameters.set_param.side_effect = self.parameters.__setitem__
        self.env = MagicMock()
        models = {"res.users": self.users, "ir.config_parameter": parameters}
        self.env.__getitem__.side_effect = lambda name: models[name]
        for model in models.values():
            model.sudo.return_value = model
        self.env.cr.fetchone.return_value = (False,)
        group = MagicMock()
        group._name, group.id = "res.groups", 8
        self.env.ref.return_value = group
        self.request: dict[str, Any] = {
            "instance": "testing",
            "copy_binding": "testing-copy",
            "task_id": "1" * 32,
            "binding": "task-binding",
            "usage_since": "2026-10-15T00:00:00Z",
            "operation": "provision",
            "groups": ["base.group_user"],
            "password": "test-only-secret",
        }

    def execute(self) -> dict[str, Any]:
        output = Path(self.directory.name) / (
            str(len(list(Path(self.directory.name).iterdir()))) + ".json"
        )
        exec(
            compile(ACCOUNT_ODOO_PROGRAM, "testing-account-consumer", "exec"),
            {
                "env": self.env,
                "request": {**self.request, "result_file": str(output)},
            },
        )
        return json.loads(output.read_text())  # type: ignore[no-any-return]

    def test_provision_preserves_staff_and_does_not_reset_existing_task(self) -> None:
        self.assertEqual(self.execute()["account_id"], self.user.id)
        self.users.create.assert_called_once()
        self.user.write.assert_not_called()
        self.execute()
        self.users.create.assert_called_once()
        self.user.write.assert_not_called()

    def test_existing_login_or_wrong_copy_refuses_without_writing(self) -> None:
        self.users.search_count.return_value = 1
        with self.assertRaises(RuntimeError):
            self.execute()
        self.users.create.assert_not_called()
        self.users.write.assert_not_called()
        self.parameters[TESTING_COPY_COMPLETION_PARAMETER] = "another-copy"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.env.cr.commit.assert_not_called()

    def test_production_and_recent_staff_signin_refuse_without_account_change(self) -> None:
        self.request["instance"] = "prod"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.request["instance"] = "testing"
        self.env.cr.fetchone.return_value = (True,)
        with self.assertRaises(RuntimeError):
            self.execute()
        self.users.create.assert_not_called()
        self.user.write.assert_not_called()

    def test_cleanup_archives_only_the_witnessed_task_and_changes_its_password(self) -> None:
        self.execute()
        self.request["operation"] = "cleanup"
        self.user.write.side_effect = lambda values: setattr(self.user, "active", values["active"])
        self.execute()
        values = self.user.write.call_args.args[0]
        self.assertFalse(values["active"])
        self.assertNotEqual(values["password"], self.request["password"])
        self.assertEqual(
            json.loads(self.parameters["launchplane.walkthrough." + "1" * 32])["state"], "closed"
        )
        self.user.write.reset_mock()
        self.execute()
        self.user.write.assert_not_called()

    def test_foreign_task_or_admin_group_is_never_adopted(self) -> None:
        self.execute()
        self.user.login = "staff"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.user.write.assert_not_called()

    def test_cleanup_of_interrupted_provision_can_prove_no_account_exists(self) -> None:
        self.request["operation"] = "cleanup"
        self.assertEqual(self.execute()["account_id"], 0)
        self.users.create.assert_not_called()
        self.user.write.assert_not_called()

    def test_existing_task_groups_cannot_be_changed_by_another_provision_request(self) -> None:
        self.execute()
        self.request["groups"] = ["stock.group_stock_manager"]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.user.write.assert_not_called()
