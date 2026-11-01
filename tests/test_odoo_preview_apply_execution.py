import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from typing import Callable, cast
import click
from unittest.mock import Mock, patch

from control_plane import secrets as control_plane_secrets
from control_plane import odoo_preview_apply_http as preview_http
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
from control_plane.contracts.artifact_identity import ArtifactBuildFlags

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
)
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.odoo_preview_apply_execution import run_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    _odoo_preview_service_environment,
    build_odoo_preview_runtime_identity,
    issue_odoo_preview_apply_plan,
    preview_uses_upstream_restore,
    preview_has_completed_restore,
    validate_odoo_preview_issued_plan,
)
from control_plane.provider_operations import DurableProviderOperationResult
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewApplyInputsResult,
)
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimePlan
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import (
    _sqlite_database_url,
    _write_odoo_preview_template_runtime_environment,
)

_HEAD_SHA = "c" * 40
_IMAGE_DIGEST = "a" * 64
_PLAN_ID = "odoo-preview-plan-worker"
_WORKER_SCOPE = "worker|odoo-preview-apply|test"


def _refresh_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "odoo-tenant-cm",
        "apply": {
            "dry_run_plan": {
                "status": "ready",
                "operation": "refresh",
                "product": "odoo-tenant-cm",
                "repository": "cbusillo/odoo-tenant-cm",
                "preview_slug": "pr-42",
                "preview_url": "https://pr-42.cm-preview.example.test",
                "domain_host": "pr-42.cm-preview.example.test",
                "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                "compose_name": "cm-odoo-preview-pr-42",
                "environment_id": "env-cm-preview",
                "template_compose_id": "compose-cm-testing",
                "summary": "ready isolated Odoo preview apply",
            },
            "image_reference": f"ghcr.io/cbusillo/odoo-tenant-cm@sha256:{_IMAGE_DIGEST}",
            "manifest": {
                "artifact_id": "artifact-cm-preview",
                "source_commit": _HEAD_SHA,
                "enterprise_base_digest": "sha256:enterprise",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                    "digest": f"sha256:{_IMAGE_DIGEST}",
                },
            },
            "wait_for_deploy": False,
            "smoke_check": False,
        },
    }


def _issued_plan(apply_request: OdooPreviewApplyEnvelope) -> OdooPreviewApplyInputsResult:
    dry_run_plan = apply_request.apply.dry_run_plan
    planned = OdooPreviewApplyInputsResult(
        status="ready",
        product=apply_request.product,
        context="cm",
        template_instance="testing",
        operation=dry_run_plan.operation,
        preview_slug=dry_run_plan.preview_slug,
        preview_url=dry_run_plan.preview_url,
        repository=dry_run_plan.repository,
        plan_request=OdooPreviewApplyInputsRequest(
            product=apply_request.product,
            operation=dry_run_plan.operation,
            pr_number=42,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            image_reference=apply_request.apply.image_reference,
            manifest=apply_request.apply.manifest,
            source_git_ref=_HEAD_SHA,
            source="test-issued-plan",
        ),
        runtime_plan=OdooPreviewRuntimePlan(
            status="ready",
            operation=dry_run_plan.operation,
            product=apply_request.product,
            repository=dry_run_plan.repository,
            pr_number=42,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            strategy="isolated_dokploy_compose",
            summary="ready test Odoo preview runtime plan",
        ),
        dry_run_plan=dry_run_plan,
        source="test-issued-plan",
    )
    return issue_odoo_preview_apply_plan(result=planned, plan_id=_PLAN_ID)


def _validated_refresh_request() -> tuple[OdooPreviewApplyEnvelope, OdooPreviewApplyInputsResult]:
    request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
    issued = _issued_plan(request)
    return (
        validate_odoo_preview_issued_plan(plan_id=_PLAN_ID, issued_plan=issued, request=request),
        issued,
    )


