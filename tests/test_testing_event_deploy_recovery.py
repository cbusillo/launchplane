"""Unavailable original requests require complete absence and atomic target evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import Any

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord

import click
from sqlalchemy.exc import OperationalError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.event_testing_deploy import event_testing_deploy_request
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_event_deploy_recovery import (
    retire_unavailable_testing_event_deploy,
    testing_event_execution_lock_name,
)
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    build_generic_web_provider_reconciliation_key,
    build_generic_web_provider_target_key,
    decode_generic_web_provider_reconciliation_target,
)
from tests.support.profiles import product_profile_payload
from tests.test_generic_web_deploy_recovery import _generic_web_recovery_target


class TestingEventDeployRecoveryTests(unittest.TestCase):
    database_url: str = ""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'records.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        self.store.write_product_profile_record(self.profile)
        self.target = _generic_web_recovery_target()
        self.target.ship_request.target_type = "compose"
        self.target.ship_request.target_category = "compose"
        self.target.ship_request.provider_target_type = "compose"
        self.target.resolved_target.target_type = "compose"
        self.context = self.target.ship_request.context
        self.target_key = build_generic_web_provider_target_key(self.target)
        self.original = event_testing_deploy_request(
            product=self.profile.product,
            image_reference="ghcr.io/example/site@sha256:" + "a" * 64,
            source_commit="a" * 40,
        ).model_dump(mode="json")
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=self.context,
                instance="testing",
                target_type="compose",
                updated_at=self.now(),
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.context,
                instance="testing",
                target_id=self.target.resolved_target.target_id,
                updated_at=self.now(),
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.context,
                instance="testing",
                provider_id="dokploy",
                target_category="compose",
                target_id=self.target.resolved_target.target_id,
                display_name="test",
                updated_at=self.now(),
            )
        )
        self.provider = DokployGenericWebDeployProvider()
        self.payload: dict[str, Any] = {
            "composeId": self.target.resolved_target.target_id,
            "appName": "fixture",
            "serverId": "server",
            "composeStatus": "error",
            "composeType": "docker-compose",
        }
        self.responses: dict[str, object] = {
            "/api/deployment.allByType": [],
            "/api/schedule.list": [],
            "/api/deployment.queueList": [],
            "/api/docker.getContainers": [{"containerId": "unrelated-container"}],
            "/api/docker.getConfig": {
                "Config": {"Labels": {"com.docker.compose.project": "unrelated"}}
            },
        }
        config_mock = patch.object(
            self.provider,
            "_read_provider_config",
            return_value=("https://provider.test", "fixture-token"),
        )
        config_mock.start()
        self.addCleanup(config_mock.stop)
        target_mock = patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            side_effect=lambda **_: dict(self.payload),
        )
        target_mock.start()
        self.addCleanup(target_mock.stop)
        read_mock = patch("control_plane.dokploy.api.dokploy_request", side_effect=self.read)
        self.read_mock = read_mock.start()
        self.addCleanup(read_mock.stop)

    @staticmethod
    def now(offset: int = 0) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

    def read(self, **kwargs: object) -> object:
        self.assertEqual(kwargs.get("method", "GET"), "GET")
        return self.responses.get(str(kwargs["path"]))

    def reserve(
        self,
        *,
        retained: bool = False,
        phase: str = "target_update",
        expired: bool = True,
        state: str = "reconcile_required",
    ) -> LaunchplaneIdempotencyRecord:
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=self.now(-600 if expired else 0),
        ):
            record = self.store.reserve_mutation(
                scope=reconcile_reservation_scope(self.profile.product),
                route_path=GENERIC_WEB_DEPLOY_ROUTE,
                idempotency_key=f"{RECONCILE_SOURCE}:{self.profile.product}:{self.context}:testing:old",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path=GENERIC_WEB_DEPLOY_ROUTE, payload=self.original
                ),
                lease_owner="old-worker",
                provider_target_key=self.target_key,
                reconciliation_key=build_generic_web_provider_reconciliation_key(
                    self.target,
                    product=self.profile.product,
                    original_event_deploy=self.original if retained else None,
                ),
            ).record
            if phase:
                checkpoint = self.store.checkpoint_mutation_provider_effect(
                    reservation=record, effect_phase=phase
                )
                assert checkpoint.record is not None
                record = checkpoint.record
            if state == "running":
                return record
            fenced = self.store.mark_mutation_reconcile_required(
                reservation=record, reconciliation_key=record.reconciliation_key
            ).record
            assert fenced is not None
            return fenced

    def recover(self, record: LaunchplaneIdempotencyRecord) -> dict[str, object] | None:
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.target_key)
        ) as check:
            assert check is not None
            return retire_unavailable_testing_event_deploy(
                store=self.store,
                reservation=record,
                profile=self.profile,
                context=self.context,
                current_target_key=self.target_key,
                provider=self.provider,
                root=self.root,
                trace_id="recovery-test",
                check_execution_lock=check,
            )

    def current(self, record: LaunchplaneIdempotencyRecord) -> LaunchplaneIdempotencyRecord:
        result = self.store.lookup_existing_mutation_reservation(
            route_path=record.route_path,
            idempotency_key=record.idempotency_key,
            request_fingerprint=record.request_fingerprint,
        ).record
        assert result is not None
        return result

    def test_absence_retires_with_failed_audit_and_releases_target_for_normal_deploy(self) -> None:
        record = self.reserve()
        audit = self.recover(record)
        self.assertIsNotNone(audit)
        completed = self.current(record)
        assert completed.response_status_code is not None
        self.assertEqual(completed.state, "completed")
        self.assertGreaterEqual(completed.response_status_code, 400)
        self.assertEqual(completed.response_payload["recovery"], audit)
        self.assertEqual(completed.request_fingerprint, record.request_fingerprint)
        self.assertIsNone(self.recover(record))
        new = self.store.reserve_mutation(
            scope=record.scope,
            route_path=record.route_path,
            idempotency_key="new-build",
            request_fingerprint="fresh",
            lease_owner="new-worker",
            reconciliation_key=record.reconciliation_key,
            provider_target_key=record.provider_target_key,
        )
        self.assertEqual(new.status, "acquired")
        self.assertEqual(
            self.store.renew_mutation_reservation(
                reservation=record.model_copy(update={"state": "running"})
            ).status,
            "not_running",
        )

    def assert_held_without_inspection(self, record: LaunchplaneIdempotencyRecord) -> None:
        self.assertIsNone(self.recover(record))
        self.read_mock.assert_not_called()
        self.assertEqual(self.current(record), record)

    def test_retained_original_remains_on_exact_recovery_path(self) -> None:
        self.assert_held_without_inspection(self.reserve(retained=True))

    def test_active_lease_is_not_retired(self) -> None:
        self.assert_held_without_inspection(self.reserve(expired=False))

    def test_expired_running_attempt_is_retired_with_its_exact_identity(self) -> None:
        record = self.reserve(state="running")
        audit = self.recover(record)
        self.assertIsNotNone(audit)
        completed = self.current(record)
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.attempt, record.attempt)
        self.assertEqual(completed.request_fingerprint, record.request_fingerprint)

    def test_post_deploy_effect_remains_held(self) -> None:
        self.assert_held_without_inspection(self.reserve(phase="post_deploy_started"))

    def test_queued_trigger_missing_from_history_stays_held_and_reports_why(self) -> None:
        record = self.reserve()
        self.responses["/api/deployment.queueList"] = [
            {"data": {"composeId": self.target.resolved_target.target_id}, "state": "waiting"}
        ]
        refusals: list[str] = []
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.target_key)
        ) as check:
            assert check is not None
            result = retire_unavailable_testing_event_deploy(
                store=self.store,
                reservation=record,
                profile=self.profile,
                context=self.context,
                current_target_key=self.target_key,
                provider=self.provider,
                root=self.root,
                trace_id="test",
                check_execution_lock=check,
                record_refusal=refusals.append,
            )
        self.assertIsNone(result)
        self.assertTrue(refusals)
        self.assertEqual(self.current(record), record)

    def test_local_provider_not_dispatched_attempt_can_retire(self) -> None:
        record = self.reserve()
        self.payload["serverId"] = None
        self.assertIsNotNone(self.recover(record))

    def test_failed_compose_may_have_hidden_live_container_and_stays_held(self) -> None:
        record = self.reserve()
        # getContainers omits a live custom container named "dokploy-agent"
        # even when the compose appName is an ordinary value.
        self.responses["/api/deployment.allByType"] = [
            {
                "deploymentId": "failed-job",
                "status": "error",
                "finishedAt": self.now(-1),
            }
        ]
        self.assertIsNone(self.recover(record))
        self.assertEqual(self.current(record), record)

    def test_triggered_attempt_cannot_qualify_with_filtered_container_inventory(self) -> None:
        record = self.reserve(phase="deploy_trigger")
        self.assertIsNone(self.recover(record))
        self.assertEqual(self.current(record), record)

    def test_execution_lock_loss_prevents_provider_checkpoint(self) -> None:
        from control_plane.generic_web_deploy_provider_adapter import (
            GenericWebDeployProviderMutationAdapter,
        )
        from control_plane.provider_operations import ProviderMutationRejectedError

        request = event_testing_deploy_request(
            product=self.profile.product,
            image_reference="ghcr.io/example/site@sha256:" + "a" * 64,
            source_commit="a" * 40,
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            deploy_request=request,
            profile=self.profile,
            lane=next(lane for lane in self.profile.lanes if lane.instance == "testing"),
            trace_id="lock-test",
            deploy_provider=self.provider,
        )

        def execute(**kwargs: Any) -> None:
            kwargs["provider_effect_checkpoint"]("target_update")
            self.fail("provider effect ran after execution lock loss")

        for error in (
            RuntimeError("execution lock lost"),
            OperationalError("select 1", None, RuntimeError("connection closed")),
        ):
            with self.subTest(error=type(error).__name__):
                adapter.execution_check = Mock(side_effect=error)
                lease = Mock()
                with (
                    patch.object(adapter, "_resolve_deploy_target", return_value=self.target),
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
                        side_effect=execute,
                    ),
                    self.assertRaises(ProviderMutationRejectedError),
                ):
                    adapter.apply("operation", lease)
                lease.checkpoint_effect.assert_not_called()

    def test_connection_loss_before_or_after_inspection_keeps_reservation(self) -> None:
        record = self.reserve()
        for checks in (
            [OperationalError("select 1", None, RuntimeError("connection closed"))],
            [None, OperationalError("select 1", None, RuntimeError("connection closed"))],
        ):
            with self.subTest(after_inspection=len(checks) > 1):
                refusals: list[str] = []
                result = retire_unavailable_testing_event_deploy(
                    store=self.store,
                    reservation=record,
                    profile=self.profile,
                    context=self.context,
                    current_target_key=self.target_key,
                    provider=self.provider,
                    root=self.root,
                    trace_id="lock-loss",
                    check_execution_lock=Mock(side_effect=checks),
                    record_refusal=refusals.append,
                )
                self.assertIsNone(result)
                self.assertTrue(refusals)
                self.assertEqual(self.current(record), record)

    def test_provider_unknown_nonempty_and_incomplete_evidence_preserves_fence(self) -> None:
        record = self.reserve()
        initial_responses = dict(self.responses)
        for path in self.responses:
            results: tuple[object, ...] = (
                None,
                {},
                [None],
                [{"status": "running"}],
                [{"status": "done"}],
                [{"status": "error"}],
            )
            for result in results:
                with self.subTest(path=path, result=result):
                    self.responses[path] = result
                    self.assertIsNone(self.recover(record))
                    self.assertEqual(self.current(record), record)
            self.responses[path] = initial_responses[path]
        for status in (None, "running", "queued"):
            self.payload["composeStatus"] = status
            self.assertIsNone(self.recover(record))
        self.payload["composeStatus"] = "error"
        with patch.object(
            self.provider,
            "observe_testing_no_live_effect",
            side_effect=click.ClickException("provider unavailable"),
        ):
            self.assertIsNone(self.recover(record))
        self.assertEqual(self.current(record), record)

    def test_swallowed_container_read_errors_and_stopped_project_container_stay_held(self) -> None:
        record = self.reserve()
        self.responses["/api/docker.getContainers"] = []
        self.assertIsNone(self.recover(record))
        self.responses["/api/docker.getContainers"] = [{"containerId": "stopped"}]
        self.responses["/api/docker.getConfig"] = {
            "Config": {"Labels": {"com.docker.compose.project": self.payload["appName"]}},
            "State": {"Running": False},
        }
        self.assertIsNone(self.recover(record))
        self.assertEqual(self.current(record), record)

    def test_target_change_during_inspection_and_stale_attempt_refuse_atomic_retirement(
        self,
    ) -> None:
        record = self.reserve()

        def drift(**_: object) -> dict[str, object]:
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=self.context,
                    instance="testing",
                    target_id="different",
                    updated_at=self.now(),
                )
            )
            return {"state": "no_live_effect"}

        with patch.object(self.provider, "observe_testing_no_live_effect", side_effect=drift):
            self.assertIsNone(self.recover(record))
        self.assertEqual(self.current(record), record)
        self.read_mock.reset_mock()
        self.assertIsNone(self.recover(record.model_copy(update={"attempt": record.attempt + 1})))
        self.read_mock.assert_not_called()

    def test_prod_cross_lane_and_worker_exclusion_remain_closed(self) -> None:
        record = self.reserve()
        snapshot = decode_generic_web_provider_reconciliation_target(record.reconciliation_key)
        snapshot.instance = "prod"
        self.assertIsNone(
            self.provider.observe_testing_no_live_effect(
                control_plane_root=self.root,
                target=snapshot,
                operation_title="",
                provider_effect_phase="target_update",
            )
        )
        self.assertIsNone(self.recover(record.model_copy(update={"scope": "caller-scope"})))
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.target_key)
        ):
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(self.target_key)
            ) as check:
                self.assertIsNone(check)
        self.read_mock.assert_not_called()
