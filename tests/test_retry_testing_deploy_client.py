"""The supported client keeps an exact testing artifact and stable service request."""

import contextlib
import io
import json
from email.message import Message
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope,
    OdooTargetReplacementApplyOperationActiveError,
    OdooTargetReplacementApplyCurrentArtifactChangedError,
    enqueue_odoo_target_replacement_apply_operation,
)
from control_plane.odoo_target_replacement_plan_http import ODOO_TARGET_REPLACEMENT_PLAN_ROUTE
from control_plane.product_reconcile import TESTING_DEPLOY_MAX_FAILED_ATTEMPTS
from tests import test_product_reconcile as fixtures
from tests.support.durable_operations import durable_operation_authorization_payload


class TestingRetryClientTests(fixtures.ProductReconcileTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.github.add_run(20, fixtures.DEPLOYABLE)
        self.request()
        self.auto_operation_id = str(self.reconcile()["queued_operation_id"])
        self.current_artifact = "artifact-before-retry"
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context="cm",
                instance="testing",
                source_git_ref=fixtures.DEPLOYABLE,
                artifact_identity=ArtifactIdentityReference(artifact_id=self.current_artifact),
                deploy=DeploymentEvidence(
                    target_name="fixture", target_type="compose", deploy_mode="image", status="pass"
                ),
                deployment_record_id="previous-deploy",
                updated_at="2026-10-17T12:00:00Z",
            )
        )
        self.desired = {
            "target": "testing",
            "context": "cm",
            "desired_artifact_id": "artifact-cm-run-20-1",
            "desired_commit": fixtures.DEPLOYABLE,
            "current_artifact_id": self.current_artifact,
        }
        self.client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/retry-testing-deploy.py")
        )
        self.plan: dict[str, Any] = {
            "plan_status": "ready",
            "product": "site",
            "context": "cm",
            "instance": "testing",
            "strategy": "recreate-in-place",
            "target_record_found": True,
            "target_id_record_found": True,
            "inventory_found": True,
            "expected_next_target_name": "private-provider-target",
            "expected_artifact_id": "artifact-cm-run-20-1",
            "expected_source_git_ref": fixtures.DEPLOYABLE,
        }
        self.hold: dict[str, Any] = {
            "product": "site",
            "context": "cm",
            "instance": "testing",
            "hold": None,
            "record_sha256": "test-record",
        }
        self.calls: list[dict[str, Any]] = []
        self.apply_error: Exception | None = None
        self.mutate_receipt = False

    def finish(self, operation_id: str, status: str) -> None:
        operation = self.store.read_odoo_stable_target_replacement_operation_record(operation_id)
        self.store.write_odoo_stable_target_replacement_operation_record(
            OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **operation.model_dump(mode="json"),
                    "status": status,
                    "phase": "failed",
                    "finished_at": "2026-09-30T12:00:00Z",
                    "error_message": "deploy failed",
                }
            )
        )

    def post(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs["path"] == ODOO_TARGET_REPLACEMENT_PLAN_ROUTE:
            return {
                "status": "accepted",
                "trace_id": "launchplane_req_fixture",
                "result": self.plan,
            }
        self.assertEqual(kwargs["path"], ODOO_TARGET_REPLACEMENT_APPLY_ROUTE)
        if self.apply_error:
            raise self.apply_error
        try:
            records, operation = self.enqueue(kwargs)
        except (
            OdooTargetReplacementApplyOperationActiveError,
            OdooTargetReplacementApplyCurrentArtifactChangedError,
        ) as error:
            http_error = HTTPError("https://fixture.invalid", 409, "conflict", Message(), None)
            self.addCleanup(http_error.close)
            raise http_error from error
        if self.mutate_receipt:
            operation["instance"] = "prod"
        return {
            "status": "accepted",
            "trace_id": "launchplane_req_fixture",
            "records": records,
            "result": operation,
        }

    def enqueue(self, kwargs: dict[str, Any]) -> tuple[Any, Any]:
        return enqueue_odoo_target_replacement_apply_operation(
            record_store=self.store,
            request=OdooTargetReplacementApplyEnvelope.model_validate(kwargs["body"]),
            context="cm",
            idempotency_key=kwargs["idempotency_key"],
            idempotency_scope="operator",
            request_fingerprint=json.dumps(kwargs["body"], sort_keys=True),
            created_at="2026-10-17T12:00:00Z",
            authorization=DurableOperationAuthorization.model_validate(
                durable_operation_authorization_payload(
                    action="odoo_target_replacement_apply.execute",
                    managed_rule_id="site-testing",
                    product="site",
                    context="cm",
                    instances=("testing",),
                )
            ),
        )

    def read(self, *, path: str, query: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        if path.endswith("/reconcile-requests"):
            return {
                "product": "site",
                "requests": [{"target_kind": "testing", "last_plan": self.desired}],
            }
        return {"result": self.hold}

    def invoke(
        self,
        *,
        apply: bool = True,
        artifact: str = "artifact-cm-run-20-1",
        source: str = fixtures.DEPLOYABLE,
    ) -> tuple[int, dict[str, Any]]:
        transport = {
            "resolve_settings": lambda _: {"service_url": "https://fixture.invalid", "token": "t"},
            "validate_service_url": lambda _: None,
            "request_launchplane_read": self.read,
            "request_launchplane": self.post,
            "public_trace_id": lambda value: value,
            "emit_http_error_payload": lambda **kwargs: print(
                json.dumps({"status": "denied", "code": kwargs["exc"].code})
            ),
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            argv = [
                "client",
                "--operator-helper",
                str(helper),
                "--product",
                "site",
                "--context",
                "cm",
                "--artifact-id",
                artifact,
                "--source-commit",
                source,
                "--idempotency-key",
                "retry-after-fix",
            ]
            output = io.StringIO()
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", argv + (["--apply"] if apply else [])),
                contextlib.redirect_stdout(output),
            ):
                code = self.client["main"]()
            self.assertNotIn("private-provider-target", output.getvalue())
            return code, json.loads(output.getvalue())

    def test_retry_after_exhaustion_uses_same_artifact_without_rebuild_and_replays(self) -> None:
        for attempt in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
            self.finish(self.auto_operation_id, "fail")
            if attempt + 1 < TESTING_DEPLOY_MAX_FAILED_ATTEMPTS:
                self.request()
                self.auto_operation_id = str(self.reconcile()["queued_operation_id"])
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            self.assertEqual(self.run_once().state, "failed")
        code, first = self.invoke()
        self.assertEqual((code, first["status"]), (0, "accepted"))
        code, repeated = self.invoke()
        self.assertEqual((code, repeated["operation_id"]), (0, first["operation_id"]))
        retry = self.store.read_odoo_stable_target_replacement_operation_record(
            first["operation_id"]
        )
        self.assertEqual(retry.request.artifact_id, self.plan["expected_artifact_id"])
        self.assertEqual(retry.request.source_git_ref, fixtures.DEPLOYABLE)
        self.assertEqual(retry.request.data_source_mode, "existing")
        self.assertFalse(retry.request.allow_empty_data)
        self.assertEqual(
            len(self.store.list_odoo_stable_target_replacement_operation_records()),
            TESTING_DEPLOY_MAX_FAILED_ATTEMPTS + 1,
        )

    def test_rerunning_push_build_admits_new_attempt_without_new_source_commit(self) -> None:
        self.finish(self.auto_operation_id, "fail")
        self.github.runs[20]["run_attempt"] = 2
        self.request()
        plan = self.reconcile()
        retry = self.store.read_odoo_stable_target_replacement_operation_record(
            str(plan["queued_operation_id"])
        )
        self.assertEqual(retry.request.source_git_ref, fixtures.DEPLOYABLE)
        self.assertNotEqual(retry.request.artifact_id, self.plan["expected_artifact_id"])
        artifact = self.store.read_artifact_manifest(retry.request.artifact_id)
        assert artifact.source_build is not None
        self.assertEqual(artifact.source_build.run_attempt, self.github.runs[20]["run_attempt"])

    def test_plan_does_not_apply(self) -> None:
        self.assertEqual(self.invoke(apply=False)[1]["status"], "ready")
        self.assertEqual(
            [call["path"] for call in self.calls], [ODOO_TARGET_REPLACEMENT_PLAN_ROUTE]
        )

    def test_held_or_unknown_lane_never_posts(self) -> None:
        for hold, status in (({"reason": "staff testing"}, "held"), ("invalid", "unverified")):
            with self.subTest(hold=hold):
                self.hold["hold"] = hold
                code, result = self.invoke()
                self.assertEqual((code, result["status"]), (1, status))
                self.assertFalse(self.calls)

    def test_blocked_or_mismatched_plan_never_applies(self) -> None:
        original = dict(self.plan)
        for update in (
            {
                "plan_status": "blocked",
                "blocker_codes": ["config_parameter_secret_reference_disagrees"],
            },
            {"instance": "prod"},
            {"expected_artifact_id": "another-artifact"},
        ):
            with self.subTest(update=update):
                self.plan = original | update
                self.calls.clear()
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(len(self.calls), 1)

    def test_denial_is_reported_once_without_another_route(self) -> None:
        self.apply_error = HTTPError("https://fixture.invalid", 403, "denied", Message(), None)
        self.addCleanup(self.apply_error.close)
        self.assertEqual(self.invoke(), (1, {"status": "denied", "code": 403}))
        self.assertEqual(len(self.calls), 2)

    def test_lost_apply_response_is_unknown_and_never_retried(self) -> None:
        self.apply_error = TimeoutError("provider-private-text")
        code, result = self.invoke()
        self.assertEqual((code, result["status"]), (1, "outcome_unknown"))
        self.assertEqual(len(self.calls), 2)

    def test_mismatched_receipt_is_not_success(self) -> None:
        self.finish(self.auto_operation_id, "fail")
        self.mutate_receipt = True
        self.assertEqual(self.invoke()[1]["status"], "outcome_unknown")

    def test_existing_active_attempt_is_not_bypassed(self) -> None:
        # Exercise the real enqueue fence through this client's transport.
        self.assertEqual(self.invoke(), (1, {"status": "denied", "code": 409}))
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_stale_or_unverified_desired_artifact_never_posts(self) -> None:
        original = dict(self.desired)
        for update in (
            {"desired_artifact_id": "newer-artifact"},
            {"desired_commit": fixtures.OLDER},
            {"current_artifact_id": ""},
            {"context": "another-context"},
        ):
            with self.subTest(update=update):
                self.desired = original | update
                self.calls.clear()
                self.assertEqual(self.invoke()[0], 1)
                self.assertFalse(self.calls)

    def test_inventory_change_after_preflight_is_http_conflict(self) -> None:
        self.finish(self.auto_operation_id, "fail")
        self.desired["current_artifact_id"] = "stale-inventory-artifact"
        self.assertEqual(self.invoke(), (1, {"status": "denied", "code": 409}))
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_invalid_artifact_source_are_parser_errors(self) -> None:
        for artifact, source in (("", fixtures.DEPLOYABLE), ("artifact", "")):
            with (
                self.subTest(artifact=artifact, source=source),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke(artifact=artifact, source=source)
                self.assertEqual(raised.exception.code, 2)
