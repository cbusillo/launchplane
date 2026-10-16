"""Daily restore admission, durable evidence and notification behavior in isolated stores."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.odoo_target_replacement_failures import TESTING_DRILL_DATABASE_KEY
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.odoo_stable_target_replacement import (
    LAUNCHPLANE_REQUIRED_ODOO_MODULES,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.odoo_testing_drill import (
    DRILL_INTERVAL_SECONDS,
    DRILL_TIMEOUT_SECONDS,
    DRILL_ALERT_RETRY_SECONDS,
    DRILL_SCOPE,
    DRILL_ROUTE,
    DRILL_DEADLINE_STEP,
    lane_key,
    OdooTestingDrillScheduler,
    require_drill_checkpoint,
    testing_drill_evidence,
    DrillSkipped,
)
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneDisposableReplacementPreparationRow,
    LaunchplaneDisposableTestingResourceAllocationRow,
    LaunchplaneFreshOdooTestingGenerationRow,
)
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.support.disposable_lanes import record_safety
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_odoo_testing_restore import generation


class TestingDrillScenario(unittest.TestCase):
    def seed(self, database_url: str) -> None:
        self.store = PostgresRecordStore(database_url=database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.context = next(
            lane.context for lane in self.profile.lanes if lane.instance == "testing"
        )
        production = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        self.profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable",
                                allowed_rebuild_sources=("upstream_restore",),
                                upstream_source=f"{self.profile.product}:{production.context}:prod",
                            ),
                            "odoo_testing_drill_enabled": True,
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.gen = generation().model_copy(
            update={
                "context": self.context,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "reconcile_requested_at": self.profile.updated_at,
            }
        )
        self.resources = OdooTestingResources(
            product=self.profile.product,
            context=self.context,
            operation_id=self.gen.operation_id,
            target_id=self.gen.target_id,
            namespace=self.gen.namespace,
        )
        self.gen = self.gen.model_copy(
            update={"resources_binding_sha256": self.resources.binding_sha256}
        )
        for lane in self.profile.lanes:
            target = DokployTargetRecord(
                context=lane.context,
                instance=lane.instance,
                target_name="fixture-" + lane.instance,
                updated_at=self.profile.updated_at,
            )
            target_id = DokployTargetIdRecord(
                context=lane.context,
                instance=lane.instance,
                target_id=self.gen.target_id
                if lane.instance == "testing"
                else "production-compose",
                updated_at=self.profile.updated_at,
            )
            self.store.write_dokploy_target_record(target)
            self.store.write_dokploy_target_id_record(target_id)
            self.store.write_provider_target_record(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target, target_id_record=target_id
                )
            )
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
            context=self.context,
            instance="testing",
            env={
                **self.resources.volumes,
                "ODOO_PROJECT_NAME": self.gen.namespace,
                "ODOO_STACK_NAME": self.gen.namespace,
                "ODOO_DB_NAME": "previous_database",
            },
            updated_at=self.profile.updated_at,
        )
        self.store.write_runtime_environment_record(self.runtime)
        # Seed a completed bootstrap as an immutable fixture, without faking provider proof.
        preparation = DisposableTestingResourcePreparation(
            operation_id=self.gen.operation_id,
            product=self.profile.product,
            context=self.context,
            retained_target_id="retained-compose",
            prepared_by="fixture",
            recorded_at=datetime.now(timezone.utc),
            resource_namespace=self.gen.namespace,
            replacement_target_id=self.gen.target_id,
            resources_binding_sha256=self.resources.binding_sha256,
        )
        with self.store._session_factory() as session:
            session.add(
                LaunchplaneDisposableReplacementPreparationRow(
                    operation_id=self.gen.operation_id,
                    payload=preparation.model_dump(
                        mode="json",
                        exclude={
                            "resource_namespace",
                            "replacement_target_id",
                            "resources_binding_sha256",
                            "reviewed_plan_sha256",
                            "retained_target_record",
                        },
                    ),
                )
            )
            session.add(
                LaunchplaneDisposableTestingResourceAllocationRow(
                    operation_id=self.gen.operation_id,
                    payload={
                        key: value
                        for key, value in preparation.model_dump(mode="json").items()
                        if key
                        in {
                            "resource_namespace",
                            "replacement_target_id",
                            "resources_binding_sha256",
                        }
                    },
                )
            )
            session.add(
                LaunchplaneFreshOdooTestingGenerationRow(
                    product=self.profile.product,
                    context=self.context,
                    payload=self.gen.model_dump(mode="json"),
                )
            )
            session.commit()
        manifest = artifact_manifest_v2(
            image_repository=self.profile.image.repository,
            odoo_install_modules=LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        )
        self.store.write_artifact_manifest(manifest)
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=self.context,
                instance="testing",
                artifact_identity=ArtifactIdentityReference(artifact_id=manifest.artifact_id),
                source_git_ref=manifest.source_commit,
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="compose",
                    deployment_id="fixture",
                    target_name="fixture",
                    target_type="compose",
                ),
                updated_at=self.profile.updated_at,
                deployment_record_id="fixture-deploy",
            )
        )
        record_safety(self.store, self.profile.product, self.context, "testing")
        self.now = datetime.now(timezone.utc).timestamp()
        self.notifications = Mock(return_value=True)
        self.scheduler = OdooTestingDrillScheduler(
            self.store, clock=lambda: self.now, notify=self.notifications
        )
        self.enterContext(
            patch("control_plane.odoo_testing_restore.require_distinct_testing_credentials")
        )
        self.signins = self.enterContext(
            patch("control_plane.odoo_testing_drill.read_drill_recent_signins", return_value=False)
        )

    def evidence(self) -> Any:
        return testing_drill_evidence(self.store, self.profile.product, self.context, "testing")

    def operation(self) -> Any:
        return self.store.read_odoo_stable_target_replacement_operation_record(
            self.evidence().operation_id
        )

    def complete(self, status: str) -> None:
        operation = self.activate()
        result = OdooStableTargetReplacementApplyResult(
            product=operation.product,
            context=operation.context,
            instance="testing",
            strategy="recreate-in-place",
            deploy_status="pass" if status == "pass" else "fail",
            health_status="pass" if status == "pass" else "fail",
            deployment_record_id="new-deployment",
        )
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": status,
                    "phase": "completed" if status == "pass" else "failed",
                    "finished_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                    "result": result,
                    "error_code": "restore_failed" if status == "fail" else "",
                    "error_message": "private provider details" if status == "fail" else "",
                }
            )
        )
        if status == "pass":
            current = self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.context
            )
            assert current is not None
            self.store.write_fresh_odoo_testing_generation(
                current.model_copy(
                    update={
                        "completed_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat()
                    }
                )
            )

    def activate(self) -> Any:
        operation = self.operation()
        if operation.status == "pending":
            operation = self.store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="fixture-drill-worker",
                claimed_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                lease_expires_at=datetime.fromtimestamp(self.now + 300, timezone.utc).isoformat(),
            )
        assert operation is not None
        self.store.activate_odoo_testing_drill(operation, self.now)
        return operation


class TestingDrillTests(TestingDrillScenario):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.seed(f"sqlite+pysqlite:///{root / 'state.db'}")

    def test_recent_successful_signin_skips_without_queueing_or_selecting_database(self) -> None:
        self.signins.return_value = True
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "skipped")
        self.assertEqual(self.evidence().step, "recent_successful_signin")
        self.assertEqual(self.evidence().operation_id, "")
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )
        self.assertFalse(self.notifications.called)

    def test_unreadable_successful_login_check_skips_without_leaking_error(self) -> None:
        self.signins.side_effect = RuntimeError("private-provider-value")
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "skipped")
        self.assertEqual(self.evidence().step, "successful_login_check_unreadable")
        self.assertNotIn("private-provider-value", self.evidence().model_dump_json())
        self.assertEqual(self.evidence().operation_id, "")

    def test_down_lane_queues_without_login_history_and_keeps_resource_qualification(self) -> None:
        self.assert_down_lane_drill_admitted("unknown")

    def test_down_lane_overrides_historical_use_for_drill_repair(self) -> None:
        self.assert_down_lane_drill_admitted("in_use")

    def assert_down_lane_drill_admitted(self, usage: str) -> None:
        from tests.support.down_lanes import record_lane_health
        from control_plane.lane_usage import VERIFIED_DOWN_USAGE_EVIDENCE_PREFIX

        record_lane_health(
            self.store,
            self.profile.product,
            self.context,
            datetime.fromtimestamp(self.now, timezone.utc),
        )
        record_safety(self.store, self.profile.product, self.context, "testing", usage=usage)
        self.signins.return_value = None
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "queued")
        self.activate()
        # Re-entering activation uses the continuously leased checkpoint path.
        self.activate()
        operation = self.operation()
        receipt = self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
        )
        assert receipt is not None
        safety = receipt.response_payload["activation_safety"]
        assert isinstance(safety, dict)
        self.assertEqual(safety["usage"], "unknown")
        self.assertTrue(
            str(safety["usage_evidence"]).startswith(VERIFIED_DOWN_USAGE_EVIDENCE_PREFIX)
        )
        require_drill_checkpoint(self.store, operation)

    def test_new_usage_reader_failures_keep_a_bounded_skip_reason(self) -> None:
        for name in ("lane_usage_protected", "evaluate_lane_usage"):
            record_safety(
                self.store,
                self.profile.product,
                self.context,
                "testing",
                usage="in_use" if name == "lane_usage_protected" else "unused",
            )
            with (
                self.subTest(reader=name),
                patch(
                    "control_plane.odoo_testing_drill." + name,
                    side_effect=RuntimeError("private-provider-detail"),
                ),
            ):
                self.scheduler.run_once()
                self.assertEqual(self.evidence().status, "skipped")
                self.assertNotIn("private-provider-detail", self.evidence().model_dump_json())
            self.now += DRILL_INTERVAL_SECONDS

    def test_idle_lane_runs_without_renewing_general_safety_facts(self) -> None:
        self.store.clear_disposable_lane_safety(self.profile.product, self.context, "testing")
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "queued")
        self.activate()
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing")
        )

    def test_signin_between_queue_and_activation_protects_served_copy(self) -> None:
        self.scheduler.run_once()
        self.signins.return_value = True
        with self.assertRaises(DrillSkipped):
            self.activate()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_reclaimed_activation_reads_the_original_served_database(self) -> None:
        self.scheduler.run_once()
        operation = self.activate()
        reclaimed = operation.model_copy(update={"attempt": operation.attempt + 1})
        self.store.write_odoo_stable_target_replacement_operation_record(reclaimed)
        self.store.activate_odoo_testing_drill(reclaimed, self.now)
        self.assertEqual(self.signins.call_args.kwargs["history_generation"], self.gen)
        self.assertEqual(
            self.signins.call_args.kwargs["history_database"], self.runtime.env["ODOO_DB_NAME"]
        )
        require_drill_checkpoint(self.store, reclaimed)

    def test_context_database_name_is_preserved_for_reclaim(self) -> None:
        self.runtime = self.runtime.model_copy(
            update={
                "env": {
                    key: value for key, value in self.runtime.env.items() if key != "ODOO_DB_NAME"
                }
            }
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.context,
                env={"ODOO_DB_NAME": "context_database"},
                updated_at=self.profile.updated_at,
            )
        )
        self.scheduler.run_once()
        operation = self.activate()
        reclaimed = operation.model_copy(update={"attempt": operation.attempt + 1})
        self.store.write_odoo_stable_target_replacement_operation_record(reclaimed)
        self.store.activate_odoo_testing_drill(reclaimed, self.now)
        self.assertEqual(self.signins.call_args.kwargs["history_database"], "context_database")

    def test_reclaim_skips_a_copy_that_may_already_have_testers(self) -> None:
        from control_plane.odoo_testing_drill_usage import DrillCopyAlreadyServing

        self.scheduler.run_once()
        operation = self.activate()
        reclaimed = operation.model_copy(update={"attempt": operation.attempt + 1})
        self.store.write_odoo_stable_target_replacement_operation_record(reclaimed)
        self.signins.side_effect = DrillCopyAlreadyServing()
        before = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        with self.assertRaises(DrillSkipped):
            self.store.activate_odoo_testing_drill(reclaimed, self.now)
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            before,
        )

    def test_recovery_checkpoint_refuses_a_changed_drill_policy(self) -> None:
        from control_plane.testing_replacement_recovery import guard_testing_replacement

        self.scheduler.run_once()
        operation = self.activate()
        with guard_testing_replacement(
            self.store, operation, operation.lease_owner, lambda phase: None
        ) as checkpoint:
            checkpoint("fixture-first-effect")
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"odoo_testing_drill_enabled": False})
                            if lane.instance == "testing"
                            else lane
                            for lane in self.profile.lanes
                        )
                    }
                )
            )
            with self.assertRaises(RuntimeError):
                checkpoint("fixture-later-effect")

    def test_activation_refuses_an_expired_observation(self) -> None:
        from control_plane.odoo_testing_drill import collect_drill_admission

        self.scheduler.run_once()
        old = collect_drill_admission(self.store, self.gen, self.now - 600)
        with patch("control_plane.odoo_testing_drill.collect_drill_admission", return_value=old):
            with self.assertRaises(DrillSkipped):
                self.activate()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_expired_recorded_person_use_still_skips(self) -> None:
        record_safety(
            self.store,
            self.profile.product,
            self.context,
            "testing",
            usage="in_use",
            observed_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1, minutes=50),
        )
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "skipped")
        self.signins.assert_not_called()

    def test_daily_admission_is_atomic_and_restart_does_not_repeat(self) -> None:
        before = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        self.scheduler.run_once()
        operation = self.operation()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )
        operation = self.activate()
        self.assertEqual(operation.request.data_source_mode, "upstream_restore")
        current = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert current is not None
        self.assertNotEqual(current.drill_database, self.runtime.env["ODOO_DB_NAME"])
        require_drill_checkpoint(self.store, operation)
        OdooTestingDrillScheduler(
            self.store, clock=lambda: self.now, notify=self.notifications
        ).run_once()
        self.assertEqual(self.operation(), operation)
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            before,
        )
        receipt = self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
        )
        assert receipt is not None
        self.assertEqual(
            receipt.response_payload["previous_generation"], self.gen.model_dump(mode="json")
        )
        self.assertIsNone(
            testing_drill_evidence(self.store, self.profile.product, self.context, "prod")
        )

    def test_hold_and_recorded_use_preserve_runtime_and_copy(self) -> None:
        for protection in ("hold", "in_use", "only_copy", "shared"):
            with self.subTest(protection=protection):
                target = self.store.read_dokploy_target_record(
                    context_name=self.context, instance_name="testing"
                )
                target = target.model_copy(
                    update={
                        "policies": target.policies.model_copy(
                            update={
                                "staff_testing_hold": DokployTargetStaffTestingHold(
                                    reason="staff session",
                                    recorded_by="fixture",
                                    recorded_at=self.profile.updated_at,
                                )
                                if protection == "hold"
                                else None
                            }
                        )
                    }
                )
                self.store.write_dokploy_target_record(target)
                changes: dict[str, Any] = {}
                if protection in {"in_use", "unknown"}:
                    changes["usage"] = protection
                if protection == "only_copy":
                    changes["data"] = "only_copy"
                if protection == "shared":
                    changes["production_resources"] = "shared"
                if protection == "expired":
                    changes.update(
                        observed_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                    )
                record_safety(self.store, self.profile.product, self.context, "testing", **changes)
                self.now += DRILL_INTERVAL_SECONDS
                self.store.enqueue_odoo_testing_drill(
                    self.profile.product,
                    self.context,
                    f"fixture-{protection}",
                    datetime.now(timezone.utc).timestamp(),
                )
                self.assertEqual(
                    self.evidence().status,
                    "skipped",
                )
                self.assertEqual(
                    self.store.read_fresh_odoo_testing_generation(
                        self.profile.product, self.context
                    ),
                    self.gen,
                )
                self.assertEqual(
                    next(
                        iter(
                            self.store.list_runtime_environment_records(
                                scope="instance", context_name=self.context, instance_name="testing"
                            )
                        )
                    ),
                    self.runtime,
                )

    def test_failure_alert_retries_and_never_recaptures_incomplete_copy(self) -> None:
        self.scheduler.run_once()
        operation = self.operation()
        self.complete("fail")
        self.notifications.side_effect = [False, True]
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        self.assertEqual(self.evidence().step, "restore_failed")
        self.assertFalse(self.evidence().alert_delivered)
        self.scheduler.run_once()
        self.assertEqual(self.notifications.call_count, 1)
        self.now += DRILL_ALERT_RETRY_SECONDS + 1
        self.scheduler.run_once()
        self.assertTrue(self.evidence().alert_delivered)
        self.assertNotIn("private provider", str(self.notifications.call_args))
        self.assertEqual(self.operation().operation_id, operation.operation_id)

    def test_queue_starvation_alerts_and_retains_original_operation(self) -> None:
        self.scheduler.run_once()
        operation = self.operation()
        self.now += DRILL_TIMEOUT_SECONDS + 1
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        self.assertEqual(self.evidence().step, DRILL_DEADLINE_STEP)
        self.assertTrue(self.notifications.called)
        self.now += DRILL_INTERVAL_SECONDS
        self.scheduler.run_once()
        self.assertEqual(self.operation(), operation)

    def test_pass_publishes_deployment_evidence_and_next_day_uses_new_database(self) -> None:
        self.scheduler.run_once()
        self.activate()
        first = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert first is not None
        self.complete("pass")
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "pass")
        self.assertEqual(self.evidence().deployment_record_id, "new-deployment")
        self.assertFalse(self.notifications.called)
        self.now += DRILL_INTERVAL_SECONDS
        # A real producer must supply new facts: yesterday's observation is not renewed.
        record_safety(
            self.store,
            self.profile.product,
            self.context,
            "testing",
            observed_at=datetime.fromtimestamp(self.now, timezone.utc),
            expires_at=datetime.fromtimestamp(self.now, timezone.utc) + timedelta(minutes=5),
        )
        self.scheduler.run_once()
        self.activate()
        second = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert second is not None
        self.assertNotEqual(first.drill_database, second.drill_database)
        self.assertNotEqual(generation_binding(first), generation_binding(second))

    def test_person_starting_after_queue_is_rechecked_before_effects(self) -> None:
        self.scheduler.run_once()
        record_safety(self.store, self.profile.product, self.context, "testing", usage="in_use")
        with self.assertRaises(DrillSkipped):
            self.activate()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_missing_discord_destination_is_an_undelivered_alert(self) -> None:
        scheduler = OdooTestingDrillScheduler(self.store)
        self.assertFalse(
            scheduler._notify(
                self.profile.product, self.context, {"step": "restore", "trace": "fixture"}
            )
        )

    def test_discord_uses_existing_lane_destination_and_omits_private_errors(self) -> None:
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="fixture-discord",
                product=self.profile.product,
                context=self.context,
                instance="testing",
                destinations=(
                    PublicIngressNotificationDestination(
                        destination_id="existing-discord",
                        kind="discord",
                        discord_webhook_secret="existing-managed-sender",
                    ),
                ),
                created_at=self.profile.updated_at,
                updated_at=self.profile.updated_at,
            )
        )
        scheduler = OdooTestingDrillScheduler(self.store)
        with (
            patch(
                "control_plane.workflows.public_ingress_monitor.managed_notification_secret_value",
                return_value="fixture-sender",
            ) as resolve,
            patch("control_plane.notifications.post_discord_webhook") as post,
        ):
            self.assertTrue(
                scheduler._notify(
                    self.profile.product,
                    self.context,
                    {
                        "step": "restore_failed",
                        "trace": "fixture-trace",
                        "private": "must not leak",
                    },
                )
            )
        self.assertEqual(
            resolve.call_args.args[1:], ("existing-managed-sender", self.context, "testing")
        )
        self.assertIn("restore_failed", post.call_args.args[1]["content"])
        self.assertIn("fixture-trace", post.call_args.args[1]["content"])
        self.assertNotIn("must not leak", str(post.call_args))

    def test_running_attempt_keeps_admission_after_observation_expires_without_rewriting_it(
        self,
    ) -> None:
        self.scheduler.run_once()
        original = self.store.read_disposable_lane_safety(
            self.profile.product, self.context, "testing"
        )
        operation = self.activate()
        self.assertEqual(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing"),
            original,
        )
        assert original is not None
        with (
            patch("control_plane.odoo_testing_drill.datetime", wraps=datetime) as clock,
            patch("control_plane.disposable_lanes.datetime", wraps=datetime) as admission_clock,
        ):
            clock.now.return_value = original.expires_at + timedelta(minutes=20)
            admission_clock.now.return_value = clock.now.return_value
            require_drill_checkpoint(self.store, operation)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="staff session",
                                recorded_by="fixture",
                                recorded_at=self.profile.updated_at,
                            )
                        }
                    )
                }
            )
        )
        with self.assertRaises(DrillSkipped):
            require_drill_checkpoint(self.store, operation)

    def test_worker_classifies_pre_effect_use_as_skip_and_keeps_current_database(self) -> None:
        from control_plane.workflows.odoo_stable_operation_worker import (
            _execute_target_replacement_with_checkpoint,
        )

        self.scheduler.run_once()
        operation = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="fixture-worker",
            claimed_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            lease_expires_at=datetime.fromtimestamp(self.now + 300, timezone.utc).isoformat(),
        )
        assert operation is not None
        record_safety(self.store, self.profile.product, self.context, "testing", usage="in_use")
        with (
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.DurableOperationAuthorizationGuard"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker._start_target_replacement_heartbeat"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply"
            ) as execute,
        ):
            self.assertTrue(
                _execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=Path("/fixture/control-plane"),
                    operation=operation,
                    lease_owner=operation.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        execute.assert_not_called()
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "skipped")
        self.assertFalse(self.notifications.called)
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_undelivered_alert_does_not_block_repaired_prerequisite_next_day(self) -> None:
        self.notifications.return_value = False
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"artifact_identity": None})
        )
        self.scheduler.run_once()
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        self.now += DRILL_INTERVAL_SECONDS
        self.store.write_environment_inventory(inventory)
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "queued")
        self.assertTrue(
            self.store.list_pending_odoo_testing_drill_alerts(self.profile.product, self.context)
        )

    def test_late_success_refreshes_original_evidence_and_keeps_deadline_alert(self) -> None:
        self.scheduler.run_once()
        self.activate()
        self.now += DRILL_TIMEOUT_SECONDS + 1
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        operation = self.operation()
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "pass",
                    "phase": "completed",
                    "finished_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                    "result": OdooStableTargetReplacementApplyResult(
                        product=operation.product,
                        context=operation.context,
                        instance="testing",
                        strategy="recreate-in-place",
                        deploy_status="pass",
                        health_status="pass",
                        deployment_record_id="late-deployment",
                    ),
                }
            )
        )
        current = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert current is not None
        self.store.write_fresh_odoo_testing_generation(
            current.model_copy(
                update={"completed_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat()}
            )
        )
        self.scheduler.run_once()
        # A new daily slot may now be queued; inspect this attempt's receipt.
        completed = self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
        )
        assert completed is not None
        self.assertEqual(completed.response_payload["status"], "pass")
        self.assertTrue(completed.response_payload["alert_delivered"])

    def test_partial_discord_delivery_does_not_resend_successful_destination(self) -> None:
        destinations = tuple(
            PublicIngressNotificationDestination(
                destination_id=name, kind="discord", discord_webhook_secret=name
            )
            for name in ("working", "unavailable")
        )
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="fixture-partial",
                product=self.profile.product,
                context=self.context,
                instance="testing",
                destinations=destinations,
                created_at=self.profile.updated_at,
                updated_at=self.profile.updated_at,
            )
        )
        scheduler = OdooTestingDrillScheduler(self.store)
        payload: dict[str, Any] = {"step": "restore", "trace": "fixture"}

        def post(sender: str, body: object) -> None:
            if sender == "unavailable":
                raise RuntimeError("fixture provider offline")

        with (
            patch(
                "control_plane.workflows.public_ingress_monitor.managed_notification_secret_value",
                side_effect=lambda store, key, context, instance: key,
            ),
            patch("control_plane.notifications.post_discord_webhook", side_effect=post) as send,
        ):
            self.assertFalse(scheduler._notify(self.profile.product, self.context, payload))
            self.assertFalse(scheduler._notify(self.profile.product, self.context, payload))
        self.assertEqual([call.args[0] for call in send.call_args_list].count("working"), 1)

    def test_failed_final_verification_retains_immediate_completed_predecessor(self) -> None:
        self.scheduler.run_once()
        self.complete("pass")
        first = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert first is not None
        self.scheduler.run_once()
        self.now += DRILL_INTERVAL_SECONDS
        record_safety(
            self.store,
            self.profile.product,
            self.context,
            "testing",
            observed_at=datetime.fromtimestamp(self.now, timezone.utc),
            expires_at=datetime.fromtimestamp(self.now, timezone.utc) + timedelta(minutes=5),
        )
        self.scheduler.run_once()
        self.complete("pass")
        second = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert second is not None
        self.scheduler.run_once()
        self.now += DRILL_INTERVAL_SECONDS
        record_safety(
            self.store,
            self.profile.product,
            self.context,
            "testing",
            observed_at=datetime.fromtimestamp(self.now, timezone.utc),
            expires_at=datetime.fromtimestamp(self.now, timezone.utc) + timedelta(minutes=5),
        )
        self.scheduler.run_once()
        self.complete("fail")
        failed = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert failed is not None
        self.assertEqual(failed.retained_drill_database, second.drill_database)
        self.assertEqual(failed.retained_copy_binding, generation_binding(second))
        self.assertEqual(failed.discard_drill_database, first.drill_database)
        self.assertNotEqual(failed.discard_drill_database, failed.retained_drill_database)

    def test_repaired_precondition_can_admit_same_daily_slot(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"artifact_identity": None})
        )
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        self.store.write_environment_inventory(inventory)
        self.now = datetime.now(timezone.utc).timestamp()
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "queued")

    def test_rollout_is_inert_until_testing_lane_is_enabled(self) -> None:
        profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"odoo_testing_drill_enabled": False})
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(profile)
        self.scheduler.run_once()
        self.assertIsNone(self.evidence())
        self.assertFalse(self.notifications.called)
        self.assertFalse(self.store.list_odoo_stable_target_replacement_operation_records())

    def test_scheduler_update_preserves_concurrent_worker_admission(self) -> None:
        self.scheduler.run_once()
        operation = self.operation()
        before = self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
        )
        assert before is not None
        operation = self.activate()
        stale = {**before.response_payload, "status": "fail", "step": DRILL_DEADLINE_STEP}
        self.store.update_odoo_testing_drill_receipt(operation.idempotency_key, self.now, stale)
        require_drill_checkpoint(self.store, operation)

    def test_guarded_worker_preflight_failure_restores_the_untouched_lane(self) -> None:
        from control_plane.contracts.deployment_record import (
            DeploymentRecord,
            ResolvedTargetEvidence,
        )
        from control_plane.testing_restore_prerequisite import incomplete_testing_restore
        from control_plane.workflows.odoo_stable_operation_worker import (
            _execute_target_replacement_with_checkpoint,
        )

        self.scheduler.run_once()
        operation = self.activate()
        active = self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context)
        assert active is not None

        def refuse_before_effects(**_: object) -> None:
            self.store.write_deployment_record(
                DeploymentRecord(
                    record_id="unstarted-drill",
                    context=self.context,
                    instance="testing",
                    source_git_ref=operation.request.source_git_ref,
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose", target_id=active.target_id, target_name="fixture"
                    ),
                    runtime_source={
                        "data_source_mode": operation.request.data_source_mode,
                        TESTING_DRILL_DATABASE_KEY: active.drill_database,
                    },
                    deploy=DeploymentEvidence(
                        status="pending",
                        deploy_mode="recreate-in-place",
                        started_at=operation.started_at,
                        target_name="fixture",
                        target_type="compose",
                    ),
                )
            )
            raise ValueError("fixture preflight")

        with (
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.DurableOperationAuthorizationGuard"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker._start_target_replacement_heartbeat"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.require_unchanged_production_artifact"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                side_effect=refuse_before_effects,
            ),
        ):
            self.assertTrue(
                _execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=Path("/fixture/control-plane"),
                    operation=operation,
                    lease_owner=operation.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                    execution_checkpoint=lambda phase: None,
                )
            )
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="instance", context_name=self.context, instance_name="testing"
            ),
            (self.runtime,),
        )
        self.assertIsNone(
            incomplete_testing_restore(
                self.store, context=self.context, instance="testing", generation=self.gen
            )
        )
        for selected in (
            None,
            active,
            active.model_copy(update={"completed_at": self.gen.completed_at}),
        ):
            with self.subTest(selected=selected):
                self.assertIsNotNone(
                    incomplete_testing_restore(
                        self.store, context=self.context, instance="testing", generation=selected
                    )
                )
        pending = self.store.read_deployment_record("unstarted-drill")
        self.store.write_deployment_record(
            pending.model_copy(update={"runtime_source": {"data_source_mode": "upstream_restore"}})
        )
        self.assertIsNotNone(
            incomplete_testing_restore(
                self.store, context=self.context, instance="testing", generation=self.gen
            )
        )
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")

    def test_no_effect_source_retry_restores_selection_before_a_protected_reclaim(self) -> None:
        from control_plane.workflows.odoo_stable_operation_worker import (
            _execute_target_replacement_with_checkpoint,
        )
        from control_plane.lane_movement import LaneMovementRefused

        self.scheduler.run_once()
        operation = self.activate()
        with (
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.DurableOperationAuthorizationGuard"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker._start_target_replacement_heartbeat"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.require_unchanged_production_artifact"
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                side_effect=LaneMovementRefused("source_order_unavailable"),
            ),
        ):
            self.assertTrue(
                _execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=Path("/fixture/control-plane"),
                    operation=operation,
                    lease_owner=operation.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                    execution_checkpoint=lambda phase: None,
                )
            )
        self.assertEqual(self.operation().status, "pending")
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="instance", context_name=self.context, instance_name="testing"
            ),
            (self.runtime,),
        )
        record_safety(self.store, self.profile.product, self.context, "testing", usage="in_use")
        self.now += 2000
        with self.assertRaises(DrillSkipped):
            self.activate()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, self.context),
            self.gen,
        )

    def test_cancellation_after_activation_alerts_and_preserves_exact_incomplete_copy(self) -> None:
        from control_plane.testing_lane_hold import staff_testing_hold_cancellation

        self.scheduler.run_once()
        operation = self.activate()
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "cancelled",
                    "phase": "cancelled",
                    "finished_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                    "cancellation": staff_testing_hold_cancellation(
                        datetime.fromtimestamp(self.now, timezone.utc).isoformat()
                    ),
                }
            )
        )
        self.scheduler.run_once()
        self.assertEqual(self.evidence().status, "fail")
        self.assertTrue(self.notifications.called)
        self.assertEqual(self.evidence().operation_id, operation.operation_id)

    def test_alert_retry_does_not_refresh_observation_and_duplicate_backlog_is_coalesced(
        self,
    ) -> None:
        self.notifications.return_value = False
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"artifact_identity": None})
        )
        for offset in range(3):
            self.store.enqueue_odoo_testing_drill(
                self.profile.product,
                self.context,
                f"{lane_key(self.profile.product, self.context)}:failure-{offset}",
                self.now + offset,
            )
        before = self.evidence().observed_at
        self.scheduler._deliver_alerts(
            self.profile.product, self.context, self.now + DRILL_ALERT_RETRY_SECONDS
        )
        self.assertEqual(self.notifications.call_count, 1)
        self.assertEqual(self.evidence().observed_at, before)
        self.assertEqual(
            len(
                self.store.list_pending_odoo_testing_drill_alerts(
                    self.profile.product, self.context
                )
            ),
            1,
        )
