from control_plane.contracts.odoo_target_replacement_failures import TESTING_RESTORE_REQUIRED_CODE
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api
from control_plane.contracts.odoo_testing_source import (
    NATIVE_TESTING_SOURCE_BOUND_CHECKPOINT,
    NATIVE_TESTING_COPY_ARTIFACT_BYTES,
)
from control_plane.dokploy.post_deploy import _build_dokploy_data_workflow_schedule_app_name
from control_plane.launchplane_reconcile_authorization import (
    build_launchplane_reconcile_authorization,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import LaunchplaneProductProfileRow, PostgresRecordStore
from control_plane.testing_replacement_recovery import (
    TestingRecoveryStore,
    guard_testing_replacement,
    inspect_testing_replacement,
    recover_stranded_testing_replacements,
    testing_replacement_recovery_read,
)
from control_plane.testing_legacy_replacement_inspection import inspect_legacy_replacement
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerStore,
    _execute_target_replacement_operation,
)
from control_plane.workflows.public_ingress_monitor import run_public_ingress_monitor_once
from control_plane.workflows.odoo_generic_web_post_deploy import (
    post_deploy_evidence_from_odoo_result,
)
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployResult,
    _prefix_post_deploy_readback_evidence,
)
from tests.test_product_reconcile import _profile, _inventory
from typing import cast


