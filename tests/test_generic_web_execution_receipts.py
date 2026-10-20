from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest
from typing import Any, cast
from unittest.mock import patch

from control_plane.dokploy import api
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    build_provider_operation_key,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_application_lifecycle import schedule_payload
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
)
from tests.test_generic_web_application_lifecycle import _extensions, _LifecycleDeployProvider
from tests.test_generic_web_deploy import _GenericWebDeployStore, _profile, _request
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.generic_web_testing_inputs import testing_input_authority
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.workflows.generic_web_execution import (
    ExecutionEntry,
    ExecutionReceipt,
    ExecutionRecorder,
    RecordedScheduleExecution,
    EXECUTION_EVIDENCE_KEY,
    evidence_digest,
    observe_execution,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    generic_web_provider_deployment_record_id,
)
from tests.test_generic_web_deploy_recovery import (
    _create_recovery_app,
    _generic_web_recovery_original_deploy,
    _generic_web_recovery_reservation,
    _write_generic_web_recovery_reservation,
    _generic_web_recovery_target,
    _RecoveryObservationProvider,
    _invoke_recovery,
    _invoke_recovery_apply,
)
from tests.support.profiles import product_profile_payload as _product_profile_payload
from control_plane.workflows.inventory import build_environment_inventory


class OriginalExecutionTests(unittest.TestCase):
    def test_foreign_response_and_history_cannot_complete_original_callback(self) -> None:
        records = _GenericWebDeployStore(_profile())
        records.write_deployment_record(
            DeploymentRecord(
                record_id="record",
                context="testing",
                instance="testing",
                source_git_ref="source",
                deploy=DeploymentEvidence(
                    target_name="app",
                    target_type="application",
                    deploy_mode="application",
                    status="pending",
                ),
            )
        )
        recorder = ExecutionRecorder(records, "record")
        observer = RecordedScheduleExecution(
            recorder, "migration", "schedule", {"applicationId": "app"}
        )
        observer.before_dispatch(())
        responses: tuple[api.JsonObject, ...] = (
            {"deploymentId": "job", "applicationId": "other"},
            {"deploymentId": "job", "schedule": {"scheduleId": "other"}},
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                observer.acknowledged(response)
        observer.acknowledged({"deploymentId": "job", "status": ["malformed"]})
        self.assertEqual(recorder.receipt.entries[0].status, "unknown")
        identities: tuple[api.JsonObject, ...] = (
            {"applicationId": "other"},
            {"schedule": {"applicationId": "other"}},
            {"schedule": {"scheduleId": "other"}},
        )
        for identity in identities:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                observer.completed(({"deploymentId": "job", "status": "done", **identity},))
        self.assertFalse(recorder.receipt.entries[0].terminal)

    def test_history_requires_original_acknowledgement_and_complete_terminal_rows(self) -> None:
        entry = ExecutionEntry(
            kind="schedule_run",
            name="migration",
            target_id="schedule",
            payload_digest=evidence_digest({}),
            baseline_ids=("before",),
        )
        done: api.JsonObject = {
            "deploymentId": "original",
            "scheduleId": "schedule",
            "status": "done",
        }
        foreign: api.JsonObject = {
            "deploymentId": "foreign",
            "scheduleId": "schedule",
            "status": "done",
        }
        cases = (
            (entry, (done,), "unknown"),
            (
                entry.model_copy(
                    update={
                        "acknowledgement": "identified",
                        "deployment_id": "original",
                        "status": "running",
                    }
                ),
                (done,),
                "terminal",
            ),
            (
                entry.model_copy(
                    update={"acknowledgement": "identified", "deployment_id": "original"}
                ),
                (foreign,),
                "unknown",
            ),
            (
                entry.model_copy(
                    update={"acknowledgement": "identified", "deployment_id": "original"}
                ),
                ({**done, "status": "error"},),
                "unknown",
            ),
            (
                entry.model_copy(
                    update={"acknowledgement": "identified", "deployment_id": "original"}
                ),
                ({**done, "scheduleId": "another"},),
                "unknown",
            ),
            (
                entry.model_copy(update={"acknowledgement": "synchronous"}),
                (done, foreign),
                "terminal",
            ),
            (
                entry.model_copy(update={"acknowledgement": "synchronous"}),
                (done, {**foreign, "status": "running"}),
                "unknown",
            ),
            (entry.model_copy(update={"acknowledgement": "synchronous"}), (), "unknown"),
            (entry.model_copy(update={"acknowledgement": "synchronous"}), (done, done), "unknown"),
        )
        for receipt_entry, history, outcome in cases:
            with self.subTest(acknowledgement=receipt_entry.acknowledgement, history=history):
                self.assertEqual(
                    observe_execution(
                        ExecutionReceipt(entries=(receipt_entry,)), lambda _schedule: history
                    ).outcome,
                    outcome,
                )

    def test_lost_activation_stays_unknown_after_completed_disable(self) -> None:
        enable = ExecutionEntry(
            kind="schedule_write",
            name="activate",
            target_id="app",
            payload_digest=evidence_digest({"enabled": True}),
        )
        hold = ExecutionEntry(
            kind="schedule_write",
            name="hold",
            target_id="app",
            payload_digest=evidence_digest({"enabled": False}),
            acknowledgement="completed",
            terminal="write_completed",
        )
        self.assertEqual(
            observe_execution(ExecutionReceipt(entries=(enable, hold)), lambda _id: ()).outcome,
            "unknown",
        )

    def test_schedule_receipt_persistence_precedes_trigger_and_uses_execution_budget(self) -> None:
        records = _GenericWebDeployStore(_profile())
        request = _request()
        record = DeploymentRecord(
            record_id="record",
            context="testing",
            instance="testing",
            source_git_ref=request.source_git_ref,
            deploy=DeploymentEvidence(
                target_name="app",
                target_type="application",
                deploy_mode="application",
                status="pending",
            ),
        )
        records.write_deployment_record(record)
        recorder = ExecutionRecorder(records, record.record_id)
        observer = RecordedScheduleExecution(recorder, "migration", "schedule", {})
        calls: list[str] = []
        reply: api.JsonValue = {"deploymentId": "job", "status": "done"}

        def provider(**kwargs: object) -> api.JsonValue:
            path = str(kwargs["path"])
            calls.append(path)
            if path == "/api/schedule.runManually":
                self.assertEqual(kwargs["timeout_seconds"], 901)
                self.assertEqual(recorder.receipt.entries[-1].acknowledgement, "none")
                return reply
            return (
                []
                if len(calls) == 1
                else [{"deploymentId": "job", "status": "done", "scheduleId": "schedule"}]
            )

        with patch.object(api, "dokploy_request", side_effect=provider):
            api.run_dokploy_schedule(
                host="https://provider.invalid",
                token="fixture",
                schedule_id="schedule",
                timeout_seconds=901,
                execution_observer=observer,
            )
        self.assertEqual(recorder.receipt.entries[0].terminal, "done")
        calls.clear()
        reply = True
        observer = RecordedScheduleExecution(recorder, "legacy", "schedule", {})
        with patch.object(api, "dokploy_request", side_effect=provider):
            api.run_dokploy_schedule(
                host="https://provider.invalid",
                token="fixture",
                schedule_id="schedule",
                timeout_seconds=901,
                execution_observer=observer,
            )
        self.assertEqual(recorder.receipt.entries[-1].acknowledgement, "synchronous")
        self.assertEqual(recorder.receipt.entries[-1].terminal, "done")
        calls.clear()
        observer = RecordedScheduleExecution(
            ExecutionRecorder(records, record.record_id, recorder.receipt),
            "another",
            "schedule",
            {},
        )
        with (
            patch.object(
                records, "write_deployment_record", side_effect=OSError("receipt unavailable")
            ),
            patch.object(api, "dokploy_request", side_effect=provider),
        ):
            with self.assertRaises(OSError):
                api.run_dokploy_schedule(
                    host="https://provider.invalid",
                    token="fixture",
                    schedule_id="schedule",
                    timeout_seconds=901,
                    execution_observer=observer,
                )
        self.assertNotIn("/api/schedule.runManually", calls)

    def test_unknown_maintenance_keeps_original_and_blocks_successor(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
                )
            ) as reservations,
        ):
            reservations.ensure_schema()
            profile = _profile()
            extensions = _extensions()
            lane = profile.lanes[0].model_copy(update={"generic_web_extensions": extensions})
            profile = profile.model_copy(update={"lanes": (lane,)})
            records = _GenericWebDeployStore(profile)
            schedules = {
                item.name: schedule_payload(item, "app-123", enabled=True)
                for item in extensions.schedules
            }
            provider = _LifecycleDeployProvider(
                target_id="app-123",
                observation=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="image-job",
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=datetime.now(UTC).isoformat(),
                ),
            )
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=Path(directory),
                record_store=records,
                deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
                profile=profile,
                lane=lane,
                trace_id="unknown-maintenance",
                deploy_provider=provider,
            )
            triggers: list[str] = []

            def read(**kwargs: object) -> api.JsonObject | None:
                value = schedules.get(str(kwargs["schedule_name"]))
                return dict(value) if value else None

            def upsert(**kwargs: object) -> api.JsonObject:
                payload = api.as_json_object(cast(api.JsonValue, kwargs["schedule_payload"]))
                assert payload is not None
                schedules[str(payload["name"])] = dict(payload)
                return {**payload, "scheduleId": payload["name"]}

            def run(**kwargs: object) -> str:
                callback = kwargs.get("before_trigger")
                if callable(callback):
                    callback()
                observer = kwargs.get("execution_observer")
                if observer is not None:
                    cast(api.ScheduleExecutionObserver, observer).before_dispatch(())
                triggers.append(str(kwargs["schedule_id"]))
                raise api.DokployScheduleExecutionFailed(
                    schedule_id=str(kwargs["schedule_id"]),
                    deployment_id="unresolved-job",
                    deployment_status="running",
                    cause="execution_timeout",
                )

            def run_operation(key: str) -> DurableProviderOperationResult:
                return run_durable_provider_operation(
                    store=reservations,
                    scope="fixture",
                    route_path="/v1/drivers/generic-web/deploy",
                    request_fingerprint="fixture-request",
                    lease_owner="worker",
                    response_trace_id="trace",
                    adapter=adapter,
                    lease_seconds=300,
                    heartbeat_interval_seconds=60,
                    idempotency_key=key,
                )

            with (
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://provider.invalid", "fixture"),
                ),
                patch.object(api, "read_dokploy_application_schedule", side_effect=read),
                patch.object(api, "upsert_dokploy_application_schedule", side_effect=upsert),
                patch.object(api, "run_dokploy_schedule", side_effect=run),
            ):
                first = run_operation("original")
                successor = run_operation("successor")
            assert first.record is not None
            self.assertEqual(first.record.state, "reconcile_required")
            self.assertEqual(successor.status, "target_busy")
            self.assertEqual(triggers, [extensions.maintenance[0].name])


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
        self.store.write_product_profile_record(self.profile)
        self.request = _generic_web_recovery_original_deploy()
        self.target = _generic_web_recovery_target()
        self.write_target_authority()
        self.app = _create_recovery_app(root=self.root, store=self.store)
        self.provider = _RecoveryObservationProvider(
            GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="image",
                started_at="2026-08-15T12:00:00Z",
                finished_at="2026-08-15T12:01:00Z",
            )
        )

    def write_target_authority(self) -> None:
        lane = self.profile.lanes[0]
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=lane.context,
                instance="testing",
                target_type="application",
                target_name=self.target.resolved_target.target_name,
                updated_at="2026-08-15T12:00:00Z",
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=lane.context,
                instance="testing",
                target_id=self.target.resolved_target.target_id,
                updated_at="2026-08-15T12:00:00Z",
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=lane.context,
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                target_id=self.target.resolved_target.target_id,
                display_name="testing",
                updated_at="2026-08-15T12:00:00Z",
            )
        )

    def prepare(
        self,
        phase: str,
        receipt: ExecutionReceipt | None,
        *,
        qualified: bool = False,
        snapshot: ApplicationLifecycleSnapshot | None = None,
    ) -> str:
        reservation = _write_generic_web_recovery_reservation(
            self.store,
            _generic_web_recovery_reservation(
                original_deploy=self.request,
                idempotency_key=phase,
                state="running",
                provider_effect_phase=phase,
            ),
        )
        operation = build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
        record_id = generic_web_provider_deployment_record_id(
            operation, context=self.target.ship_request.context, instance="testing"
        )
        record = DeploymentRecord(
            record_id=record_id,
            artifact_identity=ArtifactIdentityReference(
                artifact_id=self.target.ship_request.artifact_id
            ),
            context=self.target.ship_request.context,
            instance="testing",
            source_git_ref=self.target.ship_request.source_git_ref,
            resolved_target=self.target.resolved_target,
            deploy=DeploymentEvidence(
                target_name=self.target.resolved_target.target_name,
                target_type="application",
                deploy_mode="application",
                status="pass",
                deployment_id="image",
            ),
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=not qualified,
                status="skipped" if qualified else "pending",
                evidence={
                    **({EXECUTION_EVIDENCE_KEY: receipt.model_dump_json()} if receipt else {}),
                    **(snapshot.evidence() if snapshot else {}),
                },
            ),
        )
        with self.store.provider_evidence_guard(lambda: reservation):
            self.store.write_deployment_record(record)
        self.store.mark_mutation_reconcile_required(
            reservation=reservation, reconciliation_key=reservation.reconciliation_key
        )
        self.reservation = reservation
        return record_id

    def test_ingress_cannot_insert_an_original_execution_receipt(self) -> None:
        record_id = self.prepare("deploy_trigger", None)
        record = self.store.read_deployment_record(record_id)
        changed = record.model_copy(
            update={
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={
                        "evidence": {EXECUTION_EVIDENCE_KEY: ExecutionReceipt().model_dump_json()}
                    }
                )
            }
        )
        with self.assertRaises(ValueError):
            self.store.write_deployment_record(changed)

    def test_ingress_cannot_advance_receipt_or_replace_original_hold_baseline(self) -> None:
        snapshot = ApplicationLifecycleSnapshot(
            product=self.profile.product,
            context=self.target.ship_request.context,
            instance="testing",
            provider_id="dokploy",
            target_id=self.target.resolved_target.target_id,
            artifact_id=self.target.ship_request.artifact_id,
            source_git_ref=self.target.ship_request.source_git_ref,
            extensions=_extensions(),
            originals={"job": {"enabled": True}},
            managed_names=("job",),
        )
        record_id = self.prepare("records_commit", ExecutionReceipt(), snapshot=snapshot)
        record = self.store.read_deployment_record(record_id)
        appended = ExecutionReceipt(
            entries=(
                ExecutionEntry(
                    kind="product_executor",
                    name="forged",
                    target_id="app",
                    payload_digest=evidence_digest({}),
                    acknowledgement="completed",
                    terminal="executor_completed",
                ),
            )
        )
        for evidence in (
            {EXECUTION_EVIDENCE_KEY: appended.model_dump_json()},
            snapshot.model_copy(update={"originals": {"job": {"enabled": False}}}).evidence(),
        ):
            changed = record.model_copy(
                update={
                    "post_deploy_update": record.post_deploy_update.model_copy(
                        update={"evidence": {**record.post_deploy_update.evidence, **evidence}}
                    )
                }
            )
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.store.write_deployment_record(changed)
        corrected = record.model_copy(
            update={
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={"detail": "Corrected diagnostic."}
                )
            }
        )
        self.store.write_deployment_record(corrected)
        self.assertEqual(self.store.read_deployment_record(record_id), corrected)

    def test_exact_acknowledged_running_job_can_finish_and_publish(self) -> None:
        class Provider(_RecoveryObservationProvider):
            status = "running"

            def application_execution_history(
                self, **_kwargs: object
            ) -> tuple[api.JsonObject, ...]:
                return ({"deploymentId": "job", "status": self.status, "scheduleId": "schedule"},)

        provider = Provider(self.provider.observation)
        receipt = ExecutionReceipt(
            entries=(
                ExecutionEntry(
                    kind="schedule_run",
                    name="canary",
                    target_id="schedule",
                    payload_digest=evidence_digest({}),
                    acknowledgement="identified",
                    deployment_id="job",
                    status="running",
                ),
            )
        )
        record_id = self.prepare("records_commit", receipt, qualified=True)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=provider,
        ):
            status, held = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key="records_commit",
                reason="Observe original job.",
            )
            self.assertEqual(held["proposed_action"], "hold_unknown")
            provider.status = "done"
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key="records_commit",
                reason="Observe original job.",
            )
            self.assertEqual(dry["proposed_action"], "adopt_observed")
            status, result = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key="records_commit",
                reason="Observe original job.",
                expected_recovery_digest=dry["recovery_digest"],
            )
        self.assertEqual(status, 202)
        self.assertEqual(result["reservation_state"], "completed")
        saved = ExecutionReceipt.model_validate_json(
            self.store.read_deployment_record(record_id).post_deploy_update.evidence[
                EXECUTION_EVIDENCE_KEY
            ]
        )
        self.assertEqual(saved.entries[0].status, "running")
        self.assertEqual(saved.entries[0].terminal, "done")

    def test_publication_recovers_unchanged_prior_inventory_and_refuses_later_inventory(
        self,
    ) -> None:
        prior = DeploymentRecord(
            record_id="prior",
            context=self.target.ship_request.context,
            instance="testing",
            source_git_ref="older",
            deploy=DeploymentEvidence(
                target_name="testing",
                target_type="application",
                deploy_mode="application",
                status="pass",
            ),
        )
        inventory = build_environment_inventory(
            deployment_record=prior, updated_at="2026-08-15T11:00:00Z"
        )
        self.store.write_environment_inventory(inventory)
        phase = "records_commit"
        record_id = self.prepare(
            phase,
            ExecutionReceipt(
                inventory_baseline_digest=evidence_digest(inventory.model_dump(mode="json"))
            ),
            qualified=True,
        )
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete original publication.",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dry["proposed_action"], "adopt_observed")
            changed = inventory.model_copy(update={"deployment_record_id": "later"})
            self.store.write_environment_inventory(changed)
            status, refusal = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete original publication.",
                expected_recovery_digest=dry["recovery_digest"],
            )
            self.assertEqual(status, 409)
            self.assertEqual(refusal["error"]["code"], "stale_recovery_digest")
            self.store.write_environment_inventory(inventory)
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete original publication.",
            )
            status, applied = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete original publication.",
                expected_recovery_digest=dry["recovery_digest"],
            )
        self.assertEqual(status, 202)
        self.assertEqual(applied["reservation_state"], "completed")
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=inventory.context, instance_name="testing"
            ).deployment_record_id,
            record_id,
        )

    def test_publication_rechecks_actual_config_bundle_after_lease_cas(self) -> None:
        lane = self.profile.lanes[0]
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context=lane.context,
            instance="testing",
            env={"SITE_MODE": "reviewed"},
            updated_at="2026-08-15T12:00:00Z",
        )

        def write_settings(value: str) -> None:
            with closing(
                PostgresRecordStore(database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}")
            ) as writer:
                writer.write_product_authority_bundle(
                    ProductAuthorityBundle(
                        required_product_config_target=(
                            self.profile.product,
                            lane.context,
                            "testing",
                        ),
                        runtime_environments=(
                            settings.model_copy(update={"env": {"SITE_MODE": value}}),
                        ),
                    )
                )

        with patch.dict(os.environ, {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-master-key"}):
            write_settings("reviewed")
            self.target = self.target.model_copy(
                update={
                    "testing_runtime_digest": testing_input_authority(
                        self.store, self.profile, lane
                    )
                }
            )
            self.assertTrue(self.target.testing_runtime_digest)
            with patch(
                "tests.test_generic_web_deploy_recovery._generic_web_recovery_target",
                return_value=self.target,
            ):
                record_id = self.prepare("records_commit", ExecutionReceipt(), qualified=True)
            original = self.store.publish_generic_web_testing_recovery

            def change_then_publish(**kwargs: Any) -> None:
                write_settings("changed-after-checkpoint")
                original(**kwargs)

            with patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            ):
                status, dry = _invoke_recovery(
                    self.app,
                    original_deploy=self.request,
                    idempotency_key="records_commit",
                    reason="Publish reviewed completion.",
                )
                self.assertEqual(dry["proposed_action"], "adopt_observed")
                with patch.object(
                    self.store,
                    "publish_generic_web_testing_recovery",
                    side_effect=change_then_publish,
                ):
                    status, refused = _invoke_recovery_apply(
                        self.app,
                        original_deploy=self.request,
                        idempotency_key="records_commit",
                        reason="Publish reviewed completion.",
                        expected_recovery_digest=dry["recovery_digest"],
                    )
        self.assertEqual(status, 409)
        self.assertEqual(refused["error"]["code"], "mutation_reconciliation_required")
        self.assertEqual(
            self.store.read_deployment_record(record_id).post_deploy_update.status, "skipped"
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(
                context_name=lane.context, instance_name="testing"
            )

    def test_publication_refuses_inventory_changed_after_lease_cas(self) -> None:
        record_id = self.prepare("records_commit", ExecutionReceipt(), qualified=True)
        record = self.store.read_deployment_record(record_id)
        newer = build_environment_inventory(
            deployment_record=record, updated_at="2026-08-15T12:01:00Z"
        ).model_copy(update={"deployment_record_id": "newer"})
        original = self.store.publish_generic_web_testing_recovery

        def change_then_publish(**kwargs: Any) -> None:
            with closing(
                PostgresRecordStore(database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}")
            ) as writer:
                writer.write_environment_inventory(newer)
            original(**kwargs)

        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key="records_commit",
                reason="Publish reviewed completion.",
            )
            with patch.object(
                self.store, "publish_generic_web_testing_recovery", side_effect=change_then_publish
            ):
                status, _refused = _invoke_recovery_apply(
                    self.app,
                    original_deploy=self.request,
                    idempotency_key="records_commit",
                    reason="Publish reviewed completion.",
                    expected_recovery_digest=dry["recovery_digest"],
                )
        self.assertEqual(status, 409)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=newer.context, instance_name="testing"
            ),
            newer,
        )
        self.assertEqual(self.store.read_deployment_record(record_id), record)

    def test_legacy_image_only_publication_refuses_repointed_current_target(self) -> None:
        record_id = self.prepare("deploy_trigger", None, qualified=True)
        record = self.store.read_deployment_record(record_id)
        original = self.store.publish_generic_web_testing_recovery

        def repoint_then_publish(**kwargs: Any) -> None:
            lane = self.profile.lanes[0]
            with closing(
                PostgresRecordStore(database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}")
            ) as writer:
                binding = writer.read_dokploy_target_id_record(
                    context_name=lane.context, instance_name="testing"
                )
                provider = writer.read_provider_target_record(
                    context_name=lane.context, instance_name="testing"
                )
                writer.write_dokploy_target_id_record(
                    binding.model_copy(update={"target_id": "replacement"})
                )
                writer.write_provider_target_record(
                    provider.model_copy(update={"target_id": "replacement"})
                )
            original(**kwargs)

        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key="deploy_trigger",
                reason="Publish historical image.",
            )
            self.assertEqual(dry["proposed_action"], "adopt_observed")
            with patch.object(
                self.store, "publish_generic_web_testing_recovery", side_effect=repoint_then_publish
            ):
                status, _refused = _invoke_recovery_apply(
                    self.app,
                    original_deploy=self.request,
                    idempotency_key="deploy_trigger",
                    reason="Publish historical image.",
                    expected_recovery_digest=dry["recovery_digest"],
                )
        self.assertEqual(status, 409)
        self.assertEqual(self.store.read_deployment_record(record_id), record)

    def test_six_cleanup_and_executable_phases_without_proof_stay_unknown(self) -> None:
        for phase in (
            "application_maintenance_execute",
            "application_schedule_canary",
            "application_schedule_activate",
            "post_deploy_fixture_signup",
            "application_schedule_hold",
            "records_commit",
        ):
            with (
                self.subTest(phase=phase),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
                    )
                ) as store,
            ):
                self.store = store
                self.store.ensure_schema()
                self.store.write_product_profile_record(self.profile)
                self.app = _create_recovery_app(root=Path(directory), store=store)
                self.prepare(phase, None)
                with (
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                        return_value=self.provider,
                    ),
                    patch.object(
                        api,
                        "dokploy_request",
                        side_effect=AssertionError("No original execution reference exists"),
                    ),
                ):
                    status, payload = _invoke_recovery(
                        self.app,
                        original_deploy=self.request,
                        idempotency_key=phase,
                        reason="Inspect original effects.",
                    )
                self.assertEqual(status, 200)
                self.assertEqual(payload["proposed_action"], "hold_unknown")

    def test_healthy_image_terminal_unqualified_settles_once_without_inventory_success(
        self,
    ) -> None:
        phase = "post_deploy_finished"
        entry = ExecutionEntry(
            kind="product_executor",
            name="verification",
            target_id=self.target.resolved_target.target_id,
            payload_digest=evidence_digest({}),
            acknowledgement="completed",
            terminal="executor_completed",
        )
        record_id = self.prepare(phase, ExecutionReceipt(entries=(entry,)))
        lane = self.profile.lanes[0]
        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            ),
            patch.object(
                api, "dokploy_request", side_effect=AssertionError("No commands are allowed")
            ),
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Settle terminal unqualified effects.",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dry["proposed_action"], "settle_testing_failure")
            status, result = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Settle terminal unqualified effects.",
                expected_recovery_digest=dry["recovery_digest"],
            )
            self.assertEqual(status, 202)
            self.assertEqual(result["reservation_state"], "completed")
            observations = self.provider.observation_calls
            status, replay = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Settle terminal unqualified effects.",
                expected_recovery_digest=dry["recovery_digest"],
            )
        self.assertEqual(status, 202)
        self.assertEqual(replay["recovery_action"], "settle_testing_failure")
        self.assertEqual(self.provider.observation_calls, observations)
        saved = self.store.read_deployment_record(record_id)
        self.assertEqual(saved.deploy.status, "pass")
        self.assertEqual(saved.post_deploy_update.status, "fail")
        completed = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert completed is not None
        self.assertEqual(completed.response_status_code, 502)
        self.assertEqual(completed.response_payload["result"]["deploy_status"], "fail")
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(
                context_name=lane.context, instance_name="testing"
            )

    def test_terminal_unqualified_settlement_only_holds_captured_schedules(self) -> None:
        class Provider(_RecoveryObservationProvider, DokployGenericWebDeployProvider):
            pass

        extensions = _extensions()
        definition = extensions.schedules[0]
        original = schedule_payload(definition, self.target.resolved_target.target_id, enabled=True)
        snapshot = ApplicationLifecycleSnapshot(
            product=self.profile.product,
            context=self.target.ship_request.context,
            instance="testing",
            provider_id="dokploy",
            target_id=self.target.resolved_target.target_id,
            artifact_id=self.target.ship_request.artifact_id,
            source_git_ref=self.target.ship_request.source_git_ref,
            extensions=extensions,
            originals={definition.name: original},
            managed_names=(definition.name,),
        )
        record_id = self.prepare("post_deploy_finished", ExecutionReceipt(), snapshot=snapshot)
        schedules = {definition.name: original}
        writes: list[api.JsonObject] = []

        def upsert(**kwargs: object) -> api.JsonObject:
            payload = cast(api.JsonObject, kwargs["schedule_payload"])
            self.assertFalse(payload["enabled"])
            schedules[str(payload["name"])] = payload
            writes.append(payload)
            return payload

        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=Provider(self.provider.observation),
            ),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch.object(
                api,
                "read_dokploy_application_schedule",
                side_effect=lambda **kwargs: schedules.get(kwargs["schedule_name"]),
            ),
            patch.object(api, "upsert_dokploy_application_schedule", side_effect=upsert),
            patch.object(
                api,
                "run_dokploy_schedule",
                side_effect=AssertionError("Settlement must not execute commands"),
            ),
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key="post_deploy_finished",
                reason="Hold terminal unqualified work.",
            )
            status, result = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key="post_deploy_finished",
                reason="Hold terminal unqualified work.",
                expected_recovery_digest=dry["recovery_digest"],
            )
            self.assertEqual(status, 202)
            self.assertEqual(result["reservation_state"], "completed")
            status, _replay = _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key="post_deploy_finished",
                reason="Hold terminal unqualified work.",
                expected_recovery_digest=dry["recovery_digest"],
            )
        self.assertEqual(status, 202)
        self.assertEqual(len(writes), 1)
        saved = self.store.read_deployment_record(record_id)
        from control_plane.workflows.generic_web_application_snapshot import (
            recorded_application_lifecycle,
        )

        held = recorded_application_lifecycle(saved)
        assert held is not None
        self.assertEqual(held.state, "held")
        self.assertEqual(held.originals, snapshot.originals)
