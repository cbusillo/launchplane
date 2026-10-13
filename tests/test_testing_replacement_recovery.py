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
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api
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
                    "post_deploy_update": {
                        "attempted": True,
                        "status": "pass",
                        "evidence": {
                            "schedule_id": "private-original-schedule",
                            "schedule_deployment_id": "private-original-run",
                        },
                    },
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

            def changed_evidence_read(**kwargs: object) -> api.JsonValue:
                result = read(**kwargs)
                if kwargs["path"] == "/api/schedule.one":
                    getattr(store, "write_deployment_record")(
                        deployment.model_copy(
                            update={
                                "post_deploy_update": deployment.post_deploy_update.model_copy(
                                    update={"evidence": {}}
                                )
                            }
                        )
                    )
                return result

            with (
                patch(
                    "control_plane.testing_legacy_replacement_inspection.source.read_dokploy_config",
                    return_value=("https://provider.test", "fixture-token"),
                ),
                patch.object(api, "dokploy_request", side_effect=changed_evidence_read),
            ):
                view = testing_replacement_recovery_read(store, fenced, root)
            self.assertFalse(view["provider_inspected"])
            self.assertFalse(view["safe_to_release"])

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