def stamp(offset: int = 0) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=offset))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class _FilesystemServiceStore(FilesystemRecordStore):
    """The file storage rehearsal supplies a service-owned reconcile wake callback."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reconcile_requests: list[object] = []

    def request_product_reconcile(self, target: object, requested_at: str) -> object:
        self.reconcile_requests.append(target)
        return target

    def list_product_reconcile_requests(self, *, product: str) -> list[object]:
        return self.reconcile_requests


class TestingReplacementRecoveryTests(unittest.TestCase):
    database_url = ""

    def test_queued_maintenance_rechecks_restore_proof_before_execution(self) -> None:
        from control_plane.contracts.deployment_record import ResolvedTargetEvidence
        from control_plane.contracts.promotion_record import (
            DeploymentEvidence,
            PostDeployUpdateEvidence,
        )

        for store, root in self.stores():
            operation = self.running(store)
            # A governed restore can fail after this automatic request was queued.
            getattr(store, "write_deployment_record")(
                DeploymentRecord(
                    record_id="restore-after-queue",
                    context=operation.context,
                    instance=operation.instance,
                    source_git_ref="fixture-source",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose", target_id="compose-123", target_name="fixture"
                    ),
                    deploy=DeploymentEvidence(
                        target_name="fixture",
                        target_type="compose",
                        deploy_mode="recreate-in-place",
                        status="fail",
                        started_at=stamp(-5),
                        finished_at=stamp(),
                    ),
                    post_deploy_update=PostDeployUpdateEvidence(
                        attempted=True, status="fail", evidence={"workflow_intent": "restore"}
                    ),
                )
            )
            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply"
                ) as execute,
                self.assertLogs(level="ERROR"),
            ):
                self.assertTrue(
                    _execute_target_replacement_operation(
                        record_store=cast(OdooStableOperationWorkerStore, store),
                        control_plane_root_path=root,
                        operation=operation,
                        lease_owner="worker",
                        lease_seconds=600,
                        heartbeat_seconds=60,
                    )
                )
            execute.assert_not_called()
            failed = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            self.assertEqual(failed.error_code, TESTING_RESTORE_REQUIRED_CODE)
            self.assertEqual(failed.status, "fail")
            self.assertIsNone(failed.recovery_journal)
            from control_plane.operation_status_read import operation_status_read_view
            from control_plane.contracts.odoo_target_replacement_failures import (
                deploy_failure_description,
            )

            view = operation_status_read_view(failed, poll_url="/fixture")
            self.assertTrue(view["error_description"])
            self.assertEqual(
                view["error_description"], deploy_failure_description(failed.error_code)
            )

    def test_execution_failure_survives_fence_and_automatic_cancellation(self) -> None:
        from control_plane.operation_status_read import (
            operation_status_read_view,
            operation_result_read_view,
        )
        from control_plane.product_reconcile import _testing_failure_reason

        for store, root in self.stores():
            operation = self.running(store)
            result = OdooStableTargetReplacementApplyResult(
                product=operation.product,
                context=operation.context,
                instance=operation.instance,
                strategy="recreate-in-place",
                deploy_status="fail",
                post_deploy_status="fail",
                deployment_record_id="failed-deployment",
                error_code="post_deploy_failed",
                error_message="Private provider detail SECRET",
            )

            def execute(**_: object) -> OdooStableTargetReplacementApplyResult:
                api.dokploy_request(
                    host="https://provider.test",
                    token="fixture-token",
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": "compose-123"},
                )
                return result

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                    side_effect=execute,
                ),
                patch.object(api, "_dokploy_request", return_value={}),
            ):
                self.assertTrue(
                    _execute_target_replacement_operation(
                        record_store=cast(OdooStableOperationWorkerStore, store),
                        control_plane_root_path=root,
                        operation=operation,
                        lease_owner="worker",
                        lease_seconds=600,
                        heartbeat_seconds=60,
                    )
                )
            held = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            self.assertEqual(held.status, "reconciliation_required")
            self.assertEqual(held.deployment_record_id, result.deployment_record_id)
            assert held.execution_failure is not None
            self.assertEqual(held.execution_failure.result, result)
            with self.provider():
                self.assertEqual(
                    recover_stranded_testing_replacements(store, root), (operation.operation_id,)
                )
            cancelled = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.execution_failure, held.execution_failure)
            code, summary = _testing_failure_reason(cancelled)
            self.assertEqual(code, result.error_code)
            self.assertNotIn(result.error_message, summary)
            view = operation_status_read_view(cancelled, poll_url="/fixture")
            self.assertEqual(view["error_code"], result.error_code)
            self.assertNotEqual(
                view["error_description"],
                operation_status_read_view(
                    cancelled.model_copy(update={"cancellation": None}), poll_url="/fixture"
                )["error_description"],
            )
            self.assertNotIn("SECRET", json.dumps(view))
            assert cancelled.execution_failure is not None
            public_result = operation_result_read_view(cancelled.execution_failure.result)
            assert public_result is not None
            self.assertEqual(public_result["post_deploy_status"], result.post_deploy_status)
            import asyncio
            from control_plane.http_app import create_launchplane_fastapi_app
            from tests.http_app_test_support import (
                _asgi_get,
                _odoo_operation_status_identity,
                _odoo_operation_status_policy,
            )
            from tests.support.auth import _StubVerifier

            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(
                    action="odoo_target_replacement_apply.execute",
                    products=(operation.product,),
                    contexts=(operation.context,),
                    instances=(operation.instance,),
                    schema_version=2,
                ),
                record_store_factory=lambda: store,
            )
            response = asyncio.run(
                _asgi_get(
                    app,
                    f"/v1/drivers/odoo/target-replacement/operations/{operation.operation_id}",
                    headers={"Authorization": "Bearer valid-token"},
                )
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["result"]["post_deploy_status"], result.post_deploy_status
            )

    def test_new_isolated_target_supersedes_a_journalless_testing_fence_without_past_effect_proof(
        self,
    ) -> None:
        from tests.support.disposable_lanes import record_safety
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        for store, root in self.stores():
            self.running(store)
            fenced = self.fence(store)
            retained_target = store.read_dokploy_target_id_record(
                context_name=fenced.context, instance_name=fenced.instance
            ).target_id
            self.assertTrue(
                getattr(store, "prepare_disposable_testing_replacement")(fenced, "director-fixture")
            )
            fenced = store.read_odoo_stable_target_replacement_operation_record(fenced.operation_id)
            self.assertIsNone(fenced.recovery_journal)
            getattr(store, "write_dokploy_target_id_record")(
                DokployTargetIdRecord(
                    context="cm",
                    instance="testing",
                    target_id="new-isolated-compose",
                    updated_at=stamp(),
                )
            )
            global_config = RuntimeEnvironmentRecord(
                scope="global",
                env={"DATABASE_HOST": "fixture-rebuild.invalid"},
                updated_at=stamp(),
            )
            getattr(store, "write_runtime_environment_record")(global_config)
            record_safety(
                store,
                "site",
                "cm",
                "testing",
                replaces_operation_id=fenced.operation_id,
                replaced_target_id=retained_target,
            )
            getattr(store, "write_runtime_environment_record")(
                global_config.model_copy(update={"env": {"DATABASE_HOST": "fixture-other.invalid"}})
            )
            self.assertFalse(getattr(store, "supersede_disposable_testing_replacement")(fenced))
            record_safety(
                store,
                "site",
                "cm",
                "testing",
                replaces_operation_id=fenced.operation_id,
                replaced_target_id=retained_target,
            )
            with (
                patch(
                    "control_plane.testing_replacement_recovery.inspect_testing_replacement"
                ) as historical,
                patch(
                    "control_plane.testing_replacement_recovery.source.read_dokploy_config"
                ) as provider,
            ):
                self.assertEqual(
                    recover_stranded_testing_replacements(store, root), (fenced.operation_id,)
                )
                historical.assert_not_called()
                provider.assert_not_called()
            replaced = store.read_odoo_stable_target_replacement_operation_record(
                fenced.operation_id
            )
            self.assertEqual(replaced.status, "superseded")
            self.assertEqual(replaced.request, fenced.request)
            self.assertIsNone(replaced.cancellation)
            self.assertIsNone(replaced.recovery_journal)
            self.assertIsNotNone(replaced.disposable_supersession)
            assert replaced.disposable_supersession is not None
            self.assertEqual(replaced.disposable_supersession.retained_target_id, retained_target)
            self.assertEqual(
                replaced.disposable_supersession.replacement_target_id, "new-isolated-compose"
            )
            next_operation = fenced.model_copy(
                update={
                    "operation_id": "new-rebuild",
                    "idempotency_key": "fresh-rebuild",
                    "status": "pending",
                    "phase": "created",
                    "started_at": "",
                    "attempt": 0,
                    "error_code": "",
                    "error_message": "",
                }
            )
            _, created = getattr(
                store, "create_odoo_stable_target_replacement_operation_record_if_no_active_lane"
            )(next_operation)
            self.assertTrue(created)

    def test_disposable_supersession_refuses_same_target_production_and_protected_data(
        self,
    ) -> None:
        from tests.support.disposable_lanes import record_safety

        for store, _ in self.stores():
            self.running(store)
            fenced = self.fence(store)
            retained_target = store.read_dokploy_target_id_record(
                context_name=fenced.context, instance_name=fenced.instance
            ).target_id
            self.assertTrue(
                getattr(store, "prepare_disposable_testing_replacement")(fenced, "director-fixture")
            )
            fenced = store.read_odoo_stable_target_replacement_operation_record(fenced.operation_id)
            supersede = getattr(store, "supersede_disposable_testing_replacement")
            record_safety(
                store,
                "site",
                "cm",
                "testing",
                replaces_operation_id=fenced.operation_id,
                replaced_target_id=retained_target,
            )
            self.assertFalse(supersede(fenced))
            getattr(store, "write_dokploy_target_id_record")(
                DokployTargetIdRecord(
                    context="cm",
                    instance="testing",
                    target_id="new-isolated-compose",
                    updated_at=stamp(),
                )
            )
            for changes in (
                {"usage": "in_use"},
                {"data": "only_copy"},
                {"production_resources": "unknown"},
                {"replaced_target_id": "invented-retained-target"},
            ):
                replacement_identity = changes.get("replaced_target_id", retained_target)
                record_safety(
                    store,
                    "site",
                    "cm",
                    "testing",
                    replaces_operation_id=fenced.operation_id,
                    replaced_target_id=replacement_identity,
                    **{key: value for key, value in changes.items() if key != "replaced_target_id"},
                )
                self.assertFalse(supersede(fenced))
                self.assertEqual(
                    store.read_odoo_stable_target_replacement_operation_record(fenced.operation_id),
                    fenced,
                )
            record_safety(
                store,
                "site",
                "cm",
                "testing",
                replaces_operation_id=fenced.operation_id,
                replaced_target_id=retained_target,
            )
            with store.testing_replacement_execution_lock(fenced.operation_id) as lock:
                self.assertIsNotNone(lock)
                self.assertFalse(supersede(fenced))
            production = fenced.model_copy(
                update={
                    "instance": "prod",
                    "request": fenced.request.model_copy(update={"instance": "prod"}),
                    "authorization": None,
                    "schema_version": 1,
                }
            )
            getattr(store, "write_odoo_stable_target_replacement_operation_record")(production)
            self.assertFalse(supersede(production))
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(fenced.operation_id),
                production,
            )

    def test_legacy_read_observes_retained_target_and_schedules_without_release(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            operation = operation.model_copy(
                update={
                    "request": operation.request.model_copy(
                        update={"artifact_id": "original-artifact", "source_git_ref": "a" * 40}
                    )
                }
            )
            getattr(store, "write_odoo_stable_target_replacement_operation_record")(operation)
            deployment = DeploymentRecord.model_validate(
                {
                    "record_id": "original-pending",
                    "context": operation.context,
                    "instance": operation.instance,
                    "source_git_ref": operation.request.source_git_ref,
                    "artifact_identity": {"artifact_id": operation.request.artifact_id},
                    "resolved_target": {
                        "target_type": "compose",
                        "target_id": "compose-123",
                        "target_name": "private-target-name",
                    },
                    "deploy": {
                        "deploy_mode": "image",
                        "target_name": "private-target-name",
                        "status": "pending",
                        "target_type": "compose",
                        "started_at": operation.started_at,
                    },
                }
            )
            getattr(store, "write_deployment_record")(deployment)
            # A busy lane's unrelated build history must not hide this attempt.
            assert deployment.artifact_identity is not None
            for index in range(80):
                unrelated = deployment.model_copy(
                    update={
                        "record_id": f"unrelated-{index}",
                        "source_git_ref": "b" * 40 if index % 2 else deployment.source_git_ref,
                        "artifact_identity": deployment.artifact_identity
                        if index % 2
                        else deployment.artifact_identity.model_copy(
                            update={"artifact_id": f"other-artifact-{index}"}
                        ),
                    }
                )
                getattr(store, "write_deployment_record")(unrelated)
            fenced = self.fence(store)
            default_method = signature(api.dokploy_request).parameters["method"].default
            for schedule_status, enabled, expected_history, expected_timer in (
                ("done", False, "terminal", "disabled"),
                ("running", False, "active", "disabled"),
                ("unexpected-status", False, "unknown", "disabled"),
                ("done", None, "terminal", "unknown"),
                ("unexpected-status", True, "unknown", "enabled"),
                ("done", "false", "terminal", "unknown"),
            ):
                with self.subTest(
                    backend=type(store).__name__, schedule_status=schedule_status, enabled=enabled
                ):
                    calls: list[str] = []

                    def read(**kwargs: object) -> api.JsonValue:
                        self.assertEqual(kwargs.get("method", default_method), "GET")
                        self.assertGreater(float(cast(float, kwargs["timeout_seconds"])), 0)
                        self.assertLessEqual(float(cast(float, kwargs["timeout_seconds"])), 5)
                        path = str(kwargs["path"])
                        calls.append(path)
                        if path == "/api/compose.one":
                            return {"composeId": "compose-123", "serverId": "private-server"}
                        if path == "/api/schedule.list":
                            unrelated_schedules: list[api.JsonValue] = [
                                {
                                    "scheduleId": f"unrelated-schedule-{index}",
                                    "appName": f"other-lane-{index}",
                                    "enabled": True,
                                }
                                for index in range(80)
                            ]
                            return unrelated_schedules + [
                                {
                                    "scheduleId": "private-schedule",
                                    "appName": _build_dokploy_data_workflow_schedule_app_name(
                                        context_name=operation.context,
                                        instance_name=operation.instance,
                                    ),
                                    "enabled": enabled,
                                }
                            ]
                        query = cast(dict[str, str], kwargs["query"])
                        if query["type"] == "schedule":
                            return [{"deploymentId": "job", "status": schedule_status}]
                        return [{"deploymentId": "old-deploy", "status": "done"}]

                    diagnostics: list[str] = []
                    with (
                        patch(
                            "control_plane.testing_legacy_replacement_inspection.source.read_dokploy_config",
                            return_value=("https://provider.test", "fixture-token"),
                        ),
                        patch.object(api, "dokploy_request", side_effect=read),
                    ):
                        observation = inspect_legacy_replacement(
                            store, fenced, root, deadline_at=monotonic() + 15
                        )
                        self.assertEqual(observation.schedule_history, expected_history)
                        self.assertEqual(observation.schedule_timers, expected_timer)
                        view = testing_replacement_recovery_read(
                            store, fenced, root, diagnostics=diagnostics
                        )
                    self.assertIn("/api/schedule.list", calls)
                    self.assertTrue(view["provider_inspected"])
                    self.assertEqual(view["journal"], "absent")
                    self.assertEqual(view["provider_effects"], "unknown")
                    self.assertEqual(view["status"], "refused")
                    self.assertFalse(view["safe_to_release"])
                    self.assertNotIn("private", str(view) + str(diagnostics))
                    self.assertEqual(
                        store.read_odoo_stable_target_replacement_operation_record(
                            operation.operation_id
                        ),
                        fenced,
                    )
                    self.assertEqual(
                        getattr(store, "read_deployment_record")(deployment.record_id), deployment
                    )
                    self.assertFalse(
                        getattr(store, "list_product_reconcile_requests")(product="site")
                    )
            app_name = _build_dokploy_data_workflow_schedule_app_name(
                context_name=operation.context, instance_name=operation.instance
            )
            schedule_inventories: tuple[api.JsonValue, ...] = (
                [
                    {"scheduleId": f"matching-{index}", "appName": app_name, "enabled": False}
                    for index in range(80)
                ],
                [{"scheduleId": "matching", "appName": app_name}, None],
                {"items": [], "data": []},
            )
            for schedule_inventory in schedule_inventories:
                with self.subTest(backend=type(store).__name__, inventory=schedule_inventory):

                    def incomplete_read(**kwargs: object) -> api.JsonValue:
                        if kwargs["path"] == "/api/schedule.list":
                            return schedule_inventory
                        query = cast(dict[str, str], kwargs["query"])
                        self.assertNotEqual(query.get("type"), "schedule")
                        return read(**kwargs)

                    with (
                        patch(
                            "control_plane.testing_legacy_replacement_inspection.source.read_dokploy_config",
                            return_value=("https://provider.test", "fixture-token"),
                        ),
                        patch.object(api, "dokploy_request", side_effect=incomplete_read),
                    ):
                        view = testing_replacement_recovery_read(store, fenced, root)
                    self.assertFalse(view["provider_inspected"])
                    self.assertEqual(view["provider_effects"], "unknown")
                    self.assertFalse(view["safe_to_release"])
                    self.assertEqual(
                        store.read_odoo_stable_target_replacement_operation_record(
                            operation.operation_id
                        ),
                        fenced,
                    )
            getattr(store, "write_deployment_record")(
                deployment.model_copy(update={"record_id": "another-candidate"})
            )
            with patch.object(api, "dokploy_request") as provider_read:
                view = testing_replacement_recovery_read(store, fenced, root)
            provider_read.assert_not_called()
            self.assertFalse(view["provider_inspected"])
            self.assertEqual(view["status"], "refused")

    def test_legacy_read_inspects_the_retained_schedule_run_without_releasing(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            operation = operation.model_copy(
                update={
                    "request": operation.request.model_copy(
                        update={"artifact_id": "original-artifact", "source_git_ref": "a" * 40}
                    )
                }
            )
            getattr(store, "write_odoo_stable_target_replacement_operation_record")(operation)
            deployment = DeploymentRecord.model_validate(
                {
                    "record_id": "original-deployment",
                    "context": operation.context,
                    "instance": operation.instance,
                    "source_git_ref": operation.request.source_git_ref,
                    "artifact_identity": {"artifact_id": operation.request.artifact_id},
                    "resolved_target": {
                        "target_type": "compose",
                        "target_id": "compose-123",
                        "target_name": "fixture-target",
                    },
                    "deploy": {
                        "deploy_mode": "image",
                        "target_name": "fixture-target",
                        "target_type": "compose",
                        "started_at": operation.started_at,
                    },
                    "post_deploy_update": post_deploy_evidence_from_odoo_result(
                        OdooPostDeployResult(
                            context=operation.context,
                            instance=operation.instance,
                            phase="deploy",
                            post_deploy_status="pass",
                            override_evidence=_prefix_post_deploy_readback_evidence(
                                {
                                    "schedule_id": "private-original-schedule",
                                    "schedule_deployment_id": "private-original-run",
                                }
                            ),
                        )
                    ).model_dump(mode="json"),
                }
            )
            getattr(store, "write_deployment_record")(deployment)
            fenced = self.fence(store)
            for run_id, status, expected in (
                ("private-original-run", "done", "terminal"),
                ("private-original-run", "running", "active"),
                ("another-run", "done", "unknown"),
            ):
                with self.subTest(backend=type(store).__name__, run_id=run_id, status=status):
                    observed_schedules = []

                    def read(**kwargs: object) -> api.JsonValue:
                        path = kwargs["path"]
                        query = cast(dict[str, str], kwargs["query"])
                        if path == "/api/compose.one":
                            return {"composeId": "compose-123", "serverId": "fixture-server"}
                        if path == "/api/schedule.list":
                            # The immutable reference must survive a mutable name change.
                            return []
                        if path == "/api/schedule.one":
                            observed_schedules.append(query["scheduleId"])
                            return {"scheduleId": query["scheduleId"], "enabled": False}
                        if query["type"] == "schedule":
                            self.assertEqual(query["id"], "private-original-schedule")
                            return [{"deploymentId": run_id, "status": status}]
                        return [{"deploymentId": "compose-run", "status": "done"}]

                    diagnostics: list[str] = []
                    with (
                        patch(
                            "control_plane.testing_legacy_replacement_inspection.source.read_dokploy_config",
                            return_value=("https://provider.test", "fixture-token"),
                        ),
                        patch.object(api, "dokploy_request", side_effect=read),
                    ):
                        observation = inspect_legacy_replacement(
                            store, fenced, root, deadline_at=monotonic() + 15
                        )
                        self.assertEqual(observed_schedules, ["private-original-schedule"])
                        self.assertEqual(observation.retained_schedule_run, expected)
                        view = testing_replacement_recovery_read(
                            store, fenced, root, diagnostics=diagnostics
                        )
                    self.assertEqual(view["status"], "refused")
                    self.assertFalse(view["safe_to_release"])
                    self.assertEqual(view["provider_effects"], "unknown")
                    self.assertNotIn("private", str(view) + str(diagnostics))
                    self.assertEqual(
                        store.read_odoo_stable_target_replacement_operation_record(
                            operation.operation_id
                        ),
                        fenced,
                    )
                    self.assertFalse(
                        getattr(store, "list_product_reconcile_requests")(product="site")
                    )

            for changed_record in (
                deployment.model_copy(
                    update={
                        "post_deploy_update": deployment.post_deploy_update.model_copy(
                            update={"evidence": {}}
                        )
                    }
                ),
                deployment.model_copy(update={"record_id": "late-second-candidate"}),
            ):
                getattr(store, "write_deployment_record")(deployment)

                def changed_evidence_read(**kwargs: object) -> api.JsonValue:
                    result = read(**kwargs)
                    if kwargs["path"] == "/api/schedule.one":
                        getattr(store, "write_deployment_record")(changed_record)
                    return result

                with (
                    patch(
                        "control_plane.testing_legacy_replacement_inspection.source.read_dokploy_config",
                        return_value=("https://provider.test", "fixture-token"),
                    ),
                    patch.object(api, "dokploy_request", side_effect=changed_evidence_read),
                ):
                    # Unexpected exceptions fail this direct read, rather than
                    # looking like a successful fail-closed wrapper refusal.
                    observation = inspect_legacy_replacement(
                        store, fenced, root, deadline_at=monotonic() + 15
                    )
                self.assertFalse(observation.provider_inspected)

    def test_inspection_deadline_refuses_before_another_provider_read(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("preparing")
                fenced = self.fence(store)
            with (
                patch("control_plane.testing_replacement_recovery.monotonic", return_value=101),
                patch(
                    "control_plane.testing_replacement_recovery.source.read_dokploy_config",
                    return_value=("https://provider.test", "fixture-token"),
                ),
                patch.object(api, "dokploy_request") as provider_read,
                self.assertRaises(TimeoutError),
            ):
                inspect_testing_replacement(fenced, root, deadline_at=100)
            provider_read.assert_not_called()
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
                fenced,
            )

    def test_classification_invalidates_agreement_after_inspection_race(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("preparing")
                fenced = self.fence(store)

            def inspect(*args: object, **kwargs: object) -> dict[str, str]:
                getattr(store, "write_dokploy_target_id_record")(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="testing",
                        target_id="changed",
                        updated_at=stamp(),
                    )
                )
                return {"state": "quiescent_safe_to_retry"}

            with patch(
                "control_plane.testing_replacement_recovery.inspect_testing_replacement",
                side_effect=inspect,
            ):
                view = testing_replacement_recovery_read(store, fenced, root)
            self.assertEqual(view["reason"], "recovery_operation_or_binding_changed")
            self.assertIsNone(view["binding_matches"])
            self.assertEqual(view["provider_effects"], "unknown")
            self.assertFalse(view["safe_to_release"])
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
                fenced,
            )

    def test_classification_reports_persisted_absence_without_provider_read_or_retirement(
        self,
    ) -> None:
        for store, root in self.stores():
            self.running(store)
            operation = self.fence(store)
            with patch.object(api, "dokploy_request") as provider_read:
                view = testing_replacement_recovery_read(store, operation, root)
            self.assertEqual(view["journal"], "absent")
            self.assertEqual(view["reason"], "recovery_journal_absent")
            self.assertEqual(view["provider_effects"], "unknown")
            self.assertFalse(view["safe_to_release"])
            provider_read.assert_not_called()
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
                operation,
            )

    def test_classification_excludes_worker_and_reports_journal_eligibility_without_mutation(
        self,
    ) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("preparing")
                fenced = self.fence(store)
                view = testing_replacement_recovery_read(store, fenced, root)
                self.assertEqual(view["reason"], "recovery_execution_exclusion_unavailable")
            with self.provider():
                view = testing_replacement_recovery_read(store, fenced, root)
            self.assertEqual(view["status"], "eligible")
            self.assertTrue(view["attempt_matches"])
            self.assertTrue(view["binding_matches"])
            self.assertEqual(view["provider_effects"], "quiescent_observed")
            self.assertFalse(view["safe_to_release"])
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
                fenced,
            )
            self.assertNotIn("compose-123", str(view))
            self.assertFalse(getattr(store, "list_product_reconcile_requests")(product="site"))

    def test_classification_refuses_changed_binding_unacknowledged_and_unknown_effects(
        self,
    ) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("preparing")
                fenced = self.fence(store)
            assert fenced.recovery_journal is not None
            from control_plane.contracts.testing_replacement_recovery import (
                TestingReplacementEffect,
            )

            variants = (
                (
                    fenced.recovery_journal.model_copy(update={"attempt": 2}),
                    "recovery_attempt_changed",
                ),
                (
                    fenced.recovery_journal.model_copy(update={"target_fingerprint": "changed"}),
                    "recovery_binding_changed",
                ),
                (
                    fenced.recovery_journal.model_copy(
                        update={
                            "effects": (
                                TestingReplacementEffect(
                                    reference="private",
                                    path="/api/compose.deploy",
                                    dispatched_at=stamp(),
                                ),
                            )
                        }
                    ),
                    "recovery_dispatch_unacknowledged",
                ),
                (
                    fenced.recovery_journal.model_copy(
                        update={
                            "effects": (
                                TestingReplacementEffect(
                                    reference="private",
                                    path="records_commit",
                                    dispatched_at=stamp(),
                                    acknowledged=True,
                                ),
                            )
                        }
                    ),
                    "recovery_effect_unsupported",
                ),
            )
            for journal, reason in variants:
                changed = fenced.model_copy(update={"recovery_journal": journal})
                getattr(store, "write_odoo_stable_target_replacement_operation_record")(changed)
                with patch.object(api, "dokploy_request") as provider_read:
                    view = testing_replacement_recovery_read(store, changed, root)
                self.assertEqual(view["reason"], reason)
                provider_read.assert_not_called()
                self.assertEqual(
                    store.read_odoo_stable_target_replacement_operation_record(
                        operation.operation_id
                    ),
                    changed,
                )
                self.assertNotIn("private", str(view))

    def test_recovery_preserves_stored_preview_transport_compatibility(self) -> None:
        for mode, command in (("none", ""), ("bootstrap", "/app/seed")):
            for store, root in self.stores():
                with self.subTest(mode=mode, backend=type(store).__name__):
                    profile = store.read_product_profile_record("site")
                    payload = profile.model_dump(mode="json")
                    payload["preview"].update(
                        data_transport_mode=mode, migration_command="", seed_command=command
                    )
                    if isinstance(store, FilesystemRecordStore):
                        path = store.write_product_profile_record(profile)
                        path.write_text(json.dumps(payload))
                    else:
                        assert isinstance(store, PostgresRecordStore)
                        with store._session_factory() as session:
                            row = session.get(LaunchplaneProductProfileRow, profile.product)
                            assert row is not None
                            row.payload = payload
                            session.commit()
                    operation = self.running(store)
                    with guard_testing_replacement(store, operation, "worker", lambda _: None):
                        with patch.object(api, "_dokploy_request", return_value={}):
                            api.dokploy_request(
                                host="https://provider.test",
                                token="fixture-token",
                                path="/api/compose.update",
                                method="POST",
                                payload={"composeId": "compose-123", "env": "FIXTURE=value"},
                            )
                        self.fence(store)
                    with self.provider():
                        self.assertEqual(
                            recover_stranded_testing_replacements(store, root),
                            (operation.operation_id,),
                        )
                    self.assertEqual(
                        store.read_odoo_stable_target_replacement_operation_record(
                            operation.operation_id
                        ).status,
                        "cancelled",
                    )
                    historical = store.read_product_profile_record(
                        "site"
                    ).preview.historical_transport
                    if command:
                        self.assertEqual(historical["seed_command"], command)
                        self.assertEqual(historical["data_transport_mode"], mode)
                    else:
                        self.assertFalse(historical)

    def stores(self) -> Iterator[tuple[TestingRecoveryStore, Path]]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            postgres = PostgresRecordStore(
                database_url=self.database_url or f"sqlite+pysqlite:///{root / 'records.sqlite'}"
            )
            postgres.ensure_schema()
            self.addCleanup(postgres.close)
            for store in (postgres, _FilesystemServiceStore(root / "state")):
                store.write_repository_inventory_record(_inventory())
                payload = _profile()
                payload["lanes"] = [
                    {"context": "cm", "instance": "testing", "base_url": "https://example.test"}
                ]
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(payload)
                )
                store.write_dokploy_target_record(
                    DokployTargetRecord(context="cm", instance="testing", updated_at=stamp())
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="testing",
                        target_id="compose-123",
                        updated_at=stamp(),
                    )
                )
                yield cast(TestingRecoveryStore, store), root

    def running(self, store: TestingRecoveryStore) -> OdooStableTargetReplacementOperationRecord:
        operation = OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id="op-testing",
            product="site",
            context="cm",
            instance="testing",
            idempotency_key="launchplane-reconcile:site:cm:testing:artifact",
            idempotency_scope="launchplane-reconcile:site",
            request_fingerprint="request",
            request=OdooStableTargetReplacementApplyRequest(
                product="site",
                instance="testing",
                strategy="recreate-in-place",
                data_source_mode="existing",
                allow_empty_data=True,
            ),
            authorization=build_launchplane_reconcile_authorization(
                product="site", context="cm", authorized_at=stamp()
            ),
            status="running",
            phase="running",
            created_at=stamp(),
            updated_at=stamp(),
            started_at=stamp(),
            lease_owner="worker",
            lease_expires_at=stamp(600),
            heartbeat_at=stamp(),
            attempt=1,
        )
        getattr(store, "write_odoo_stable_target_replacement_operation_record")(operation)
        return operation

    def fence(self, store: TestingRecoveryStore) -> OdooStableTargetReplacementOperationRecord:
        current = store.read_odoo_stable_target_replacement_operation_record("op-testing")
        getattr(store, "write_odoo_stable_target_replacement_operation_record")(
            current.model_copy(update={"lease_expires_at": stamp(-1)})
        )
        getattr(store, "recover_expired_odoo_stable_target_replacement_operation_records")(
            now=stamp(), safe_phases=("created",), max_attempts=3
        )
        fenced = store.read_odoo_stable_target_replacement_operation_record("op-testing")
        self.assertEqual(fenced.status, "reconciliation_required")
        return fenced

    @contextmanager
    def provider(self, history: Sequence[api.JsonObject] | None = None) -> Iterator[None]:
        def read(**kwargs: object) -> api.JsonValue:
            if kwargs["path"] == "/api/compose.one":
                return {"composeId": "compose-123"}
            if kwargs["path"] == "/api/deployment.allByType":
                return [cast(api.JsonValue, item) for item in (history or [])]
            raise AssertionError(f"Unexpected provider read: {kwargs['path']}")

        with (
            patch(
                "control_plane.testing_replacement_recovery.source.read_dokploy_config",
                return_value=("https://provider.test", "fixture-token"),
            ),
            patch.object(api, "dokploy_request", side_effect=read),
        ):
            yield

    def test_recovery_excludes_surviving_worker_then_cancels_with_proof_and_wakes_reconcile(
        self,
    ) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                with patch.object(api, "_dokploy_request", return_value={}):
                    api.dokploy_request(
                        host="https://provider.test",
                        token="fixture-token",
                        path="/api/compose.update",
                        method="POST",
                        payload={"composeId": "compose-123", "env": "SECRET=fixture"},
                    )
                fenced = self.fence(store)
                with self.provider():
                    self.assertEqual(recover_stranded_testing_replacements(store, root), ())
                with patch.object(api, "_dokploy_request") as dispatch:
                    with self.assertRaises(RuntimeError):
                        api.dokploy_request(
                            host="https://provider.test",
                            token="fixture-token",
                            path="/api/compose.update",
                            method="POST",
                            payload={"composeId": "compose-123"},
                        )
                    dispatch.assert_not_called()
            with self.provider():
                self.assertEqual(
                    recover_stranded_testing_replacements(store, root), (operation.operation_id,)
                )
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())
            cancelled = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            self.assertEqual(cancelled.status, "cancelled")
            assert cancelled.cancellation is not None
            assert cancelled.cancellation.reconciliation_attestation is not None
            self.assertGreaterEqual(
                cancelled.cancellation.reconciliation_attestation.provider_inspected_at,
                fenced.updated_at,
            )
            self.assertTrue(cancelled.cancellation.reconciliation_attestation.safe_to_release)
            self.assertNotIn("SECRET", cancelled.model_dump_json())
            pending = getattr(store, "list_product_reconcile_requests")(product="site")
            self.assertEqual(pending[0].target_key, "site:testing")

    def test_dispatch_intent_survives_timeout_and_cannot_be_replayed(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                with patch.object(api, "_dokploy_request", side_effect=TimeoutError):
                    with self.assertRaises(TimeoutError):
                        api.dokploy_request(
                            host="https://provider.test",
                            token="fixture-token",
                            path="/api/compose.update",
                            method="POST",
                            payload={"composeId": "compose-123"},
                        )
            fenced = self.fence(store)
            assert fenced.recovery_journal is not None
            self.assertFalse(fenced.recovery_journal.effects[0].acknowledged)
            with self.provider():
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ).status,
                "reconciliation_required",
            )

    def test_acknowledged_native_delivery_uses_one_aggregate_effect_and_recovers(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                counts = []
                with patch.object(api, "_dokploy_request", return_value={}):
                    for _ in range(3):
                        api.dokploy_request(
                            host="https://provider.test",
                            token="fixture-token",
                            path=api.NATIVE_TESTING_CHUNK_UPLOAD_PATH,
                            method="POST",
                            payload={},
                        )
                        journal = store.read_odoo_stable_target_replacement_operation_record(
                            operation.operation_id
                        ).recovery_journal
                        assert journal is not None
                        counts.append(len(journal.effects))
                        self.assertTrue(all(effect.acknowledged for effect in journal.effects))
                self.assertTrue(counts)
                self.assertEqual(min(counts), max(counts))
            fenced = self.fence(store)
            with self.provider():
                view = testing_replacement_recovery_read(store, fenced, root)
                self.assertEqual(view["status"], "eligible")
                self.assertEqual(
                    recover_stranded_testing_replacements(store, root), (operation.operation_id,)
                )

    def test_lost_native_upload_reply_keeps_aggregate_delivery_unacknowledged(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                with patch.object(
                    api, "_dokploy_request", side_effect=[{}, TimeoutError("fixture lost reply")]
                ):
                    api.dokploy_request(
                        host="https://provider.test",
                        token="fixture-token",
                        path=api.NATIVE_TESTING_CHUNK_UPLOAD_PATH,
                        method="POST",
                        payload={},
                    )
                    with self.assertRaises(TimeoutError):
                        api.dokploy_request(
                            host="https://provider.test",
                            token="fixture-token",
                            path=api.NATIVE_TESTING_CHUNK_UPLOAD_PATH,
                            method="POST",
                            payload={},
                        )
            fenced = self.fence(store)
            with self.provider():
                view = testing_replacement_recovery_read(store, fenced, root)
                self.assertFalse(view["effects_acknowledged"])
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())

    def test_native_writer_failure_acknowledges_only_its_terminal_witness(self) -> None:
        from control_plane.dokploy.native_copy_stream import (
            NativeSourceUnavailable,
            NativeUploadRejected,
        )

        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                for error in (
                    NativeUploadRejected(),
                    NativeSourceUnavailable("fixture interrupted writer"),
                ):
                    with patch(
                        "control_plane.dokploy.native_copy_stream.write_native_testing_chunk",
                        side_effect=error,
                    ):
                        with self.assertRaises(NativeSourceUnavailable):
                            api.upload_native_testing_chunk(
                                host="https://provider.test",
                                token="fixture",
                                server_id="fixture-server",
                                container_id="e" * 64,
                                filename="lp-native-" + "f" * 32 + "-database-0",
                                content=b"fixture bytes",
                            )
                    journal = store.read_odoo_stable_target_replacement_operation_record(
                        operation.operation_id
                    ).recovery_journal
                    assert journal is not None
                    self.assertEqual(
                        journal.effects[-1].acknowledged, isinstance(error, NativeUploadRejected)
                    )
            self.fence(store)
            with self.provider():
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())

    def test_determinate_source_bound_refuses_recovery_without_reading_provider(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint(NATIVE_TESTING_SOURCE_BOUND_CHECKPOINT)
            fenced = self.fence(store)
            with self.provider():
                view = testing_replacement_recovery_read(store, fenced, root)
                self.assertEqual(view["status"], "refused")
                self.assertFalse(view["provider_inspected"])
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())
                with patch(
                    "control_plane.testing_replacement_recovery.NATIVE_TESTING_COPY_ARTIFACT_BYTES",
                    NATIVE_TESTING_COPY_ARTIFACT_BYTES * 2,
                ):
                    self.assertEqual(
                        recover_stranded_testing_replacements(store, root),
                        (operation.operation_id,),
                    )

    def test_classifier_requires_exact_terminal_deploy_and_bound_provider(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                with patch.object(api, "_dokploy_request", return_value={}) as dispatch:
                    api.trigger_deployment(
                        host="https://provider.test",
                        token="fixture-token",
                        target_type="compose",
                        target_id="compose-123",
                        no_cache=False,
                    )
                    self.assertTrue(dispatch.call_args.kwargs["payload"]["title"])
            fenced = self.fence(store)
            assert fenced.recovery_journal is not None
            title = fenced.recovery_journal.effects[0].deployment_title
            histories: tuple[list[api.JsonObject], ...] = (
                [],
                [{"deploymentId": "deploy-1", "title": "other", "status": "done"}],
                [{"deploymentId": "deploy-1", "title": title, "status": "running"}],
                [{"deploymentId": "deploy-1", "title": title, "status": "unknown"}],
            )
            for history in histories:
                with self.provider(history):
                    self.assertIsNone(inspect_testing_replacement(fenced, root))
            with self.provider([{"deploymentId": "deploy-1", "title": title, "status": "done"}]):
                self.assertIsNotNone(inspect_testing_replacement(fenced, root))

    def test_missing_journal_stays_fenced_and_opens_visible_incident(self) -> None:
        for store, root in self.stores():
            self.running(store)
            operation = self.fence(store)
            with self.provider():
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())
            result = run_public_ingress_monitor_once(record_store=store, checked_at=stamp())  # type: ignore[arg-type]
            self.assertEqual(result.open_incident_count, 1)
            self.assertIn(operation.operation_id, result.records[0].summary)
            self.assertEqual(result.incidents[0].failure_code, "deploy_fence_held")

    def test_target_change_during_inspection_prevents_cancellation(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("first-write")
            self.fence(store)

            def inspect(*_: object) -> dict[str, str]:
                getattr(store, "write_dokploy_target_id_record")(
                    DokployTargetIdRecord(
                        context="cm", instance="testing", target_id="different", updated_at=stamp()
                    )
                )
                return {"state": "quiescent_safe_to_retry"}

            with patch(
                "control_plane.testing_replacement_recovery.inspect_testing_replacement",
                side_effect=inspect,
            ):
                self.assertEqual(recover_stranded_testing_replacements(store, root), ())
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ).status,
                "reconciliation_required",
            )

    def test_worker_keeps_uncertain_effect_fenced_instead_of_terminal_failure(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)

            def execute(**_: object) -> None:
                api.dokploy_request(
                    host="https://provider.test",
                    token="fixture-token",
                    path="/api/compose.deploy",
                    method="POST",
                    payload={"composeId": "compose-123"},
                )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                    side_effect=execute,
                ),
                patch.object(api, "_dokploy_request", side_effect=TimeoutError),
                self.assertLogs(level="ERROR"),
            ):
                self.assertTrue(
                    _execute_target_replacement_operation(
                        record_store=cast(OdooStableOperationWorkerStore, store),
                        control_plane_root_path=root,
                        operation=operation,
                        lease_owner="worker",
                        lease_seconds=600,
                        heartbeat_seconds=60,
                    )
                )
            self.assertEqual(
                store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ).status,
                "reconciliation_required",
            )

    def test_running_schedule_or_enabled_timer_keeps_the_fence(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(store, operation, "worker", lambda _: None):
                with patch.object(api, "_dokploy_request", return_value={}):
                    api.dokploy_request(
                        host="https://provider.test",
                        token="fixture-token",
                        path="/api/schedule.runManually",
                        method="POST",
                        payload={"scheduleId": "schedule-1"},
                    )
            fenced = self.fence(store)
            for enabled, status in (
                (True, "done"),
                (False, "running"),
                (False, "unknown"),
                (False, "done"),
            ):

                def read(**kwargs: object) -> api.JsonValue:
                    if kwargs["path"] == "/api/compose.one":
                        return {"composeId": "compose-123"}
                    if kwargs["path"] == "/api/schedule.one":
                        return {"scheduleId": "schedule-1", "enabled": enabled}
                    query = cast(dict[str, str], kwargs["query"])
                    if query["type"] == "schedule":
                        return [{"deploymentId": "schedule-deployment", "status": status}]
                    return []

                with (
                    patch(
                        "control_plane.testing_replacement_recovery.source.read_dokploy_config",
                        return_value=("https://provider.test", "fixture-token"),
                    ),
                    patch.object(api, "dokploy_request", side_effect=read),
                ):
                    proof = inspect_testing_replacement(fenced, root)
                    self.assertEqual(proof is not None, not enabled and status == "done")

    def test_other_origins_and_production_are_never_inspected_or_recovered(self) -> None:
        from tests.support.durable_operations import durable_operation_authorization_payload
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
            DurableOperationCallerIdentity,
        )

        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("first-write")
            fenced = self.fence(store)
            for instance, grant in (
                ("testing", "policy_rule"),
                ("prod", "launchplane_reconcile"),
                ("testing", "client_release_acceptance"),
            ):
                authorization = fenced.authorization
                assert authorization is not None
                if grant == "policy_rule":
                    authorization = DurableOperationAuthorization.model_validate(
                        durable_operation_authorization_payload(
                            action="odoo_target_replacement_apply.execute",
                            managed_rule_id="fixture-testing-replacement",
                            product="site",
                            context="cm",
                        )
                    )
                elif grant == "client_release_acceptance":
                    authorization = authorization.model_copy(
                        update={
                            "grant": grant,
                            "release_decision_record_id": "review-1",
                            "caller": DurableOperationCallerIdentity(
                                identity_type="github_human",
                                login="fixture-client",
                                github_id=123,
                                role="client",
                            ),
                        }
                    )
                else:
                    authorization = authorization.model_copy(update={"instances": (instance,)})
                changed = fenced.model_copy(
                    update={
                        "instance": instance,
                        "request": fenced.request.model_copy(update={"instance": instance}),
                        "authorization": authorization,
                    }
                )
                getattr(store, "write_odoo_stable_target_replacement_operation_record")(changed)
                with patch(
                    "control_plane.testing_replacement_recovery.inspect_testing_replacement"
                ) as inspect:
                    self.assertEqual(recover_stranded_testing_replacements(store, root), ())
                    inspect.assert_not_called()

    def test_publication_intent_is_not_safe_to_retry(self) -> None:
        for store, root in self.stores():
            operation = self.running(store)
            with guard_testing_replacement(
                store, operation, "worker", lambda _: None
            ) as checkpoint:
                checkpoint("target_replacement_records_commit")
            fenced = self.fence(store)
            with self.provider():
                self.assertIsNone(inspect_testing_replacement(fenced, root))
