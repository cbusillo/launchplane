from __future__ import annotations

import asyncio
import base64
import io
from contextlib import ExitStack
import json
import logging
import subprocess
import sys
import time
from types import SimpleNamespace
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
    WALKTHROUGH_RECONCILIATION_REQUIRED,
    OdooWalkthroughRequest,
    WalkthroughStep,
    walkthrough_activity_scope,
    WALKTHROUGH_DISPATCH_SECONDS,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.dokploy.odoo_walkthrough import (
    ACCOUNT_ODOO_PROGRAM,
    ACCOUNT_HOST_PROGRAM,
    WALKTHROUGH_JOURNAL_DIRECTORY,
)
from control_plane.http_routes.odoo_walkthrough import (
    WalkthroughDependencies,
    register_walkthrough_route,
    walkthrough_actor,
    walkthrough_fingerprint,
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
            "/odoo/../web/database/manager",
            "/odoo/./contacts",
            "/odoo/%2e%2e/web/session/logout",
            "/odoo/%252e%252e/web/session/logout",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                WalkthroughStep(action="open", value=value)
        for value in ("staff password", "1234"):
            with self.assertRaises(ValidationError):
                WalkthroughStep(action="fill", selector="input", value=value)
        self.assertEqual(WalkthroughStep(action="open", value="/web#action=example").action, "open")
        for group in (
            "base.group_system",
            "base.group_allow_export",
            "base.group_sanitize_override",
            "base.group_no_one",
        ):
            with self.assertRaises(ValidationError):
                task_request(groups=(group,))


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
        self.finished = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.testing_account_dispatch_finished",
                return_value=True,
            )
        )
        self.unused = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.testing_task_unused", return_value=True)
        )
        self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.source.read_dokploy_config",
                return_value=(connection.host, connection.token),
            )
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
        status, _ = self.invoke(
            task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "key"
        )
        self.assertEqual(status, 403)
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
        self.assertEqual(status, 409)
        self.assertNotIn(password, json.dumps(result))
        self.assertNotIn("session-secret", json.dumps(result))
        self.planner.side_effect = ValueError("expired or held or changed runtime")
        status, recovered = self.invoke(run, "failed-browser")
        self.assertEqual((status, recovered["result"]["status"]), (200, "unknown"))
        self.assertEqual(self.browser.call_count, 1)

    def test_unobserved_dispatch_keeps_the_fence_and_never_retries_account_writes(self) -> None:
        self.account.side_effect = RuntimeError("provider-private-password")
        provision = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        self.assertEqual(self.invoke(provision, "unknown")[0], 409)
        self.finished.return_value = False
        self.planner.side_effect = ValueError("lane changed")
        status, response = self.invoke(provision, "unknown")
        self.assertEqual(status, 409)
        self.assertNotIn("provider-private-password", json.dumps(response))
        self.assertEqual(self.account.call_count, 1)
        original = self.store.read_idempotency_record(
            scope=walkthrough_activity_scope(task_request().product),
            route_path=WALKTHROUGH_ROUTE,
            idempotency_key="unknown",
        )
        assert original is not None

    def test_pre_dispatch_plan_failure_releases_its_reservation(self) -> None:
        with patch(
            "control_plane.odoo_walkthrough.plan_walkthrough", side_effect=ValueError("changed")
        ):
            status, _ = self.invoke(
                task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "not-dispatched"
            )
        self.assertEqual(status, 409)
        self.account.assert_not_called()
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=walkthrough_activity_scope(task_request().product),
                route_path=WALKTHROUGH_ROUTE,
                idempotency_key="not-dispatched",
            )
        )

    def test_live_original_lease_cannot_be_settled_or_inspected_as_an_ended_dispatch(self) -> None:
        provision = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        identity = LocalOperatorIdentity(subject="test-agent", token_label="test-token")
        actor = walkthrough_actor(identity)
        self.store.reserve_mutation(
            scope=walkthrough_activity_scope(task_request().product),
            route_path=WALKTHROUGH_ROUTE,
            idempotency_key="live",
            request_fingerprint=walkthrough_fingerprint(actor, provision),
            lease_owner="other-live-worker",
            reconciliation_key=self.plan.digest,
            provider_target_key="active-fixture-target",
        )
        self.planner.side_effect = ValueError("current lane is held")
        status, response = self.invoke(provision, "live")
        self.assertEqual(status, 409)
        self.assertEqual(response["detail"]["code"], WALKTHROUGH_RECONCILIATION_REQUIRED)
        self.finished.assert_not_called()
        self.account.assert_not_called()

    def test_screenshot_bytes_are_transient_and_replay_keeps_only_their_digest(self) -> None:
        self.invoke(task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "provision")
        png = base64.b64encode(b"private-png-fixture").decode()
        self.browser.return_value = (1, (png,))
        run = task_request(operation="run", mode="apply", reviewed_plan_sha256=self.plan.digest)
        status, first = self.invoke(run, "screenshot")
        self.assertEqual(status, 200)
        self.assertEqual(first["result"]["screenshots"], [png])
        replay = self.invoke(run, "screenshot")[1]
        self.assertEqual(replay["result"]["screenshots"], [])
        self.assertEqual(
            first["result"]["screenshot_sha256"], replay["result"]["screenshot_sha256"]
        )
        stored = self.store.read_idempotency_record(
            scope=walkthrough_activity_scope(task_request().product),
            route_path=WALKTHROUGH_ROUTE,
            idempotency_key="screenshot",
        )
        assert stored is not None
        self.assertNotIn(png, json.dumps(stored.response_payload))

    def test_changed_task_cannot_reuse_a_key(self) -> None:
        first = task_request(mode="apply", reviewed_plan_sha256=self.plan.digest)
        self.invoke(first, "same-key")
        second = first.model_copy(update={"task_id": "2" * 32})
        self.assertEqual(self.invoke(second, "same-key")[0], 409)
        self.assertEqual(self.account.call_count, 1)

    def test_definite_consumer_refusal_is_terminal_and_does_not_block_cleanup(self) -> None:
        self.account.return_value = {"account_id": 0, "status": "fail"}
        status, receipt = self.invoke(
            task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "refused"
        )
        self.assertEqual((status, receipt["result"]["status"]), (200, "fail"))
        self.browser.assert_not_called()
        self.account.return_value = {"account_id": 0, "status": "pass"}
        cleanup = task_request(
            operation="cleanup", mode="apply", reviewed_plan_sha256=self.plan.digest
        )
        self.assertEqual(self.invoke(cleanup, "after-refusal")[1]["result"]["status"], "pass")
        self.assertTrue(
            all(record.status == "disabled" for record in self.store.list_secret_records())
        )

    def test_cleanup_needs_no_password_or_secret_record(self) -> None:
        self.account.return_value = {"account_id": 0, "status": "pass"}
        with patch(
            "control_plane.odoo_walkthrough._decrypt_secret_value",
            side_effect=AssertionError("cleanup must not decrypt"),
        ):
            status, receipt = self.invoke(
                task_request(
                    operation="cleanup", mode="apply", reviewed_plan_sha256=self.plan.digest
                ),
                "empty-cleanup",
            )
        self.assertEqual((status, receipt["result"]["status"]), (200, "pass"))
        self.assertEqual(self.store.list_secret_records(), ())
        self.assertNotIn("password", self.account.call_args.kwargs["payload"])

    def test_new_staff_activity_stops_browser_before_another_step(self) -> None:
        self.invoke(task_request(mode="apply", reviewed_plan_sha256=self.plan.digest), "provision")
        self.unused.return_value = False

        def browser(**kwargs: Any) -> tuple[int, tuple[str, ...]]:
            kwargs["checkpoint"]()
            raise AssertionError("Browser must stop at the usage checkpoint")

        self.browser.side_effect = browser
        with patch("control_plane.odoo_walkthrough.require_drill_use_unprotected"):
            status, _ = self.invoke(
                task_request(operation="run", mode="apply", reviewed_plan_sha256=self.plan.digest),
                "staff-arrived",
            )
        self.assertEqual(status, 409)
        self.unused.assert_called_once()
        self.assertNotIn("password", self.unused.call_args.kwargs["intent"])


