from contextlib import closing, ExitStack
from datetime import UTC, datetime
from pathlib import Path
import os
import io
from email.message import Message
from tempfile import TemporaryDirectory
import unittest
from typing import Any, cast
from unittest.mock import patch
import click
from urllib.error import HTTPError, URLError

from control_plane.dokploy import api
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_source_qualification import (
    GenericWebSourceUnqualified,
    GenericWebPromotionSourceRefused,
    qualified_generic_web_source_inventory,
)
from control_plane.release_review import release_version, ReleaseEvidenceUnavailable
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    resolve_generic_web_promotion_inputs,
)
from control_plane.workflows.verireel_app_maintenance import (
    _request_internal_smoke_maintenance_api,
    VeriReelAppMaintenanceRequest,
)
from tests.test_generic_web_promotion import _store_with_production
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderOperationLease,
    ProviderEvidenceLease,
    ProviderObservation,
    ProviderMutationUnknownError,
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
from tests.test_generic_web_deploy import (
    _GenericWebDeployStore,
    _FakeGenericWebDeployProvider,
    _profile,
    _request,
)
from control_plane.contracts.deployment_record import (
    DeploymentRecord,
    IntegrationKeyReadbackEvidence,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.generic_web_testing_inputs import testing_input_authority
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
    RecordFailure,
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
from control_plane.workflows.evidence_ingestion import apply_deployment_evidence
from control_plane.generic_web_testing_settlement import settle_testing_pre_image_attempt


class OriginalExecutionTests(unittest.TestCase):
    def test_real_post_image_transport_errors_keep_current_source_unqualified(self) -> None:
        for failure in ("product_transport", "product_server", "canary_server"):
            with (
                self.subTest(failure=failure),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
                ) as store,
                ExitStack() as patches,
            ):
                fixture = _store_with_production()
                profile = fixture.profile
                lane = profile.lanes[0]
                if failure == "canary_server":
                    lane = lane.model_copy(
                        update={
                            "generic_web_extensions": _extensions().model_copy(
                                update={"maintenance": ()}
                            )
                        }
                    )
                    profile = profile.model_copy(update={"lanes": (lane, *profile.lanes[1:])})
                fixture.profile = profile
                prior = fixture.read_environment_inventory(
                    context_name=lane.context, instance_name="testing"
                )
                assert prior.runtime_identity is not None
                fixture.write_qualified_testing_inventory(
                    prior.model_copy(
                        update={
                            "source_git_ref": "b" * 40,
                            "runtime_identity": prior.runtime_identity.model_copy(
                                update={"source_git_ref": "b" * 40}
                            ),
                        }
                    )
                )
                store.ensure_schema()
                store.write_product_profile_record(profile)
                for record in fixture.deployments.values():
                    store.write_deployment_record(record)
                for inventory in fixture.inventories.values():
                    store.write_environment_inventory(inventory)
                release_version(store=store, profile=profile, instance="testing")
                provider_type = (
                    _LifecycleDeployProvider
                    if failure == "canary_server"
                    else _FakeGenericWebDeployProvider
                )
                provider = provider_type(
                    target_id="fixture-target",
                    observation=GenericWebProviderDeploymentObservation(
                        outcome="present",
                        deployment_status="success",
                        deployment_id="new-image",
                        started_at="2026-10-20T20:00:00Z",
                        finished_at="2026-10-20T20:01:00Z",
                    ),
                )
                adapter = GenericWebDeployProviderMutationAdapter(
                    control_plane_root=Path(directory),
                    record_store=store,
                    deploy_request=GenericWebDeployEnvelope(
                        product=profile.product,
                        deploy=_request().model_copy(update={"source_git_ref": "b" * 40}),
                    ),
                    profile=profile,
                    lane=lane,
                    trace_id="source-uncertainty",
                    deploy_provider=provider,
                )
                target = adapter._resolve_deploy_target()
                store.write_dokploy_target_record(
                    DokployTargetRecord(
                        context=lane.context,
                        instance="testing",
                        target_type="application",
                        target_name=target.resolved_target.target_name,
                        updated_at=provider.observation.started_at,
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context=lane.context,
                        instance="testing",
                        target_id=target.resolved_target.target_id,
                        updated_at=provider.observation.started_at,
                    )
                )
                store.write_provider_target_record(
                    ProviderTargetRecord(
                        context=lane.context,
                        instance="testing",
                        target_id=target.resolved_target.target_id,
                        provider_id=provider.provider_id,
                        target_category="application",
                        provider_target_type="application",
                        display_name="fixture",
                        updated_at=provider.observation.started_at,
                    )
                )
                calls: list[str] = []

                def response_lost(*_args: object, **_kwargs: object) -> Any:
                    current = store.read_environment_inventory(
                        context_name=lane.context, instance_name="testing"
                    )
                    self.assertEqual(current.post_deploy_update.status, "pending")
                    calls.append(failure)
                    if failure == "product_transport":
                        raise URLError("Response lost after dispatch.")
                    error = HTTPError(
                        "https://fixture.invalid",
                        500,
                        "Remote error",
                        Message(),
                        io.BytesIO(b"failed"),
                    )
                    self.addCleanup(error.close)
                    raise error

                if failure.startswith("product"):

                    def executor(
                        root: Path, records: object, context: Any, guard: Any
                    ) -> PostDeployUpdateEvidence:
                        _request_internal_smoke_maintenance_api(
                            base_url="https://fixture.invalid",
                            secret="fixture-only",
                            request=VeriReelAppMaintenanceRequest(
                                action="grant-sponsored",
                                intent="stable-testing-remote-e2e-grant-sponsored",
                                email="fixture@example.com",
                            ),
                            execution_guard=guard,
                        )
                        return PostDeployUpdateEvidence(attempted=True, status="pass")

                    patches.enter_context(
                        patch(
                            "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile",
                            return_value=executor,
                        )
                    )
                    patches.enter_context(
                        patch(
                            "control_plane.workflows.verireel_app_maintenance.urlopen",
                            side_effect=response_lost,
                        )
                    )
                else:
                    schedules: dict[str, api.JsonObject] = {}

                    def upsert(**kwargs: Any) -> api.JsonObject:
                        payload = dict(kwargs["schedule_payload"])
                        schedules[str(payload["name"])] = payload
                        return {**payload, "scheduleId": payload["name"]}

                    patches.enter_context(
                        patch(
                            "control_plane.dokploy.source.read_dokploy_config",
                            return_value=("https://fixture.invalid", "fixture-only"),
                        )
                    )
                    patches.enter_context(
                        patch.object(
                            api,
                            "read_dokploy_application_schedule",
                            side_effect=lambda **kwargs: schedules.get(kwargs["schedule_name"]),
                        )
                    )
                    patches.enter_context(
                        patch.object(api, "upsert_dokploy_application_schedule", side_effect=upsert)
                    )
                    patches.enter_context(
                        patch.object(api, "_read_schedule_deployments", return_value=[])
                    )
                    patches.enter_context(patch.object(api, "urlopen", side_effect=response_lost))
                outcome = run_durable_provider_operation(
                    store=store,
                    scope="fixture",
                    route_path="/v1/drivers/generic-web/deploy",
                    request_fingerprint=failure,
                    idempotency_key=failure,
                    lease_owner="fixture",
                    response_trace_id="fixture",
                    adapter=adapter,
                )
                self.assertEqual(outcome.status, "reconcile_required")
                self.assertTrue(calls)
                current = store.read_environment_inventory(
                    context_name=lane.context, instance_name="testing"
                )
                held = store.read_deployment_record(current.deployment_record_id)
                self.assertTrue(
                    any(
                        not entry.terminal
                        for entry in ExecutionReceipt.model_validate_json(
                            held.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY]
                        ).entries
                    )
                )
                with self.assertRaises(ReleaseEvidenceUnavailable):
                    release_version(store=store, profile=profile, instance="testing")
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    resolve_generic_web_promotion_inputs(
                        record_store=store,
                        request=GenericWebProdPromotionRequest(product=profile.product),
                    )

    def test_durable_terminal_run_does_not_reobserve_later_cron_history(self) -> None:
        entry = ExecutionEntry(
            kind="schedule_run",
            name="canary",
            target_id="schedule",
            payload_digest=evidence_digest({}),
            acknowledgement="synchronous",
            terminal="done",
        )
        reads: list[str] = []

        def history(schedule: str) -> tuple[api.JsonObject, ...]:
            reads.append(schedule)
            return ({"deploymentId": "later-cron", "status": "running"},)

        observation = observe_execution(ExecutionReceipt(entries=(entry,)), history)
        self.assertEqual(observation.outcome, "terminal")
        self.assertEqual(reads, [])

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
            self.assertEqual(records.inventories, [])
            self.assertEqual(records.deployments[0].deploy.status, "pending")


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

    def complete_original(
        self, phase: str, *, action: str = "adopt_observed"
    ) -> tuple[int, dict[str, Any]]:
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            status, dry = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete only original work.",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dry["proposed_action"], action)
            return _invoke_recovery_apply(
                self.app,
                original_deploy=self.request,
                idempotency_key=phase,
                reason="Complete only original work.",
                expected_recovery_digest=dry["recovery_digest"],
            )

    def test_recovery_preserves_already_final_corrected_record_and_inventory(self) -> None:
        record_id = self.prepare("records_commit", ExecutionReceipt(), qualified=True)
        record = self.store.read_deployment_record(record_id)
        corrected = record.model_copy(
            update={
                "integration_key_readback": IntegrationKeyReadbackEvidence(
                    status="pass", checked_binding_keys=("fixture-key",)
                ),
                "failure": RecordFailure(
                    code="fixture-correction", description="Retained diagnostic"
                ),
                "deploy": record.deploy.model_copy(update={"started_at": "2026-08-15T11:59:00Z"}),
            }
        )
        apply_deployment_evidence(record_store=self.store, deployment_record=corrected)
        inventory = self.store.read_environment_inventory(
            context_name=corrected.context, instance_name="testing"
        ).model_copy(update={"bootstrap_record_id": "retained-correction"})
        self.store.write_environment_inventory(inventory)
        status, result = self.complete_original("records_commit")
        self.assertEqual(status, 202)
        self.assertEqual(result["reservation_state"], "completed")
        self.assertEqual(self.store.read_deployment_record(record_id), corrected)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=inventory.context, instance_name="testing"
            ),
            inventory,
        )

    def test_historical_completion_keeps_newer_inventory_and_original_evidence(self) -> None:
        record_id = self.prepare("records_commit", ExecutionReceipt(), qualified=True)
        original = self.store.read_deployment_record(record_id)
        newer = original.model_copy(
            update={
                "record_id": "newer",
                "post_deploy_update": original.post_deploy_update.model_copy(
                    update={"evidence": {}}
                ),
            }
        )
        apply_deployment_evidence(record_store=self.store, deployment_record=newer)
        inventory = self.store.read_environment_inventory(
            context_name=original.context, instance_name="testing"
        )
        status, result = self.complete_original("records_commit")
        self.assertEqual(status, 202)
        self.assertEqual(result["reservation_state"], "completed")
        self.assertEqual(self.store.read_deployment_record(record_id), original)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=original.context, instance_name="testing"
            ),
            inventory,
        )

    def test_legacy_failed_image_completes_failed_without_new_inventory(self) -> None:
        record_id = self.prepare("deploy_trigger", None, qualified=True)
        record = self.store.read_deployment_record(record_id)
        self.store.write_deployment_record(
            record.model_copy(
                update={"deploy": record.deploy.model_copy(update={"status": "fail"})}
            )
        )
        self.provider.observation = self.provider.observation.model_copy(
            update={"deployment_status": "error"}
        )
        status, result = self.complete_original("deploy_trigger")
        self.assertEqual(status, 202)
        self.assertEqual(result["reservation_state"], "completed")
        completed = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key="deploy_trigger",
        )
        assert completed is not None
        self.assertEqual(completed.response_status_code, 502)
        self.assertEqual(completed.response_payload["result"]["deploy_status"], "fail")
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(
                context_name=record.context, instance_name="testing"
            )

    def test_settlement_updates_failed_qualification_only_on_original_inventory_baseline(
        self,
    ) -> None:
        for independent in (False, True):
            with self.subTest(independent=independent):
                key = f"terminal-failure-{independent}"
                prior = DeploymentRecord(
                    record_id=f"prior-{independent}",
                    context=self.target.ship_request.context,
                    instance="testing",
                    source_git_ref=self.target.ship_request.source_git_ref,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=self.target.ship_request.artifact_id
                    ),
                    deploy=DeploymentEvidence(
                        target_name="testing",
                        target_type="application",
                        deploy_mode="application",
                        status="pass",
                        deployment_id="previous-image",
                    ),
                )
                apply_deployment_evidence(record_store=self.store, deployment_record=prior)
                baseline = self.store.read_environment_inventory(
                    context_name=prior.context, instance_name="testing"
                )
                entry = ExecutionEntry(
                    kind="product_executor",
                    name="verify",
                    target_id=self.target.resolved_target.target_id,
                    payload_digest=evidence_digest({}),
                    acknowledgement="completed",
                    terminal="executor_completed",
                )
                record_id = self.prepare(
                    "post_deploy_finished",
                    ExecutionReceipt(
                        inventory_baseline_digest=evidence_digest(baseline.model_dump(mode="json")),
                        entries=(entry,),
                    ),
                    idempotency_key=key,
                )
                if independent:
                    newer = prior.model_copy(update={"record_id": f"newer-{independent}"})
                    apply_deployment_evidence(record_store=self.store, deployment_record=newer)
                    baseline = self.store.read_environment_inventory(
                        context_name=prior.context, instance_name="testing"
                    )
                status, result = self.complete_original(key, action="settle_testing_failure")
                self.assertEqual(status, 202)
                self.assertEqual(result["reservation_state"], "completed")
                inventory = self.store.read_environment_inventory(
                    context_name=prior.context, instance_name="testing"
                )
                if independent:
                    self.assertEqual(inventory, baseline)
                else:
                    self.assertEqual(inventory.deployment_record_id, record_id)
                    self.assertEqual(inventory.post_deploy_update.status, "fail")

    def test_receipt_merge_preserves_correction_between_recorder_read_and_write(self) -> None:
        record_id = self.prepare("records_commit", ExecutionReceipt())
        current = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key="records_commit",
        )
        assert current is not None
        acquired = self.store.retry_reconciled_mutation(
            reservation=current, lease_owner="receipt-owner", preserve_effect_checkpoint=True
        )
        assert acquired.record is not None
        owner = acquired.record
        record = self.store.read_deployment_record(record_id)
        recorder = ExecutionRecorder(
            self.store, record_id, ExecutionReceipt(), expected_record=record
        )
        with self.store.provider_evidence_guard(lambda: owner):
            index = recorder.begin(
                "product_executor", "verify", self.target.resolved_target.target_id, {}
            )
            merge = self.store.advance_generic_web_execution_receipt

            def correct_then_merge(**kwargs: Any) -> None:
                with closing(
                    PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
                    )
                ) as writer:
                    saved = writer.read_deployment_record(record_id)
                    correction = saved.model_copy(
                        update={
                            "post_deploy_update": saved.post_deploy_update.model_copy(
                                update={"status": "fail", "detail": "Preserve supported correction"}
                            ),
                            "integration_key_readback": IntegrationKeyReadbackEvidence(
                                status="pass"
                            ),
                        }
                    )
                    apply_deployment_evidence(record_store=writer, deployment_record=correction)
                merge(**kwargs)

            with patch.object(
                self.store, "advance_generic_web_execution_receipt", side_effect=correct_then_merge
            ):
                recorder.update(index, acknowledgement="completed", terminal="executor_completed")
        saved = self.store.read_deployment_record(record_id)
        self.assertEqual(saved.post_deploy_update.status, "fail")
        self.assertEqual(saved.post_deploy_update.detail, "Preserve supported correction")
        self.assertIsNotNone(saved.integration_key_readback)
        self.assertEqual(
            ExecutionReceipt.model_validate_json(
                saved.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY]
            )
            .entries[index]
            .terminal,
            "executor_completed",
        )
        self.assertNotEqual(saved, recorder.expected_record)

    def normal_adapter(self) -> GenericWebDeployProviderMutationAdapter:
        provider = _FakeGenericWebDeployProvider(observation=self.provider.observation)
        provider.provider_id = "dokploy"
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            deploy_request=GenericWebDeployEnvelope.model_validate(self.request),
            profile=self.profile,
            lane=self.profile.lanes[0],
            trace_id="normal-publication",
            deploy_provider=provider,
            recorded_artifact=True,
        )
        adapter._resolved_deploy_target = self.target
        return adapter

    def run_normal(
        self, adapter: GenericWebDeployProviderMutationAdapter, key: str
    ) -> DurableProviderOperationResult:
        return run_durable_provider_operation(
            store=self.store,
            scope="normal-fixture",
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key=key,
            request_fingerprint=idempotency_request_fingerprint(
                route_path="/v1/drivers/generic-web/deploy",
                payload=self.request,
            ),
            lease_owner="normal-worker",
            response_trace_id="normal-result",
            adapter=adapter,
        )

    def normal_record(self, outcome: DurableProviderOperationResult) -> DeploymentRecord:
        assert outcome.record is not None
        operation = build_provider_operation_key(
            scope=outcome.record.scope,
            route_path=outcome.record.route_path,
            idempotency_key=outcome.record.idempotency_key,
            request_fingerprint=outcome.record.request_fingerprint,
            reconciliation_key=outcome.record.reconciliation_key,
        )
        return self.store.read_deployment_record(
            self.normal_adapter()._deployment_record_id(operation)
        )

    def test_answered_executor_result_lost_before_publication_settles_without_replay(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        executions: list[str] = []
        for verdict in ("fail", "pass", "exception"):
            with self.subTest(verdict=verdict):

                def executor(*_args: object) -> PostDeployUpdateEvidence:
                    executions.append(verdict)
                    if verdict == "exception":
                        raise click.ClickException("Answered product assertion failed.")
                    return PostDeployUpdateEvidence(attempted=True, status=cast(Any, verdict))

                publish = self.store.publish_generic_web_testing_result

                def interrupt_final(**kwargs: Any) -> None:
                    if (
                        kwargs["deployment"].deploy.status == "pass"
                        and kwargs["deployment"].post_deploy_update.status != "pending"
                    ):
                        raise ValueError("Final publication interrupted.")
                    publish(**kwargs)

                key = f"unpublished-{verdict}"
                with (
                    patch(
                        "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile",
                        return_value=executor,
                    ),
                    patch.object(
                        self.store,
                        "publish_generic_web_testing_result",
                        side_effect=interrupt_final,
                    ),
                ):
                    outcome = self.run_normal(self.normal_adapter(), key)
                self.assertEqual(outcome.status, "reconcile_required")
                pending = self.normal_record(outcome)
                inventory = self.store.read_environment_inventory(
                    context_name=pending.context, instance_name="testing"
                )
                self.assertEqual(inventory.deployment_record_id, pending.record_id)
                self.assertEqual(pending.post_deploy_update.status, "pending")
                with self.assertRaises(GenericWebSourceUnqualified):
                    qualified_generic_web_source_inventory(
                        store=self.store, profile=self.profile, lane=self.profile.lanes[0]
                    )
                status, result = self.complete_original(key, action="settle_testing_failure")
                self.assertEqual((status, result["reservation_state"]), (202, "completed"))
                self.assertEqual(
                    self.store.read_deployment_record(pending.record_id).post_deploy_update.status,
                    "fail",
                )
                self.assertEqual(
                    self.store.read_environment_inventory(
                        context_name=pending.context, instance_name="testing"
                    ),
                    inventory,
                )
                replay = self.run_normal(self.normal_adapter(), key)
                self.assertEqual(replay.status, "replayed")
        self.assertEqual(executions, ["fail", "pass", "exception"])
        successor = self.run_normal(self.normal_adapter(), "ordinary-successor")
        self.assertEqual(successor.status, "completed")
        qualified = qualified_generic_web_source_inventory(
            store=self.store, profile=self.profile, lane=self.profile.lanes[0]
        )
        self.assertEqual(qualified.deployment_record_id, self.normal_record(successor).record_id)

    def test_durable_normal_pass_recovery_preserves_corrected_inventory(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        publish = self.store.publish_generic_web_testing_result
        executions: list[str] = []

        def executor(*_args: object) -> PostDeployUpdateEvidence:
            executions.append("executed")
            return PostDeployUpdateEvidence(attempted=True, status="pass")

        def lose_completion(**kwargs: Any) -> None:
            publish(**kwargs)
            if kwargs["deployment"].post_deploy_update.status == "pass":
                raise ValueError("Publication committed before worker stopped.")

        with (
            patch(
                "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile",
                return_value=executor,
            ),
            patch.object(
                self.store, "publish_generic_web_testing_result", side_effect=lose_completion
            ),
        ):
            outcome = self.run_normal(self.normal_adapter(), "durable-pass")
        self.assertEqual(outcome.status, "reconcile_required")
        saved = self.normal_record(outcome)
        inventory = self.store.read_environment_inventory(
            context_name=saved.context, instance_name="testing"
        ).model_copy(update={"bootstrap_record_id": "supported-correction"})
        self.store.write_environment_inventory(inventory)
        status, result = self.complete_original("durable-pass")
        self.assertEqual((status, result["reservation_state"]), (202, "completed"))
        self.assertEqual(self.store.read_deployment_record(saved.record_id), saved)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=saved.context, instance_name="testing"
            ),
            inventory,
        )
        self.assertEqual(self.run_normal(self.normal_adapter(), "durable-pass").status, "replayed")
        self.assertEqual(executions, ["executed"])

    def test_exact_original_image_failure_disqualifies_saved_normal_pass(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        publish = self.store.publish_generic_web_testing_result

        def lose_completion(**kwargs: Any) -> None:
            publish(**kwargs)
            if kwargs["deployment"].deploy.status == "pass":
                raise ValueError("Final result committed before worker stopped.")

        for newer in (False, True):
            with self.subTest(newer=newer):
                key = f"contradicted-image-{newer}"
                self.provider.observation = self.provider.observation.model_copy(
                    update={"deployment_status": "success"}
                )
                with patch.object(
                    self.store, "publish_generic_web_testing_result", side_effect=lose_completion
                ):
                    outcome = self.run_normal(self.normal_adapter(), key)
                self.assertEqual(outcome.status, "reconcile_required")
                saved = self.normal_record(outcome)
                self.assertEqual(
                    qualified_generic_web_source_inventory(
                        store=self.store, profile=self.profile, lane=self.profile.lanes[0]
                    ).deployment_record_id,
                    saved.record_id,
                )
                if newer:
                    apply_deployment_evidence(
                        record_store=self.store,
                        deployment_record=saved.model_copy(
                            update={
                                "record_id": "independent-newer-failed-result",
                                "post_deploy_update": PostDeployUpdateEvidence(
                                    attempted=True,
                                    status="fail",
                                    detail="Independent later result.",
                                ),
                            }
                        ),
                    )
                inventory = self.store.read_environment_inventory(
                    context_name=saved.context, instance_name="testing"
                )
                self.provider.observation = self.provider.observation.model_copy(
                    update={"deployment_status": "error"}
                )
                if not newer:
                    exact = self.provider.observation
                    self.provider.observation = exact.model_copy(
                        update={"deployment_id": "foreign-image"}
                    )
                    status, _rejected = self.complete_original(key, action="settle_testing_failure")
                    self.assertEqual(status, 409)
                    self.assertEqual(self.store.read_deployment_record(saved.record_id), saved)
                    self.assertEqual(
                        self.store.read_environment_inventory(
                            context_name=saved.context, instance_name="testing"
                        ),
                        inventory,
                    )
                    self.provider.observation = exact
                status, result = self.complete_original(key, action="settle_testing_failure")
                self.assertEqual((status, result["reservation_state"]), (202, "completed"))
                settled = self.store.read_deployment_record(saved.record_id)
                self.assertEqual(settled.deploy.status, "fail")
                self.assertEqual(settled.post_deploy_update.status, "fail")
                self.assertEqual(settled.destination_health, saved.destination_health)
                self.assertEqual(
                    self.store.read_environment_inventory(
                        context_name=saved.context, instance_name="testing"
                    ),
                    inventory,
                )
                with self.assertRaises(GenericWebSourceUnqualified):
                    qualified_generic_web_source_inventory(
                        store=self.store, profile=self.profile, lane=self.profile.lanes[0]
                    )
                self.assertEqual(self.run_normal(self.normal_adapter(), key).status, "replayed")

    def test_progress_refuses_supported_correction_before_any_executor_dispatch(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        publish = self.store.publish_generic_web_testing_result
        corrected: list[DeploymentRecord] = []

        def correct_before_progress(**kwargs: Any) -> None:
            if kwargs["deployment"].post_deploy_update.status == "pending":
                with closing(
                    PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
                    )
                ) as writer:
                    saved = writer.read_deployment_record(kwargs["deployment"].record_id)
                    correction = saved.model_copy(
                        update={
                            "integration_key_readback": IntegrationKeyReadbackEvidence(
                                status="pass"
                            )
                        }
                    )
                    apply_deployment_evidence(record_store=writer, deployment_record=correction)
                    corrected.append(correction)
            publish(**kwargs)

        with (
            patch(
                "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile"
            ) as selector,
            patch.object(
                self.store,
                "publish_generic_web_testing_result",
                side_effect=correct_before_progress,
            ),
        ):
            executor = selector.return_value
            executor.__name__ = "unused_executor"
            outcome = self.run_normal(self.normal_adapter(), "progress-correction")
            executor.assert_not_called()
        self.assertEqual(outcome.status, "reconcile_required")
        self.assertEqual(self.normal_record(outcome), corrected[0])
        inventory = self.store.read_environment_inventory(
            context_name=corrected[0].context, instance_name="testing"
        )
        status, result = self.complete_original(
            "progress-correction", action="settle_testing_failure"
        )
        self.assertEqual((status, result["reservation_state"]), (202, "completed"))
        settled = self.store.read_deployment_record(corrected[0].record_id)
        self.assertEqual(settled.integration_key_readback, corrected[0].integration_key_readback)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=settled.context, instance_name="testing"
            ),
            inventory,
        )

    def test_normal_final_result_preserves_same_original_inventory_correction(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                corrected: list[object] = []

                def executor(*_args: object) -> PostDeployUpdateEvidence:
                    inventory = self.store.read_environment_inventory(
                        context_name=self.profile.lanes[0].context, instance_name="testing"
                    ).model_copy(update={"bootstrap_record_id": f"corrected-{verdict}"})
                    self.store.write_environment_inventory(inventory)
                    corrected.append(inventory)
                    return PostDeployUpdateEvidence(attempted=True, status=cast(Any, verdict))

                with patch(
                    "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile",
                    return_value=executor,
                ):
                    outcome = self.run_normal(self.normal_adapter(), f"same-original-{verdict}")
                self.assertEqual(outcome.status, "completed")
                saved = self.normal_record(outcome)
                self.assertEqual(saved.post_deploy_update.status, verdict)
                self.assertEqual(
                    self.store.read_environment_inventory(
                        context_name=saved.context, instance_name="testing"
                    ),
                    corrected[0],
                )

    def test_saved_aggregate_failure_keeps_original_health_evidence(self) -> None:
        for health_status, post_status in (("pending", "fail"), ("fail", "skipped")):
            with self.subTest(health=health_status):
                key = f"saved-health-{health_status}"
                record_id = self.prepare(
                    "records_commit", ExecutionReceipt(), qualified=True, idempotency_key=key
                )
                saved = self.store.read_deployment_record(record_id)
                failed = saved.model_copy(
                    update={
                        "post_deploy_update": saved.post_deploy_update.model_copy(
                            update={
                                "attempted": post_status != "skipped",
                                "status": post_status,
                            }
                        ),
                        "destination_health": saved.destination_health.model_copy(
                            update={"status": health_status}
                        ),
                    }
                )
                self.store.write_deployment_record(failed)
                status, result = self.complete_original(key)
                self.assertEqual((status, result["reservation_state"]), (202, "completed"))
                self.assertEqual(self.store.read_deployment_record(record_id), failed)
                completed = self.store.read_idempotency_record(
                    scope=self.reservation.scope,
                    route_path=self.reservation.route_path,
                    idempotency_key=key,
                )
                assert completed is not None
                self.assertEqual(completed.response_status_code, 502)
                self.assertEqual(completed.response_payload["result"]["deploy_status"], "fail")

    def test_legacy_missing_record_with_required_executor_is_not_image_only(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        reservation = _write_generic_web_recovery_reservation(
            self.store,
            _generic_web_recovery_reservation(
                original_deploy=self.request,
                idempotency_key="missing-executor-proof",
                provider_effect_phase="deploy_trigger",
            ),
        )

        def executor(*_args: object) -> PostDeployUpdateEvidence:
            raise AssertionError("Observation must not run the executor.")

        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            ),
            patch(
                "control_plane.generic_web_deploy_provider_adapter.generic_web_post_deploy_executor_for_profile",
                return_value=executor,
            ),
            patch(
                "control_plane.workflows.generic_web_post_deploy.generic_web_post_deploy_executor_for_profile",
                return_value=executor,
            ),
        ):
            status, result = _invoke_recovery(
                self.app,
                original_deploy=self.request,
                idempotency_key=reservation.idempotency_key,
                reason="Inspect required executor with missing original evidence.",
            )
        self.assertEqual((status, result["proposed_action"]), (200, "hold_unknown"))

    def test_new_image_only_interruption_settles_then_normal_successor_qualifies(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        with patch(
            "control_plane.workflows.generic_web_deploy.integration_key_readback",
            side_effect=ProviderMutationUnknownError("Worker interrupted after image observation."),
        ):
            outcome = self.run_normal(self.normal_adapter(), "image-only-interruption")
        self.assertEqual(outcome.status, "reconcile_required")
        original = self.normal_record(outcome)
        receipt = ExecutionReceipt.model_validate_json(
            original.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY]
        )
        self.assertFalse(any(entry.kind == "product_executor" for entry in receipt.entries))
        status, result = self.complete_original(
            "image-only-interruption", action="settle_testing_failure"
        )
        self.assertEqual((status, result["reservation_state"]), (202, "completed"))
        successor = self.run_normal(self.normal_adapter(), "image-only-successor")
        self.assertEqual(successor.status, "completed")
        qualified = qualified_generic_web_source_inventory(
            store=self.store, profile=self.profile, lane=self.profile.lanes[0]
        )
        self.assertEqual(qualified.post_deploy_update.status, "skipped")

    def test_normal_failed_qualification_updates_only_unchanged_inventory_baseline(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        for independent in (False, True):
            with self.subTest(independent=independent):
                prior = DeploymentRecord(
                    record_id=f"baseline-{independent}",
                    context=self.target.ship_request.context,
                    instance="testing",
                    source_git_ref=self.target.ship_request.source_git_ref,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=self.target.ship_request.artifact_id
                    ),
                    deploy=DeploymentEvidence(
                        target_name="testing",
                        target_type="application",
                        deploy_mode="application",
                        status="pass",
                        deployment_id="previous-image",
                    ),
                )
                apply_deployment_evidence(record_store=self.store, deployment_record=prior)
                newer_inventory = None

                def answered_failure(*_args: object) -> PostDeployUpdateEvidence:
                    nonlocal newer_inventory
                    if independent:
                        newer = prior.model_copy(update={"record_id": "independent-newer"})
                        with closing(
                            PostgresRecordStore(
                                database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
                            )
                        ) as writer:
                            apply_deployment_evidence(record_store=writer, deployment_record=newer)
                            newer_inventory = writer.read_environment_inventory(
                                context_name=prior.context, instance_name="testing"
                            )
                    return PostDeployUpdateEvidence(
                        attempted=True, status="fail", detail="Answered product assertion failed."
                    )

                with patch(
                    "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile",
                    return_value=answered_failure,
                ):
                    outcome = self.run_normal(self.normal_adapter(), f"failed-{independent}")
                self.assertEqual(outcome.status, "completed")
                assert outcome.record is not None
                record_id = outcome.record.response_payload["records"]["deployment_record_id"]
                saved = self.store.read_deployment_record(record_id)
                self.assertEqual(saved.deploy.status, "pass")
                self.assertEqual(saved.post_deploy_update.status, "fail")
                current = self.store.read_environment_inventory(
                    context_name=prior.context, instance_name="testing"
                )
                if independent:
                    self.assertEqual(current, newer_inventory)
                else:
                    self.assertEqual(current.deployment_record_id, record_id)
                    self.assertEqual(current.post_deploy_update.status, "fail")

    def assert_progress_disqualifies_before_refusing_changed_authority(
        self, *, prior_inventory: bool, profile_drift: bool
    ) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        lane = self.profile.lanes[0]
        prior = None
        if prior_inventory:
            baseline = self.run_normal(self.normal_adapter(), "qualified-prior")
            self.assertEqual(baseline.status, "completed")
            prior = qualified_generic_web_source_inventory(
                store=self.store, profile=self.profile, lane=lane
            )
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context=lane.context,
            instance="testing",
            env={"SITE_MODE": "reviewed"},
            updated_at="2026-08-15T12:00:00Z",
        )

        def configure(value: str) -> None:
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
            if not profile_drift:
                configure("reviewed")
                self.target = self.target.model_copy(
                    update={
                        "testing_runtime_digest": testing_input_authority(
                            self.store, self.profile, lane
                        )
                    }
                )
            publisher = self.store.publish_generic_web_testing_result

            def changed_then_publish(**kwargs: Any) -> None:
                if kwargs["deployment"].post_deploy_update.status == "pending":
                    if profile_drift:
                        with closing(
                            PostgresRecordStore(
                                database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
                            )
                        ) as writer:
                            writer.write_product_profile_record(
                                self.profile.model_copy(
                                    update={"display_name": "Changed during image execution"}
                                )
                            )
                    else:
                        configure("changed-after-callback")
                publisher(**kwargs)

            with (
                patch.object(
                    self.store,
                    "publish_generic_web_testing_result",
                    side_effect=changed_then_publish,
                ),
                patch(
                    "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile"
                ) as selector,
            ):
                outcome = self.run_normal(self.normal_adapter(), "normal-authority-race")
                selector.return_value.assert_not_called()
        self.assertEqual(outcome.status, "reconcile_required")
        saved = self.normal_record(outcome)
        current = self.store.read_environment_inventory(
            context_name=lane.context, instance_name="testing"
        )
        self.assertEqual(saved.deploy.status, "pass")
        self.assertEqual(saved.post_deploy_update.status, "pending")
        self.assertEqual(current.deployment_record_id, saved.record_id)
        if prior is not None:
            self.assertNotEqual(current.deployment_record_id, prior.deployment_record_id)
        with self.assertRaises(GenericWebSourceUnqualified):
            qualified_generic_web_source_inventory(
                store=self.store, profile=self.profile, lane=lane
            )

    def test_normal_publication_checks_actual_config_change_after_callback(self) -> None:
        self.assert_progress_disqualifies_before_refusing_changed_authority(
            prior_inventory=False, profile_drift=False
        )

    def test_progress_replaces_old_qualification_before_input_drift_refusal(self) -> None:
        self.assert_progress_disqualifies_before_refusing_changed_authority(
            prior_inventory=True, profile_drift=False
        )

    def test_progress_records_pending_before_profile_drift_refusal(self) -> None:
        self.assert_progress_disqualifies_before_refusing_changed_authority(
            prior_inventory=False, profile_drift=True
        )

    def test_progress_replaces_old_qualification_before_profile_drift_refusal(self) -> None:
        self.assert_progress_disqualifies_before_refusing_changed_authority(
            prior_inventory=True, profile_drift=True
        )

    def test_progress_refuses_repointed_target_without_replacing_prior_inventory(self) -> None:
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        lane = self.profile.lanes[0]
        self.assertEqual(self.run_normal(self.normal_adapter(), "prior-target").status, "completed")
        prior = qualified_generic_web_source_inventory(
            store=self.store, profile=self.profile, lane=lane
        )
        publisher = self.store.publish_generic_web_testing_result

        def repoint_then_publish(**kwargs: Any) -> None:
            if kwargs["deployment"].post_deploy_update.status == "pending":
                with closing(
                    PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
                    )
                ) as writer:
                    binding = writer.read_dokploy_target_id_record(
                        context_name=lane.context, instance_name="testing"
                    )
                    target = writer.read_provider_target_record(
                        context_name=lane.context, instance_name="testing"
                    )
                    writer.write_dokploy_target_id_record(
                        binding.model_copy(update={"target_id": "independent-target"})
                    )
                    writer.write_provider_target_record(
                        target.model_copy(update={"target_id": "independent-target"})
                    )
            publisher(**kwargs)

        with (
            patch.object(
                self.store, "publish_generic_web_testing_result", side_effect=repoint_then_publish
            ),
            patch(
                "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile"
            ) as selector,
        ):
            outcome = self.run_normal(self.normal_adapter(), "target-repoint-before-progress")
            selector.return_value.assert_not_called()
        self.assertEqual(outcome.status, "reconcile_required")
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=lane.context, instance_name="testing"
            ),
            prior,
        )
        self.assertNotEqual(self.normal_record(outcome).deploy.status, "pass")

    def test_pre_image_settlement_records_enable_and_delete_and_fences_lost_enable(self) -> None:
        class Provider(_RecoveryObservationProvider, DokployGenericWebDeployProvider):
            pass

        extensions = _extensions()
        lane = self.profile.lanes[0].model_copy(update={"generic_web_extensions": extensions})
        self.profile = self.profile.model_copy(update={"lanes": (lane,)})
        self.store.write_product_profile_record(self.profile)
        self.target = self.target.model_copy(
            update={"testing_runtime_digest": evidence_digest({"captured": "inputs"})}
        )
        for lost in (False, True):
            with self.subTest(lost=lost):
                key = f"pre-image-{lost}"
                original = schedule_payload(
                    extensions.schedules[0], self.target.resolved_target.target_id, enabled=True
                )
                snapshot = ApplicationLifecycleSnapshot(
                    product=self.profile.product,
                    context=lane.context,
                    instance="testing",
                    provider_id="dokploy",
                    target_id=self.target.resolved_target.target_id,
                    artifact_id=self.target.ship_request.artifact_id,
                    source_git_ref=self.target.ship_request.source_git_ref,
                    extensions=extensions,
                    originals={extensions.schedules[0].name: original, "created": None},
                    managed_names=(extensions.schedules[0].name, "created"),
                )
                with patch(
                    "tests.test_generic_web_deploy_recovery._generic_web_recovery_target",
                    return_value=self.target,
                ):
                    record_id = self.prepare(
                        "runtime_environment_update",
                        ExecutionReceipt(),
                        snapshot=snapshot,
                        idempotency_key=key,
                    )
                # The next fixture uses a distinct original key without changing its target.
                reserved = self.reservation
                record = self.store.read_deployment_record(record_id)
                self.store.write_deployment_record(
                    record.model_copy(
                        update={"deploy": record.deploy.model_copy(update={"status": "fail"})}
                    )
                )
                schedules = {
                    extensions.schedules[0].name: {**original, "enabled": False},
                    "created": {**original, "name": "created", "enabled": False},
                }
                writes: list[str] = []

                def upsert(**kwargs: Any) -> api.JsonObject:
                    payload = cast(api.JsonObject, kwargs["schedule_payload"])
                    current = self.store.read_deployment_record(record_id)
                    self.assertFalse(
                        ExecutionReceipt.model_validate_json(
                            current.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY]
                        )
                        .entries[-1]
                        .terminal
                    )
                    schedules[str(payload["name"])] = payload
                    writes.append("enable")
                    if lost:
                        raise ProviderMutationUnknownError("Original enable reply lost.")
                    return payload

                def delete(**kwargs: Any) -> None:
                    schedules.pop(kwargs["schedule_payload"]["name"])
                    writes.append("delete")

                class Adapter:
                    def target_key(inner) -> str:
                        return reserved.provider_target_key

                    def reconciliation_key(inner) -> str:
                        return reserved.reconciliation_key

                    def observe(inner, *_args: object) -> ProviderObservation:
                        return ProviderObservation(
                            outcome="absent", retry_safe=True, preserve_effect_checkpoint=True
                        )

                    def apply(inner, operation: str, lease: ProviderOperationLease) -> Any:
                        with self.store.provider_evidence_guard(
                            cast(ProviderEvidenceLease, lease).evidence_reservation
                        ):
                            return settle_testing_pre_image_attempt(
                                record_store=self.store,
                                profile=self.profile,
                                lane=lane,
                                target=self.target,
                                operation_key=operation,
                                phase="runtime_environment_update",
                                no_effect_checkpoint=False,
                                superseded=True,
                                provider=Provider(
                                    GenericWebProviderDeploymentObservation(outcome="absent")
                                ),
                                control_plane_root=self.root,
                                lease=lease,
                                check_execution=lambda: None,
                                trace_id="pre-image-settlement",
                            )

                with (
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
                    patch.object(api, "delete_dokploy_application_schedule", side_effect=delete),
                ):
                    outcome = run_durable_provider_operation(
                        store=self.store,
                        scope=reserved.scope,
                        route_path=reserved.route_path,
                        idempotency_key=reserved.idempotency_key,
                        request_fingerprint=reserved.request_fingerprint,
                        lease_owner="settlement-worker",
                        response_trace_id="settlement",
                        adapter=Adapter(),
                    )
                saved = self.store.read_deployment_record(record_id)
                receipt = ExecutionReceipt.model_validate_json(
                    saved.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY]
                )
                if lost:
                    self.assertEqual(outcome.status, "reconcile_required")
                    self.assertEqual(writes, ["enable"])
                    self.assertFalse(receipt.entries[-1].terminal)
                else:
                    self.assertEqual(outcome.status, "completed")
                    self.assertEqual(writes, ["enable", "delete"])
                    self.assertEqual(
                        [entry.kind for entry in receipt.entries],
                        ["schedule_write", "schedule_delete"],
                    )
                    self.assertTrue(all(entry.terminal for entry in receipt.entries))

    def prepare(
        self,
        phase: str,
        receipt: ExecutionReceipt | None,
        *,
        qualified: bool = False,
        snapshot: ApplicationLifecycleSnapshot | None = None,
        idempotency_key: str = "",
    ) -> str:
        reservation = _write_generic_web_recovery_reservation(
            self.store,
            _generic_web_recovery_reservation(
                original_deploy=self.request,
                idempotency_key=idempotency_key or phase,
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
            original = self.store.publish_generic_web_testing_result

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
                    "publish_generic_web_testing_result",
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
        original = self.store.publish_generic_web_testing_result

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
                self.store, "publish_generic_web_testing_result", side_effect=change_then_publish
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
        original = self.store.publish_generic_web_testing_result

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
                self.store, "publish_generic_web_testing_result", side_effect=repoint_then_publish
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