class RunOdooPreviewApplyOperationTests(unittest.TestCase):
    def test_failed_native_diagnostics_survive_without_repeating_provider_effects(self) -> None:
        from control_plane.copy_readback_diagnostics import CopyFailureStage, ReadbackPredicate

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request, issued = _validated_refresh_request()
            evidence = {
                "schedule_id": "fixture-schedule",
                "schedule_deployment_key": "fixture-job",
                "schedule_deployment_id": "fixture-deployment",
                "copy_failure_stage": CopyFailureStage.MODULE_UPDATE,
                "copy_failure_exit_status": "1",
                "copy_failure_evidence_id": "e" * 32,
                "readback_failed_predicate": ReadbackPredicate.SETTINGS,
                "readback_failure_kind": "unavailable",
            }

            def failed_apply(**kwargs: object) -> dict[str, object]:
                checkpoint = cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])
                checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                return {
                    "status": "fail",
                    "provider_effect_attempted": True,
                    "error_message": "private failure text",
                    "module_install_update_evidence": {
                        **evidence,
                        "unrecognized": "private result",
                    },
                }

            execute = Mock(side_effect=failed_apply)
            observe = Mock(return_value=("unknown", None, False))
            with self.assertLogs(level="WARNING") as logs:
                results = [
                    run_odoo_preview_apply_operation(
                        store=store,
                        control_plane_root=root,
                        record_store=store,
                        profile=profile,
                        apply_request=request,
                        issued_plan=issued,
                        reservation_scope=_WORKER_SCOPE,
                        idempotency_key=_PLAN_ID,
                        request_fingerprint="failed-native-diagnostics",
                        trace_id="fixture-trace",
                        execute_apply=execute,
                        observe_apply=observe,
                    )
                    for _ in range(2)
                ]
            execute.assert_called_once()
            self.assertTrue(all(result.status == "reconcile_required" for result in results))
            self.assertEqual(store.list_preview_records(), ())
            record = store.read_idempotency_record(
                scope=_WORKER_SCOPE,
                route_path=ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=_PLAN_ID,
            )
            assert record is not None
            self.assertEqual(record.state, "reconcile_required")
            self.assertEqual(record.provider_effect_phase, ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
            diagnostics = [
                json.loads(entry.getMessage())
                for entry in logs.records
                if entry.name == "control_plane.odoo_preview_apply_execution"
            ]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["operation_id"], record.record_id)
            self.assertEqual(diagnostics[0]["trace_id"], "fixture-trace")
            self.assertEqual(diagnostics[0]["evidence"], evidence)
            self.assertNotIn("private", " ".join(logs.output))

    def test_failed_native_diagnostics_reject_unvalidated_values(self) -> None:
        from control_plane.odoo_preview_apply_execution import _log_failed_preview_evidence

        for evidence in (
            {
                "schedule_id": "private\nforged log",
                "schedule_deployment_key": "private" * 100,
                "schedule_deployment_id": {"private": "payload"},
                "copy_failure_stage": "private",
                "copy_failure_exit_status": "private",
                "copy_failure_evidence_id": "private",
                "readback_failed_predicate": "private",
                "readback_failure_kind": "private",
            },
            "private",
            None,
        ):
            with self.subTest(evidence=evidence), self.assertLogs(level="WARNING") as logs:
                _log_failed_preview_evidence(
                    {"module_install_update_evidence": evidence},
                    operation_id="fixture-operation",
                    trace_id="fixture-trace",
                )
            self.assertEqual(json.loads(logs.records[0].getMessage())["evidence"], {})
            self.assertNotIn("private", " ".join(logs.output))

    def test_preservation_reference_survives_plan_validation_and_retained_intent(self) -> None:
        original = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        original.apply.dry_run_plan.operation = "destroy"
        reference = build_launchplane_mutation_reservation_id(
            scope="original-preview", route_path=ODOO_PREVIEW_APPLY_ROUTE, idempotency_key="donor"
        )
        request = OdooPreviewApplyEnvelope.model_validate(
            {**original.model_dump(mode="json"), "preserve_original_reservation_id": reference}
        )
        issued = _issued_plan(request)
        assert issued.plan_provenance is not None
        validated = validate_odoo_preview_issued_plan(
            plan_id=issued.plan_provenance.plan_id, issued_plan=issued, request=request
        )
        from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation

        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=Path("."),
            record_store=Mock(),
            profile=LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload()),
            apply_request=validated,
            issued_plan=issued,
            reservation_scope=_WORKER_SCOPE,
            idempotency_key=issued.plan_provenance.plan_id,
            request_fingerprint="preserving-destroy",
            trace_id="preserving-destroy",
        )
        restored = OdooPreviewApplyEnvelope.model_validate(
            operation.adapter.original_intent()["apply_request"]
        )
        self.assertEqual(restored.preserve_original_reservation_id, reference)

    def test_preservation_reference_cannot_mix_with_unrecorded_recovery(self) -> None:
        reference = build_launchplane_mutation_reservation_id(
            scope="original-preview", route_path=ODOO_PREVIEW_APPLY_ROUTE, idempotency_key="donor"
        )
        with self.assertRaises(ValueError):
            OdooPreviewApplyEnvelope.model_validate(
                {
                    **_refresh_payload(),
                    "preserve_original_reservation_id": reference,
                    "unrecorded_original_reservation_id": reference,
                }
            )

    def test_qualified_snapshot_preview_refresh_needs_no_first_snapshot(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        profile.preview.clean_testing_snapshot = True
        envelope = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(envelope)
        store = Mock(spec=["read_clean_testing_snapshot", "read_odoo_instance_override_record"])
        store.read_odoo_instance_override_record.side_effect = FileNotFoundError
        store.read_clean_testing_snapshot.side_effect = AssertionError(
            "A retained preview needs no source lookup"
        )
        environment = preview_http._OdooPreviewServiceEnvironment(values={})
        with (
            patch.object(preview_http, "build_odoo_preview_apply_inputs", return_value=issued),
            patch.object(preview_http, "preview_has_completed_restore", return_value=True),
            patch.object(
                preview_http, "_odoo_preview_service_environment", return_value=environment
            ),
        ):
            plan = preview_http._build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=store,
                profile=profile,
                request=issued.plan_request,
                database_url=None,
            )
        self.assertIsNone(plan.dry_run_plan.clean_snapshot)

    def test_apply_reproves_retained_copy_even_when_snapshot_descriptor_is_absent(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        profile.preview.clean_testing_snapshot = True
        envelope = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(envelope)
        with (
            patch.object(preview_http, "refresh_odoo_preview_issued_plan", return_value=envelope),
            patch.object(
                preview_http,
                "_odoo_preview_service_environment",
                return_value=preview_http._OdooPreviewServiceEnvironment(values={}),
            ),
            patch("control_plane.lane_movement.require_forward_preview_build"),
            patch.object(
                preview_http, "preview_has_completed_restore", return_value=True
            ) as retained,
            patch.object(
                preview_http,
                "execute_odoo_preview_dokploy_apply",
                return_value=Mock(model_dump=Mock(return_value={"status": "pass"})),
            ) as execute,
        ):
            preview_http.execute_odoo_preview_apply_result(
                control_plane_root_path=Path("."),
                record_store=Mock(),
                profile=profile,
                request=envelope,
                issued_plan=issued,
                database_url=None,
                deployment_record_id="fixture-retained-operation",
            )
        retained.assert_called_once()
        self.assertTrue(execute.call_args.kwargs["preserve_existing_data"])
        self.assertFalse(execute.call_args.kwargs["restore_upstream"])
        self.assertFalse(execute.call_args.kwargs["restore_clean_snapshot"])

    def test_upstream_import_and_snapshot_retention_keep_existing_evidence_path(self) -> None:
        from control_plane.product_secret_copy import ProductSecretCopyFrom

        for snapshot in (False, True):
            with self.subTest(snapshot=snapshot):
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _odoo_preview_profile_payload()
                )
                profile.preview.data_transport_mode = "driver"
                profile.preview.clean_testing_snapshot = snapshot
                issued = _issued_plan(OdooPreviewApplyEnvelope.model_validate(_refresh_payload()))
                if not snapshot:
                    issued.plan_request.fishbowl_source = ProductSecretCopyFrom(
                        context="cm", instance="testing", version_id="fixture-version"
                    )
                with (
                    patch.object(preview_http, "preview_has_completed_restore", return_value=True),
                    patch.object(
                        preview_http,
                        "preview_has_completed_retained_source",
                        side_effect=ValueError("strict legacy intent unavailable"),
                    ),
                ):
                    decision = preview_http.resolve_preview_source_decision(
                        record_store=Mock(),
                        profile=profile,
                        issued_plan=issued,
                        environment={"ODOO_UPSTREAM_HOST": "source.example.test"},
                    )
                self.assertEqual(decision.source, "retained")
                self.assertEqual(decision.fishbowl_import, not snapshot)

    def test_changed_effective_source_refuses_before_the_driver_effect(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        profile.preview.data_transport_mode = "driver"
        envelope = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        issued = _issued_plan(envelope)
        expected = preview_http.resolve_preview_source_decision(
            record_store=Mock(), profile=profile, issued_plan=issued, environment={}
        )
        with (
            patch.object(preview_http, "refresh_odoo_preview_issued_plan", return_value=envelope),
            patch.object(
                preview_http,
                "_odoo_preview_service_environment",
                return_value=preview_http._OdooPreviewServiceEnvironment(
                    values={"ODOO_UPSTREAM_HOST": "source.example.test"}
                ),
            ),
            patch("control_plane.lane_movement.require_forward_preview_build"),
            patch.object(preview_http, "preview_has_completed_restore", return_value=False),
            patch.object(preview_http, "execute_odoo_preview_dokploy_apply") as execute,
            self.assertRaises(ValueError),
        ):
            preview_http.execute_odoo_preview_apply_result(
                control_plane_root_path=Path("."),
                record_store=Mock(),
                profile=profile,
                request=envelope,
                issued_plan=issued,
                database_url=None,
                deployment_record_id="fixture-changed-source",
                expected_source_decision=expected,
            )
        execute.assert_not_called()

    def test_completed_restore_receipt_preserves_copy_and_unknown_binding_refuses_reset(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        envelope = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        plan = envelope.apply.dry_run_plan.model_copy(update={"compose_ref": "compose-fixture"})
        envelope.apply.dry_run_plan = plan
        identity = build_odoo_preview_runtime_identity(
            profile=profile,
            issued_plan=_issued_plan(envelope),
            deployment_record_id="original-operation",
        )
        preview = SimpleNamespace(preview_id=identity.preview_id, state="active")
        generation = SimpleNamespace(
            preview_id=preview.preview_id,
            generation_id=identity.preview_generation_id,
            runtime_identity=identity,
            state="superseded",
        )
        result = {
            "status": "pass",
            "operation": "refresh",
            "product": profile.product,
            "repository": profile.repository,
            "preview_slug": plan.preview_slug,
            "preview_url": plan.preview_url,
            "domain_host": plan.domain_host,
            "compose_id": plan.compose_ref,
            "compose_name": plan.compose_name,
            "module_install_update_status": "pass",
            "module_install_update_evidence": {
                "odoo_restore_completed": "true",
                "upstream_restore_requested": "true",
            },
        }
        operation = SimpleNamespace(
            state="completed",
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            response_status_code=202,
            response_payload={
                "records": {
                    "preview_id": preview.preview_id,
                    "generation_id": generation.generation_id,
                },
                "result": result,
            },
        )
        store = SimpleNamespace(
            list_preview_generation_records=Mock(return_value=[generation]),
            read_idempotency_record_by_id=Mock(return_value=operation),
        )

        def retained() -> bool:
            return preview_has_completed_restore(
                record_store=store, profile=profile, plan=plan, pr_number=42
            )

        with patch("control_plane.odoo_preview_apply_http.find_preview_record", return_value=None):
            self.assertFalse(retained())
        with patch(
            "control_plane.odoo_preview_apply_http.find_preview_record", return_value=preview
        ):
            self.assertTrue(retained())
            changes: tuple[dict[str, object], ...] = (
                {"compose_id": "replacement-compose"},
                {"status": "fail"},
                {"module_install_update_evidence": {}},
                {"module_install_update_evidence": {"odoo_restore_completed": "true"}},
                {"preview_slug": "other-preview"},
            )
            for change in changes:
                operation.response_payload["result"] = {**result, **change}
                with self.subTest(change=change), self.assertRaises(click.ClickException):
                    retained()
            operation.response_payload["result"] = result
            store.read_idempotency_record_by_id.return_value = None
            with self.assertRaises(click.ClickException):
                retained()
            profile.preview.clean_testing_snapshot = True
            store.read_idempotency_record_by_id.return_value = operation
            operation.response_payload["result"] = {**result, "module_install_update_evidence": {}}
            self.assertTrue(retained())

    def test_driver_selects_upstream_restore_only_when_it_has_an_upstream_source(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        for mode in ("none", "driver"):
            selected = profile.model_copy(
                update={"preview": profile.preview.model_copy(update={"data_transport_mode": mode})}
            )
            self.assertFalse(preview_uses_upstream_restore(selected, {}))
            self.assertFalse(preview_uses_upstream_restore(selected, {"ODOO_UPSTREAM_HOST": ""}))
            self.assertEqual(
                preview_uses_upstream_restore(
                    selected, {"ODOO_UPSTREAM_HOST": "source.example.test"}
                ),
                mode == "driver",
            )
            self.assertEqual(
                preview_uses_upstream_restore(selected, {"ODOO_UPSTREAM_USER": "reader"}),
                mode == "driver",
            )

    def test_destroy_no_effect_refusal_releases_reservation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            request.apply.dry_run_plan = request.apply.dry_run_plan.model_copy(
                update={"operation": "destroy", "compose_ref": "owned-preview"}
            )
            issued = _issued_plan(request)
            request = validate_odoo_preview_issued_plan(
                plan_id=_PLAN_ID, issued_plan=issued, request=request
            )
            observe = Mock()
            outcome = run_odoo_preview_apply_operation(
                store=store,
                control_plane_root=root,
                record_store=store,
                profile=profile,
                apply_request=request,
                issued_plan=issued,
                reservation_scope=_WORKER_SCOPE,
                idempotency_key=_PLAN_ID,
                request_fingerprint="no-effect-destroy",
                trace_id="test",
                execute_apply=Mock(
                    return_value={
                        "status": "blocked",
                        "operation": "destroy",
                        "provider_effect_attempted": False,
                    }
                ),
                observe_apply=observe,
            )
            result = outcome.response_payload["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["status"], "blocked")
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=_WORKER_SCOPE,
                    route_path=ODOO_PREVIEW_APPLY_ROUTE,
                    idempotency_key=_PLAN_ID,
                )
            )
            observe.assert_not_called()

    def test_source_capture_refusal_keeps_its_reason_without_reserving_or_dispatching(self) -> None:
        from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
        from control_plane.odoo_preview_apply_http import OdooPreviewPlanProvenanceError

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            execute = Mock()
            operation = prepare_odoo_preview_apply_operation(
                control_plane_root=root,
                record_store=store,
                profile=profile,
                apply_request=request,
                issued_plan=_issued_plan(request),
                reservation_scope=_WORKER_SCOPE,
                idempotency_key=_PLAN_ID,
                request_fingerprint="source-refusal",
                trace_id="source-refusal",
                execute_apply=execute,
            )
            cause = click.ClickException("Original source proof is unavailable")
            with (
                patch.object(preview_http, "resolve_preview_source_decision", side_effect=cause),
                self.assertRaises(OdooPreviewPlanProvenanceError) as refused,
            ):
                operation.run(store)
            self.assertIs(refused.exception.__cause__, cause)
            self.assertEqual(str(refused.exception), str(cause))
            self.assertIsNone(
                store.read_idempotency_record_by_id(operation.adapter._deployment_record_id)
            )
            execute.assert_not_called()

    def test_capacity_or_provider_denial_releases_unused_preview_key(self) -> None:
        from control_plane.dokploy.network_capacity import BridgeNetworkCapacityRefused
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request, issued = _validated_refresh_request()
            observe = Mock()
            failures = (
                BridgeNetworkCapacityRefused("host_bridge_network_capacity_short"),
                NativeSourceAuthorizationDenied("fixture denial"),
            )
            for error in failures:
                with self.subTest(error=type(error)), self.assertRaises(type(error)) as refused:
                    run_odoo_preview_apply_operation(
                        store=store,
                        control_plane_root=root,
                        record_store=store,
                        profile=profile,
                        apply_request=request,
                        issued_plan=issued,
                        reservation_scope=_WORKER_SCOPE,
                        idempotency_key=_PLAN_ID,
                        request_fingerprint="capacity-refusal",
                        trace_id="test",
                        execute_apply=Mock(side_effect=error),
                        observe_apply=observe,
                    )
                self.assertIs(refused.exception, error)
                row = store.read_idempotency_record(
                    scope=_WORKER_SCOPE,
                    route_path=ODOO_PREVIEW_APPLY_ROUTE,
                    idempotency_key=_PLAN_ID,
                )
                self.assertIsNone(row)
            self.assertEqual(store.list_preview_records(), ())
            observe.assert_not_called()

    def test_source_outage_can_retry_the_same_preview_key_and_keeps_each_refusal(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request, issued = _validated_refresh_request()
            execute = Mock(side_effect=LaneMovementRefused("source_order_unavailable"))
            observe = Mock()
            results = [
                run_odoo_preview_apply_operation(
                    store=store,
                    control_plane_root=root,
                    record_store=store,
                    profile=profile,
                    apply_request=request,
                    issued_plan=issued,
                    reservation_scope=_WORKER_SCOPE,
                    idempotency_key=_PLAN_ID,
                    request_fingerprint="outage",
                    trace_id="test",
                    execute_apply=execute,
                    observe_apply=observe,
                )
                for _ in range(2)
            ]
            self.assertNotEqual(results[1].status, "replayed")
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(store.list_preview_records(), ())
            refusals = store.list_deployment_records()
            self.assertEqual(len(refusals), 2)
            self.assertEqual(len({record.record_id for record in refusals}), 2)
            observe.assert_not_called()

    def test_backward_refusal_is_durable_and_replayed_without_lifecycle_change(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            request, issued = _validated_refresh_request()
            execute = Mock(side_effect=LaneMovementRefused("ancestor_build"))
            observe = Mock()
            results = [
                run_odoo_preview_apply_operation(
                    store=store,
                    control_plane_root=root,
                    record_store=store,
                    profile=profile,
                    apply_request=request,
                    issued_plan=issued,
                    reservation_scope=_WORKER_SCOPE,
                    idempotency_key=_PLAN_ID,
                    request_fingerprint="refusal",
                    trace_id="test",
                    execute_apply=execute,
                    observe_apply=observe,
                )
                for _ in range(2)
            ]
            self.assertEqual([result.status for result in results], ["completed", "replayed"])
            result = results[0].response_payload["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["error_code"], "lane_movement.ancestor_build")
            self.assertTrue(result["error_message"])
            assert results[0].record is not None
            persisted = store.read_idempotency_record_by_id(results[0].record.record_id)
            assert persisted is not None
            persisted_result = persisted.response_payload["result"]
            assert isinstance(persisted_result, dict)
            self.assertIs(persisted_result["provider_effect_attempted"], False)
            self.assertEqual(store.list_preview_records(), ())
            execute.assert_called_once()
            observe.assert_not_called()

    def test_runs_durable_preview_apply_without_http_identity_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            store.write_product_profile_record(profile)
            apply_request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            issued_plan = _issued_plan(apply_request)
            service_apply_request = validate_odoo_preview_issued_plan(
                plan_id=_PLAN_ID,
                issued_plan=issued_plan,
                request=apply_request,
            )
            execute_apply = Mock(
                return_value={
                    "status": "pass",
                    "operation": "refresh",
                    "product": "odoo-tenant-cm",
                    "repository": "cbusillo/odoo-tenant-cm",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.cm-preview.example.test",
                    "domain_host": "pr-42.cm-preview.example.test",
                    "compose_name": "cm-odoo-preview-pr-42",
                }
            )
            observe_apply = Mock(side_effect=AssertionError("fresh apply must not observe"))

            def run(trace_id: str) -> DurableProviderOperationResult:
                return run_odoo_preview_apply_operation(
                    store=store,
                    control_plane_root=root,
                    record_store=store,
                    profile=profile,
                    apply_request=service_apply_request,
                    issued_plan=issued_plan,
                    reservation_scope=_WORKER_SCOPE,
                    idempotency_key=_PLAN_ID,
                    request_fingerprint="worker-request-fingerprint",
                    trace_id=trace_id,
                    execute_apply=execute_apply,
                    observe_apply=observe_apply,
                )

            first_result = run("trace-worker-first")
            replay_result = run("trace-worker-replay")

            provider_operation_record = store.read_idempotency_record(
                scope=_WORKER_SCOPE,
                route_path=ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=_PLAN_ID,
            )
            previews = store.list_preview_records(
                context_name="cm",
                anchor_repo="odoo-tenant-cm",
                anchor_pr_number=42,
            )

        self.assertEqual(first_result.status, "completed")
        self.assertEqual(first_result.response_status_code, 202)
        self.assertEqual(first_result.response_payload["trace_id"], "trace-worker-first")
        first_payload_result = first_result.response_payload["result"]
        assert isinstance(first_payload_result, dict)
        self.assertEqual(first_payload_result["status"], "pass")
        first_records = first_result.response_payload["records"]
        assert isinstance(first_records, dict)
        self.assertEqual(first_records["lifecycle_evidence_status"], "applied")
        self.assertEqual(replay_result.status, "replayed")
        execute_apply.assert_called_once()
        observe_apply.assert_not_called()
        expected_deployment_record_id = build_launchplane_mutation_reservation_id(
            scope=_WORKER_SCOPE,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=_PLAN_ID,
        )
        self.assertEqual(
            execute_apply.call_args.kwargs["deployment_record_id"],
            expected_deployment_record_id,
        )
        assert provider_operation_record is not None
        self.assertEqual(provider_operation_record.record_id, expected_deployment_record_id)
        self.assertEqual(provider_operation_record.state, "completed")
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].state, "active")


class OdooPreviewServiceEnvironmentOdooVersionTests(unittest.TestCase):
    def _service_environment(
        self,
        *,
        manifest_odoo_version: str,
        site_odoo_version: str = "",
        preview_payload: str = "",
        website_bootstrap: bool = False,
        snapshot_enabled: bool = False,
        template_extra: dict[str, str] | None = None,
        preview_override: OdooInstanceOverrideRecord | None = None,
    ) -> dict[str, str]:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile_payload = _odoo_preview_profile_payload()
            profile_payload["expected_config"] = {
                "runtime_environment_keys": [
                    {"key": "ODOO_VERSION", "context": "cm", "instance": "testing"}
                ]
            }
            profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
            if preview_override is not None:
                store.write_odoo_instance_override_record(preview_override)
            if preview_payload:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context=profile.preview.context,
                        instance="preview",
                        env={ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: preview_payload},
                        updated_at="2026-10-26T00:00:00Z",
                    )
                )
            if snapshot_enabled:
                profile.preview.clean_testing_snapshot = True
            if website_bootstrap:
                store.write_odoo_instance_override_record(
                    OdooInstanceOverrideRecord(
                        context="cm",
                        instance="testing",
                        addon_settings=(
                            OdooAddonSettingOverride(
                                addon="authentik_sso",
                                setting="client_id",
                                value=OdooOverrideValue(
                                    source="literal", value="template-fixture-client"
                                ),
                            ),
                        ),
                        website_bootstrap=OdooWebsiteBootstrapPayload(
                            tenant="cm",
                            name="Preview fixture",
                            canonical_url="https://testing.example.test",
                        ),
                        updated_at=profile.updated_at,
                    )
                )
            _write_odoo_preview_template_runtime_environment(store=store)
            if site_odoo_version:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="cm",
                        instance="testing",
                        env={
                            "ODOO_DB_USER": "odoo",
                            "ODOO_VERSION": site_odoo_version,
                            **(template_extra or {}),
                        },
                        updated_at="2026-05-09T12:30:00Z",
                        source_label="test",
                    )
                )
            apply_request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            manifest = apply_request.apply.manifest
            assert manifest is not None
            if manifest_odoo_version:
                manifest = manifest.model_copy(
                    update={
                        "build_flags": ArtifactBuildFlags(
                            values={"odoo_version": manifest_odoo_version}
                        )
                    }
                )
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                return _odoo_preview_service_environment(
                    control_plane_root_path=root,
                    record_store=store,
                    profile=profile,
                    plan=apply_request.apply.dry_run_plan,
                    manifest=manifest,
                    database_url=database_url,
                ).values

    def test_snapshot_preview_never_receives_template_source_connections(self) -> None:
        from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key

        imported_key = config_parameter_runtime_key("fishbowl.password")
        values = self._service_environment(
            manifest_odoo_version="19.0",
            site_odoo_version="19.0",
            snapshot_enabled=True,
            template_extra={
                "ODOO_UPSTREAM_HOST": "production.invalid",
                "ODOO_UPSTREAM_PASSWORD": "source-secret",
                "DATA_WORKFLOW_SSH_DIR": "/private-reader",
                "OPENUPGRADE_ENABLED": "true",
                imported_key: "import-secret",
            },
        )
        self.assertFalse(
            any(key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_")) for key in values)
        )
        self.assertNotIn(imported_key, values)
        self.assertEqual(values["BOOTSTRAP"], "false")
        self.assertEqual(values["OPENUPGRADE_ENABLED"], "false")
        self.assertEqual(values["ODOO_MAX_CRON_THREADS"], "0")

    def test_artifact_odoo_version_supplies_a_declared_key_the_site_lacks(self) -> None:
        values = self._service_environment(manifest_odoo_version="19.0")

        self.assertEqual(values["ODOO_VERSION"], "19.0")

    def test_artifact_odoo_version_wins_over_the_site_setting(self) -> None:
        values = self._service_environment(manifest_odoo_version="19.0", site_odoo_version="18.0")

        self.assertEqual(values["ODOO_VERSION"], "19.0")

    def test_manifest_without_odoo_version_still_refuses_the_missing_key(self) -> None:
        with self.assertRaises(OdooPreviewApplyConfigError) as refusal:
            self._service_environment(manifest_odoo_version="")

        self.assertEqual(refusal.exception.missing_keys, ("ODOO_VERSION",))

    def test_template_addon_settings_do_not_enter_generated_preview_bootstrap(self) -> None:
        values = self._service_environment(manifest_odoo_version="19.0", website_bootstrap=True)
        decoded = json.loads(base64.b64decode(values[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]))
        self.assertEqual(decoded["addon_settings"], [])

    def test_exact_preview_can_withdraw_inherited_website_intent(self) -> None:
        from control_plane.odoo_instance_overrides import (
            LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
        )

        plan = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply.dry_run_plan
        values = self._service_environment(
            manifest_odoo_version="19.0",
            website_bootstrap=True,
            preview_override=OdooInstanceOverrideRecord(
                context="cm",
                instance=plan.preview_slug,
                apply_on=("preview",),
                updated_at="2026-10-28T00:00:00Z",
            ),
        )
        self.assertNotIn(LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY, values)
        if encoded := values.get(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY):
            self.assertFalse(json.loads(base64.b64decode(encoded)).get("website_bootstrap"))

    def test_exact_preview_intent_does_not_change_a_sibling(self) -> None:
        expected = self._service_environment(manifest_odoo_version="19.0", website_bootstrap=True)
        actual = self._service_environment(
            manifest_odoo_version="19.0",
            website_bootstrap=True,
            preview_override=OdooInstanceOverrideRecord(
                context="cm",
                instance="pr-99",
                apply_on=("preview",),
                updated_at="2026-10-28T00:00:00Z",
            ),
        )
        self.assertEqual(actual, expected)

    def test_exact_preview_bootstrap_uses_its_own_intent_and_generated_url(self) -> None:
        bootstrap = OdooWebsiteBootstrapPayload(
            name="Own website", canonical_url="https://invalid.example"
        )
        plan = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply.dry_run_plan
        values = self._service_environment(
            manifest_odoo_version="19.0",
            website_bootstrap=True,
            preview_override=OdooInstanceOverrideRecord(
                context="cm",
                instance=plan.preview_slug,
                apply_on=("preview",),
                website_bootstrap=bootstrap,
                updated_at="2026-10-28T00:00:00Z",
            ),
        )
        payload = json.loads(base64.b64decode(values[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]))
        self.assertEqual(payload["instance"], plan.preview_slug)
        self.assertEqual(payload["website_bootstrap"]["name"], bootstrap.name)
        self.assertEqual(payload["website_bootstrap"]["canonical_url"], plan.preview_url)

    def test_exact_preview_withdrawal_refuses_conflicting_carried_intent(self) -> None:
        from control_plane.odoo_instance_overrides import (
            LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
        )

        plan = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply.dry_run_plan
        for extra in (
            {LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY: "true"},
            {
                ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: base64.b64encode(
                    json.dumps(
                        {
                            "context": "cm",
                            "instance": "testing",
                            "website_bootstrap": {"name": "Explicit carried website"},
                        }
                    ).encode()
                ).decode()
            },
        ):
            with self.subTest(extra_keys=list(extra)), self.assertRaises(ValueError):
                self._service_environment(
                    manifest_odoo_version="19.0",
                    site_odoo_version="19.0",
                    template_extra=extra,
                    preview_override=OdooInstanceOverrideRecord(
                        context="cm",
                        instance=plan.preview_slug,
                        apply_on=("preview",),
                        updated_at="2026-10-28T00:00:00Z",
                    ),
                )

    def test_exact_preview_intent_refuses_mixed_phases_and_unrelated_settings(self) -> None:
        plan = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply.dry_run_plan
        for changes in (
            {"apply_on": ("preview", "deploy")},
            {
                "addon_settings": (
                    OdooAddonSettingOverride(
                        addon="authentik_sso",
                        setting="client_id",
                        value=OdooOverrideValue(source="literal", value="must-not-be-dropped"),
                    ),
                )
            },
        ):
            record = OdooInstanceOverrideRecord(
                context="cm",
                instance=plan.preview_slug,
                apply_on=("preview",),
                updated_at="2026-10-28T00:00:00Z",
            ).model_copy(update=changes)
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self._service_environment(manifest_odoo_version="19.0", preview_override=record)

    def test_preview_addon_settings_survive_website_bootstrap(self) -> None:
        settings = [
            {
                "addon": "authentik_sso",
                "setting": "client_id",
                "value": {"source": "literal", "value": "preview-fixture-client"},
            }
        ]
        payload = base64.b64encode(
            json.dumps(
                {
                    "context": "cm",
                    "instance": "preview",
                    "addon_settings": settings,
                }
            ).encode()
        ).decode()
        values = self._service_environment(
            manifest_odoo_version="19.0", preview_payload=payload, website_bootstrap=True
        )
        decoded = json.loads(base64.b64decode(values[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]))
        self.assertEqual(decoded["addon_settings"], settings)
        self.assertEqual(
            decoded["website_bootstrap"]["canonical_url"],
            OdooPreviewApplyEnvelope.model_validate(
                _refresh_payload()
            ).apply.dry_run_plan.preview_url,
        )


if __name__ == "__main__":
    unittest.main()