class AccountProgramTests(unittest.TestCase):
    """Execute the shipped consumer against a tiny ORM, including staff negatives."""

    def setUp(self) -> None:
        self.addCleanup(logging.disable, logging.root.manager.disable)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parameters: dict[str, str] = {TESTING_COPY_COMPLETION_PARAMETER: "testing-copy"}
        self.user = MagicMock()
        self.user.id, self.user.login, self.user.active = 17, "", True
        self.user.__len__.return_value = 1
        self.user.has_group.return_value = False
        self.users = MagicMock()
        self.users.with_context.return_value = self.users
        self.users.search_count.return_value = 0

        def create_user(values: dict[str, Any]) -> MagicMock:
            self.user.login = values["login"]
            return self.user

        self.users.create.side_effect = create_user
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
            "dispatch_deadline": int(time.time()) + WALKTHROUGH_DISPATCH_SECONDS,
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
        self.user.write.assert_not_called()
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

        def execute(query: str, *args: Any) -> None:
            if "res_users_log" in query:
                raise RuntimeError("login history unavailable")

        self.env.cr.execute.side_effect = execute
        self.request["operation"] = "cleanup"
        self.user.write.side_effect = lambda values: setattr(self.user, "active", values["active"])
        self.execute()
        values = self.user.write.call_args.args[0]
        self.assertFalse(values["active"])
        self.assertNotEqual(values["password"], self.request["password"])
        self.request["operation"] = "provision"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.request["operation"] = "cleanup"
        self.user.write.reset_mock()
        self.execute()
        self.user.write.assert_not_called()

    def test_foreign_task_is_never_adopted(self) -> None:
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

    def test_run_and_cleanup_use_stored_groups_without_repeating_provision_input(self) -> None:
        self.request["groups"] = ["stock.group_stock_user"]
        self.execute()
        self.request.update(operation="run", groups=["base.group_user"])
        self.execute()
        self.user.write.assert_not_called()
        self.request["operation"] = "cleanup"
        self.user.write.side_effect = lambda values: setattr(self.user, "active", values["active"])
        self.execute()
        self.assertFalse(self.user.active)

    def test_late_consumer_refuses_before_account_or_parameter_writes(self) -> None:
        self.request["dispatch_deadline"] = int(time.time()) - 1
        before = self.parameters.copy()
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.parameters, before)
        self.users.create.assert_not_called()
        self.user.write.assert_not_called()

    def test_groups_implying_administration_refuse_before_commit(self) -> None:
        self.user.has_group.return_value = True
        with self.assertRaises(RuntimeError):
            self.execute()
        self.env.cr.commit.assert_not_called()

    def test_application_groups_cannot_imply_export_or_unsanitized_html(self) -> None:
        self.request["groups"] = ["example.group_business"]
        for forbidden in ("base.group_allow_export", "base.group_sanitize_override"):
            self.user.has_group.side_effect = lambda name: name == forbidden
            with self.assertRaises(RuntimeError):
                self.execute()
        self.env.cr.commit.assert_not_called()

    def test_commit_failure_has_no_passing_consumer_receipt(self) -> None:
        self.env.cr.commit.side_effect = RuntimeError("commit failed")
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_fixed_host_wrapper_executes_stdin_consumer_and_writes_only_terminal_journal(
        self,
    ) -> None:
        self._exercise_fixed_host_wrapper()

    def test_fixed_host_wrapper_rolls_back_clean_refusal_and_finishes_its_journal(self) -> None:
        self.env.ref.side_effect = ValueError("private consumer refusal")
        self._exercise_fixed_host_wrapper(consumer_refuses=True)
        self.env.cr.rollback.assert_called_once()
        self.env.cr.commit.assert_not_called()

    def test_fixed_host_wrapper_preserves_unknown_when_commit_result_is_ambiguous(self) -> None:
        self.env.cr.commit.side_effect = RuntimeError("private ambiguous commit")
        self._exercise_fixed_host_wrapper(commit_unknown=True)

    def _exercise_fixed_host_wrapper(
        self, *, consumer_refuses: bool = False, commit_unknown: bool = False
    ) -> None:
        identity = RuntimeIdentity(
            product="example-odoo",
            context="example",
            instance="testing",
            deployment_record_id="deployment",
            artifact_id="artifact",
            source_git_ref="a" * 40,
            image_reference="example@sha256:" + "b" * 64,
        )
        payload = {
            **self.request,
            "database": "testing",
            "project": "fixture",
            "dispatch_id": "d" * 64,
            "identity": identity.model_dump(mode="json"),
            "identity_key": next(iter(runtime_identity_env(identity))),
            "image": identity.image_reference,
            "account_program": ACCOUNT_ODOO_PROGRAM,
        }
        environment = {
            **runtime_identity_env(identity),
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_NAME": "testing",
        }
        startup = SimpleNamespace(
            _load_settings=lambda args: args,
            _write_runtime_config=lambda settings: print("private startup noise"),
            _build_odoo_shell_command=lambda settings: ["odoo", "shell"],
            _odoo_environment=lambda settings: {},
        )
        original_path = Path
        output = io.StringIO()
        invocations: list[list[str]] = []

        def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            invocations.append(arguments)
            if arguments[:2] == ["docker", "ps"]:
                raw = b"fixture-runner\n"
            elif arguments[:2] == ["docker", "inspect"]:
                raw = json.dumps(
                    [
                        {
                            "Config": {
                                "Env": [key + "=" + value for key, value in environment.items()],
                                "Image": identity.image_reference,
                            }
                        }
                    ]
                ).encode()
            elif arguments[:2] == ["docker", "exec"]:
                private = io.StringIO()
                with (
                    patch.object(sys, "stdin", io.StringIO(kwargs["input"].decode())),
                    patch.object(sys, "stdout", private),
                ):
                    exec(compile(arguments[-1], "container-wrapper", "exec"), {})
                raw = private.getvalue().encode()
            else:
                exec(compile(kwargs["input"].decode(), "odoo-consumer", "exec"), {"env": self.env})
                raw = b"discarded private output"
            return subprocess.CompletedProcess(arguments, 0, stdout=raw, stderr=b"")

        with (
            patch("subprocess.run", side_effect=run),
            patch.dict(sys.modules, {"run_odoo_startup": startup}),
            patch.object(sys, "path", sys.path.copy()),
            patch.object(sys, "stdout", output),
            patch.object(
                sys,
                "argv",
                ["host", base64.b64encode(json.dumps(payload).encode()).decode(), "FRAME:"],
            ),
            patch(
                "pathlib.Path",
                side_effect=lambda path: (
                    original_path(self.directory.name) / "journal"
                    if path == WALKTHROUGH_JOURNAL_DIRECTORY
                    else original_path(path)
                ),
            ),
        ):
            exec(compile(ACCOUNT_HOST_PROGRAM, "account-host", "exec"), {})
        self.assertNotIn(self.request["password"], output.getvalue())
        self.assertNotIn("private startup noise", output.getvalue())
        self.assertNotIn("private consumer refusal", output.getvalue())
        self.assertNotIn("private ambiguous commit", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue().removeprefix("FRAME:"))["status"],
            "unknown" if commit_unknown else "fail" if consumer_refuses else "pass",
        )
        self.assertTrue(
            json.loads(
                (original_path(self.directory.name) / "journal" / ("d" * 64 + ".json")).read_text()
            )["finished"]
        )
        self.assertTrue(any(arguments[:2] == ["docker", "exec"] for arguments in invocations))
        self.assertTrue(
            all(
                self.request["password"] not in argument
                for arguments in invocations
                for argument in arguments
            )
        )
        from control_plane.dokploy.odoo_walkthrough import ACCOUNT_JOURNAL_READ_PROGRAM

        def read_journal() -> bool:
            receipt = io.StringIO()
            with (
                patch.object(sys, "stdout", receipt),
                patch.object(
                    sys,
                    "argv",
                    [
                        "host",
                        base64.b64encode(
                            json.dumps(
                                {"dispatch_id": payload["dispatch_id"], "mode": "quiescence"}
                            ).encode()
                        ).decode(),
                        "FRAME:",
                    ],
                ),
                patch(
                    "pathlib.Path",
                    side_effect=lambda path: (
                        original_path(self.directory.name) / "journal"
                        if path == WALKTHROUGH_JOURNAL_DIRECTORY
                        else original_path(path)
                    ),
                ),
            ):
                exec(compile(ACCOUNT_JOURNAL_READ_PROGRAM, "journal-reader", "exec"), {})
            return json.loads(receipt.getvalue().removeprefix("FRAME:"))["clear"] is True

        self.assertEqual(read_journal(), not commit_unknown)
        journal = (
            original_path(self.directory.name) / "journal" / (payload["dispatch_id"] + ".json")
        )
        journal.write_text(json.dumps({"finished": False}))
        self.assertFalse(read_journal())
        journal.unlink()
        self.assertFalse(read_journal())
