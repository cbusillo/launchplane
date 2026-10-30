import base64
from contextlib import contextmanager
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import runpy
from tempfile import TemporaryDirectory
from typing import Callable, Iterator, cast
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    SUPERSEDED_PROVIDER_EFFECT_PHASE,
)
from control_plane.contracts.preview_fixture import (
    PREVIEW_FIXTURE_CANCEL_PHASE,
    PreviewFixtureBinding,
    PreviewFixtureIntent,
    PreviewFixtureReceipt,
    PreviewFixtureRequest,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity, RUNTIME_IDENTITY_ENV_KEY
from control_plane.host_package import host_package
from control_plane.odoo_app_maintenance_http import OdooAppMaintenanceEnvelope
from control_plane.preview_fixture import (
    PREVIEW_FIXTURE_RESULT_MARKER,
    FixtureContainerStillRunning,
    PreviewFixtureAdapter,
    fixture_plan_sha256,
    fixture_schedule_app_name,
    fixture_schedule_script,
    run_fixture_schedule,
)
from control_plane.dokploy.post_deploy import (
    OdooPostDeployReadbackFailure,
    ODOO_POST_DEPLOY_SCHEDULE_UPSERT,
    ODOO_POST_DEPLOY_SCHEDULE_TRIGGER,
)
from control_plane.provider_operations import (
    DurableProviderMutationAdapter,
    DurableProviderOperationResult,
    ProviderOperationLease,
    ProviderMutationRejectedError,
    ProviderMutationOutcome,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_preview_apply_execution import (
    OdooPreviewApplyOperation,
    prepare_odoo_preview_apply_operation,
)
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_odoo_preview_apply_execution import _refresh_payload, _issued_plan
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import StubVerifier, identity
from tests.support.http import request


def fixture_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        product="example-product",
        context="example",
        instance="example-pr-42",
        environment_kind="preview",
        deployment_record_id="deploy-example",
        artifact_id="artifact-example",
        source_git_ref="a" * 40,
        image_reference="registry.example/product@sha256:" + "b" * 64,
        preview_id="preview-example",
        preview_generation_id="generation-example",
    )


def fixture_request(**updates: object) -> PreviewFixtureRequest:
    return PreviewFixtureRequest.model_validate(
        {
            "action": "preview-fixture",
            "intent": "preview-fixture",
            "context": "example",
            "instance": "example-pr-42",
            "preview_id": "preview-example",
            "generation_id": "generation-example",
            "fixture": "sample-parts",
            "reason": "Exercise sample parts",
            **updates,
        }
    )


def fixture_receipt(mode: str, **updates: object) -> PreviewFixtureReceipt:
    return PreviewFixtureReceipt.model_validate(
        {
            "identity": fixture_identity().model_dump(mode="json"),
            "fixture": "sample-parts",
            "fixture_sha256": canonical_json_sha256({"parts": 3}),
            "mode": mode,
            "committed": mode == "apply",
            "verified": True,
            "records": [{"model": "product.product", "id": 8, "disposition": "created"}],
            **updates,
        }
    )


def fixture_binding() -> PreviewFixtureBinding:
    return PreviewFixtureBinding(
        identity=fixture_identity(),
        target_id="compose-example",
        target_name="example-pr-42",
        container_id="d" * 64,
        app_name="sample-app",
        database="sample",
        schedule_type="server",
        lookup_id="server",
        server_id="server",
        anchor_pr_number=42,
    )


class PreviewFixtureTests(unittest.TestCase):
    def test_unavailable_readback_does_not_block_recovery_before_trigger(self) -> None:
        adapter = PreviewFixtureAdapter(
            Mock(), Path("."), "example-product", fixture_request(), "trace"
        )
        with patch.object(
            PreviewFixtureAdapter, "invoke", side_effect=ValueError("container unavailable")
        ) as invoke:
            observation = adapter.observe("original", "", adapter.reconciliation_key())
            self.assertEqual(observation.outcome, "absent")
            self.assertTrue(observation.retry_safe)
            invoke.assert_not_called()
            triggered = adapter.observe(
                "original", ODOO_POST_DEPLOY_SCHEDULE_TRIGGER, adapter.reconciliation_key()
            )
            self.assertEqual(triggered.outcome, "unknown")
            invoke.assert_called_once_with("readback")

    def test_setup_failure_releases_but_trigger_uncertainty_keeps_fixture_custody(self) -> None:
        for failure in ("setup", "trigger", "crash"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
                )
                store.ensure_schema()
                self.addCleanup(store.close)
                binding = fixture_binding()
                planned = fixture_receipt("dry-run")
                adapter = PreviewFixtureAdapter(
                    store,
                    Path(directory),
                    "example-product",
                    fixture_request(
                        mode="apply", reviewed_plan_sha256=fixture_plan_sha256(planned)
                    ),
                    "trace",
                    binding,
                )
                connection = Mock(
                    selection=Mock(identity=binding.identity),
                    evidence=Mock(target_id=binding.target_id, container_id=binding.container_id),
                    credentials={"ODOO_DB_NAME": binding.database},
                    app_name=binding.app_name,
                )

                attempts = {"apply": 0}

                def schedule(**kwargs: object) -> dict[str, str]:
                    mode = kwargs["mode"]
                    assert isinstance(mode, str)
                    if mode == "apply":
                        attempts["apply"] += 1
                        if failure == "crash" and attempts["apply"] == 1:
                            raise KeyboardInterrupt
                        self.assertEqual(kwargs["expected_fixture_sha256"], planned.fixture_sha256)
                        callback = cast(Callable[[str], None], kwargs["before_provider_mutation"])
                        callback(ODOO_POST_DEPLOY_SCHEDULE_UPSERT)
                        if failure in {"trigger", "crash"}:
                            callback(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                        if failure != "crash":
                            raise OSError("provider unavailable")
                    receipt = fixture_receipt(
                        mode,
                        records=[]
                        if mode == "readback"
                        else planned.model_dump(mode="json")["records"],
                    )
                    return {PREVIEW_FIXTURE_RESULT_MARKER: receipt.model_dump_json().encode().hex()}

                with (
                    patch.object(PreviewFixtureAdapter, "connect", return_value=connection),
                    patch.object(PreviewFixtureAdapter, "runtime_binding", return_value=binding),
                    patch.object(
                        PreviewFixtureAdapter, "selection", return_value=connection.selection
                    ) as qualification,
                    patch("control_plane.preview_fixture.reads.assert_selection_current"),
                    patch(
                        "control_plane.preview_fixture.run_fixture_schedule", side_effect=schedule
                    ),
                ):

                    def run() -> DurableProviderOperationResult:
                        return run_durable_provider_operation(
                            store=store,
                            scope="fixture",
                            route_path="/v1/drivers/odoo/app-maintenance",
                            idempotency_key="sample",
                            request_fingerprint="sample",
                            lease_owner="worker",
                            response_trace_id="trace",
                            adapter=adapter,
                        )

                    if failure == "setup":
                        with self.assertRaises(ValueError):
                            run()
                    elif failure == "crash":
                        with self.assertRaises(KeyboardInterrupt):
                            run()
                        before = store.read_idempotency_record(
                            scope="fixture",
                            route_path="/v1/drivers/odoo/app-maintenance",
                            idempotency_key="sample",
                        )
                        assert before is not None
                        self.assertFalse(before.provider_effect_phase)
                        self.assertEqual(run().status, "completed")
                        self.assertEqual(attempts["apply"], 2)
                    else:
                        self.assertEqual(run().status, "reconcile_required")
                        self.assertEqual(run().status, "reconcile_required")
                        self.assertEqual(attempts["apply"], 1)
                held = store.read_idempotency_record(
                    scope="fixture",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key="sample",
                )
                self.assertEqual(held is not None, failure != "setup")
                if held is not None:
                    self.assertEqual(
                        held.state, "completed" if failure == "crash" else "reconcile_required"
                    )
                self.assertEqual(qualification.call_count, 1 if failure == "setup" else 2)

    def test_historical_readback_reconnects_only_within_original_runtime_and_database(self) -> None:
        binding = fixture_binding()
        for change, allowed in (
            ("container", True),
            ("generation", False),
            ("database", False),
            ("target", False),
        ):
            with self.subTest(change=change):
                current = binding.model_copy(update={"container_id": "e" * 64})
                selected_identity = (
                    current.identity.model_copy(
                        update={"preview_generation_id": "different-generation"}
                    )
                    if change == "generation"
                    else current.identity
                )
                connection = Mock(
                    selection=Mock(identity=selected_identity),
                    evidence=Mock(
                        target_id="another-target" if change == "target" else current.target_id,
                        container_id=current.container_id,
                    ),
                    credentials={
                        "ODOO_DB_NAME": "another-db" if change == "database" else current.database
                    },
                    app_name=current.app_name,
                )
                adapter = PreviewFixtureAdapter(
                    Mock(), Path("."), "example-product", fixture_request(), "trace", binding
                )
                evidence = {
                    PREVIEW_FIXTURE_RESULT_MARKER: fixture_receipt("readback")
                    .model_dump_json()
                    .encode()
                    .hex()
                }
                with (
                    patch.object(PreviewFixtureAdapter, "connect", return_value=connection),
                    patch.object(PreviewFixtureAdapter, "runtime_binding", return_value=current),
                    patch("control_plane.preview_fixture.reads.assert_selection_current"),
                    patch(
                        "control_plane.preview_fixture.run_fixture_schedule", return_value=evidence
                    ) as schedule,
                ):
                    if allowed:
                        self.assertTrue(adapter.invoke("readback").records)
                        self.assertEqual(
                            schedule.call_args.kwargs["binding"].container_id, current.container_id
                        )
                    else:
                        with self.assertRaises(ValueError):
                            adapter.invoke("readback")
                        schedule.assert_not_called()
                    with self.assertRaises(ValueError):
                        adapter.invoke(
                            "apply",
                            Mock(),
                            expected_fixture_sha256=fixture_receipt("apply").fixture_sha256,
                        )
                self.assertIs(adapter.binding, binding)

    def test_completed_and_rejected_fixtures_do_not_enter_source_free_refresh_history(self) -> None:
        from control_plane.preview_source import (
            PreviewSourceDecision,
            require_source_free_preview_history,
        )

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            old_request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            old_plan = _issued_plan(old_request)
            assert old_plan.plan_provenance is not None
            source_key = old_plan.plan_provenance.plan_id
            operation = prepare_odoo_preview_apply_operation(
                control_plane_root=Path(directory),
                record_store=store,
                profile=profile,
                apply_request=old_request,
                issued_plan=old_plan,
                reservation_scope="source",
                idempotency_key=source_key,
                request_fingerprint="original",
                trace_id="source",
            )
            operation.adapter.source_decision = PreviewSourceDecision(
                source="none", fishbowl_import=False
            )
            provider = Mock()
            provider.reconciliation_key.return_value = operation.adapter.reconciliation_key()
            provider.target_key.return_value = operation.adapter.target_key()
            provider.apply.return_value = ProviderMutationOutcome(
                202, {"result": {"compose_id": "compose-example"}}, provider_effect_performed=False
            )
            original = run_durable_provider_operation(
                store=store,
                scope="source",
                route_path=operation.route_path,
                idempotency_key=source_key,
                request_fingerprint="original",
                lease_owner="source",
                response_trace_id="source",
                adapter=cast(DurableProviderMutationAdapter, cast(object, provider)),
                original_intent=operation.adapter.original_intent(),
            )
            self.assertEqual(original.status, "completed")
            payload = _refresh_payload()
            cast(dict[str, object], cast(dict[str, object], payload["apply"])["dry_run_plan"])[
                "compose_ref"
            ] = "compose-example"
            refresh = _issued_plan(OdooPreviewApplyEnvelope.model_validate(payload))
            fixture_runtime = fixture_identity().model_copy(
                update={
                    "product": profile.product,
                    "context": profile.preview.context,
                    "instance": refresh.dry_run_plan.compose_name,
                }
            )
            fixture = fixture_request(mode="apply", reviewed_plan_sha256="c" * 64).model_copy(
                update={"context": fixture_runtime.context, "instance": fixture_runtime.instance}
            )
            intent = PreviewFixtureIntent(
                product=profile.product,
                maintenance=fixture,
                fixture_binding=fixture_binding().model_copy(update={"identity": fixture_runtime}),
            )
            for status in (202, 409):
                provider.apply.return_value = ProviderMutationOutcome(
                    status,
                    {"result": {"receipt": {"committed": status == 202}}},
                    provider_effect_performed=False,
                )
                outcome = run_durable_provider_operation(
                    store=store,
                    scope="fixture",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key=str(status),
                    request_fingerprint=str(status),
                    lease_owner="fixture",
                    response_trace_id="fixture",
                    adapter=cast(DurableProviderMutationAdapter, cast(object, provider)),
                    original_intent=intent.model_dump(mode="json"),
                )
                self.assertEqual(outcome.status, "completed")
                require_source_free_preview_history(
                    store, profile=profile, issued_plan=refresh, operation_record_id="next-refresh"
                )
                self.assertEqual(
                    len(
                        store.list_odoo_preview_originals(
                            product=profile.product, number=refresh.plan_request.pr_number
                        )
                    ),
                    1,
                )

    def test_schedules_reuse_exact_invocations_and_isolate_every_script_path(self) -> None:
        binding = fixture_binding()
        cases = [
            (binding, "apply", "sample-parts"),
            (binding, "apply", "sample-parts"),
            *[
                (binding, mode, "sample-parts")
                for mode in ("dry-run", "readback", "cancel", "cancel-readback")
            ],
            (binding, "apply", "other-parts"),
            (binding.model_copy(update={"container_id": "e" * 64}), "apply", "sample-parts"),
            (
                binding.model_copy(
                    update={
                        "identity": binding.identity.model_copy(
                            update={"preview_generation_id": "another-generation"}
                        )
                    }
                ),
                "apply",
                "sample-parts",
            ),
        ]
        with patch(
            "control_plane.preview_fixture.post_deploy.run_manual_data_schedule", return_value={}
        ) as schedule:
            for target, mode, fixture in cases:
                run_fixture_schedule(
                    host="https://provider.invalid",
                    token="fixture-token",
                    binding=target,
                    mode=mode,
                    fixture=fixture,
                )
        names = [call.kwargs["schedule_name"] for call in schedule.call_args_list]
        paths = [call.kwargs["schedule_app_name"] for call in schedule.call_args_list]
        self.assertEqual(names[0], names[1])
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(len(set(names)), len(cases) - 1)
        self.assertEqual(len(set(paths)), len(cases) - 1)
        for call in schedule.call_args_list:
            self.assertEqual(
                call.kwargs["schedule_app_name"], call.kwargs["schedule_payload"]["appName"]
            )
        script = fixture_schedule_script(binding, fixture="sample-parts", mode="apply")
        self.assertNotEqual(
            fixture_schedule_app_name(script), fixture_schedule_app_name(script + " ")
        )

    def test_original_intent_recovers_its_original_binding_without_provider_access(self) -> None:
        adapter = PreviewFixtureAdapter(
            Mock(),
            Path("."),
            "example-product",
            fixture_request(mode="apply", reviewed_plan_sha256="c" * 64),
            "trace",
        )
        binding = fixture_binding()
        with patch.object(PreviewFixtureAdapter, "capture_binding", return_value=binding):
            intent = adapter.original_intent()
        with patch.object(
            PreviewFixtureAdapter, "capture_binding", side_effect=ValueError("runtime unavailable")
        ):
            self.assertEqual(adapter.original_intent(intent), intent)

    def test_read_qualification_does_not_apply_mutation_holds(self) -> None:
        adapter = PreviewFixtureAdapter(
            Mock(), Path("."), "example-product", fixture_request(), "trace"
        )
        selected = Mock(identity=fixture_identity())
        with (
            patch(
                "control_plane.preview_fixture.reads.select_preview_runtime", return_value=selected
            ),
            patch(
                "control_plane.preview_fixture.require_disposable_lane",
                side_effect=ValueError("held"),
            ) as guard,
        ):
            self.assertIs(adapter.selection(), selected)
            guard.assert_not_called()
            with self.assertRaises(ValueError):
                adapter.selection(mutation=True)
            guard.assert_called_once()

    def test_historical_witness_adopts_before_mutable_sample_qualification(self) -> None:
        receipt = fixture_receipt("readback")
        adapter = PreviewFixtureAdapter(
            Mock(),
            Path("."),
            "example-product",
            fixture_request(mode="apply", reviewed_plan_sha256=fixture_plan_sha256(receipt)),
            "trace",
        )
        with (
            patch.object(PreviewFixtureAdapter, "invoke", return_value=receipt) as invoke,
            patch.object(
                PreviewFixtureAdapter, "plan", side_effect=ValueError("sample was used")
            ) as plan,
        ):
            outcome = adapter.apply("original", cast(ProviderOperationLease, cast(object, Mock())))
        self.assertTrue(outcome.durable)
        self.assertFalse(outcome.provider_effect_performed)
        invoke.assert_called_once_with("readback")
        plan.assert_not_called()

    def test_explicit_product_rejection_settles_without_claiming_data_commit(self) -> None:
        dry_run = fixture_receipt("dry-run")
        adapter = PreviewFixtureAdapter(
            Mock(),
            Path("."),
            "example-product",
            fixture_request(mode="apply", reviewed_plan_sha256=fixture_plan_sha256(dry_run)),
            "trace",
        )
        with (
            patch.object(
                PreviewFixtureAdapter,
                "invoke",
                side_effect=[
                    fixture_receipt("readback", records=[]),
                    dry_run,
                    fixture_receipt("apply", committed=False, records=[], fixture_sha256="f" * 64),
                ],
            ),
        ):
            outcome = adapter.apply("original", cast(ProviderOperationLease, cast(object, Mock())))
        self.assertEqual(outcome.response_status_code, 409)
        self.assertTrue(outcome.durable)
        result = cast(dict[str, object], outcome.response_payload["result"])
        receipt = PreviewFixtureReceipt.model_validate(result["receipt"])
        self.assertFalse(receipt.committed)
        self.assertFalse(receipt.records)

    def test_existing_maintenance_route_accepts_fixture_and_refuses_stable_targets_and_caller_code(
        self,
    ) -> None:
        envelope = OdooAppMaintenanceEnvelope(
            product="example-product", maintenance=fixture_request()
        )
        self.assertIsInstance(envelope.maintenance, PreviewFixtureRequest)
        for update in (
            {"instance": "testing"},
            {"instance": "prod"},
            {"script": "write()"},
            {"fixture": "../fixture"},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                fixture_request(**update)

    def test_receipt_refuses_business_values_and_duplicate_record_identities(self) -> None:
        for update in (
            {
                "records": [
                    {
                        "model": "product.product",
                        "id": 8,
                        "disposition": "created",
                        "name": "private",
                    }
                ]
            },
            {"records": fixture_receipt("apply").model_dump(mode="json")["records"] * 2},
            {"committed": False},
            {
                "identity": fixture_identity()
                .model_copy(update={"environment_kind": "stable"})
                .model_dump(mode="json")
            },
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                fixture_receipt("apply", **update)

    def test_review_drift_rejects_before_fixture_apply(self) -> None:
        adapter = PreviewFixtureAdapter(
            Mock(),
            Path("."),
            "example-product",
            fixture_request(mode="apply", reviewed_plan_sha256="c" * 64),
            "trace",
        )
        with patch.object(
            PreviewFixtureAdapter, "invoke", return_value=fixture_receipt("dry-run")
        ) as invoke:
            with self.assertRaises(ProviderMutationRejectedError):
                adapter.apply("original", cast(ProviderOperationLease, cast(object, Mock())))
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ["readback"])

    def test_preflight_provider_failures_release_the_target_without_dispatch(self) -> None:
        for error in (
            OdooPostDeployReadbackFailure("refused", evidence={}),
            FileNotFoundError("missing"),
            OSError("provider unavailable"),
        ):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
                )
                store.ensure_schema()
                self.addCleanup(store.close)
                adapter = PreviewFixtureAdapter(
                    store,
                    Path(directory),
                    "example-product",
                    fixture_request(
                        mode="apply",
                        reviewed_plan_sha256=canonical_json_sha256(
                            {
                                "identity": fixture_identity().model_dump(mode="json"),
                                "fixture": "sample-parts",
                                "fixture_sha256": fixture_receipt("dry-run").fixture_sha256,
                            }
                        ),
                    ),
                    "trace",
                )
                with (
                    patch.object(PreviewFixtureAdapter, "plan", side_effect=error),
                    patch.object(
                        PreviewFixtureAdapter,
                        "invoke",
                        return_value=fixture_receipt("readback", records=[]),
                    ) as invoke,
                ):
                    with self.assertRaises(ValueError):
                        run_durable_provider_operation(
                            store=store,
                            scope="operator:example",
                            route_path="/v1/drivers/odoo/app-maintenance",
                            idempotency_key="failure",
                            request_fingerprint="same",
                            lease_owner="test",
                            response_trace_id="trace",
                            adapter=adapter,
                        )
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope="operator:example",
                        route_path="/v1/drivers/odoo/app-maintenance",
                        idempotency_key="failure",
                    )
                )
                self.assertEqual([call.args[0] for call in invoke.call_args_list], ["readback"])

    def test_absent_witness_never_retries_an_uncertain_writer(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            planned = fixture_receipt("dry-run")
            adapter = PreviewFixtureAdapter(
                store, Path(directory), "example-product", fixture_request(), "trace"
            )
            with patch.object(PreviewFixtureAdapter, "invoke", return_value=planned):
                _receipt, digest = adapter.plan()
            adapter.request = fixture_request(mode="apply", reviewed_plan_sha256=digest)
            calls: list[str] = []

            def invoke(
                _adapter: PreviewFixtureAdapter,
                mode: str,
                lease: ProviderOperationLease | None = None,
                **_kwargs: object,
            ) -> PreviewFixtureReceipt:
                calls.append(mode)
                if mode == "apply":
                    assert lease is not None
                    lease.checkpoint_effect("fixture_started")
                    raise OdooPostDeployReadbackFailure("product refused", evidence={})
                if mode == "readback":
                    return fixture_receipt(mode, records=[])
                if "apply" in calls:
                    raise ValueError("sample conflict")
                return planned

            def run() -> DurableProviderOperationResult:
                return run_durable_provider_operation(
                    store=store,
                    scope="operator:example",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key="failure",
                    request_fingerprint="same",
                    lease_owner="test",
                    response_trace_id="trace",
                    adapter=adapter,
                )

            with patch.object(PreviewFixtureAdapter, "invoke", invoke):
                first = run()
                second = run()
            self.assertEqual(first.status, "reconcile_required")
            self.assertEqual(second.status, "reconcile_required")
            self.assertEqual(calls, ["readback", "dry-run", "apply", "readback"])
            self.assertIsNotNone(
                store.read_idempotency_record(
                    scope="operator:example",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key="failure",
                )
            )

    def test_repeat_and_uncertain_apply_use_durable_receipt_without_repeating_write(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            planned = fixture_receipt("dry-run")
            digest = canonical_json_sha256(
                {
                    "identity": planned.identity.model_dump(mode="json"),
                    "fixture": planned.fixture,
                    "fixture_sha256": planned.fixture_sha256,
                }
            )
            adapter = PreviewFixtureAdapter(
                store,
                Path(directory),
                "example-product",
                fixture_request(mode="apply", reviewed_plan_sha256=digest),
                "trace",
            )
            calls: list[str] = []

            def invoke(
                _adapter: PreviewFixtureAdapter,
                mode: str,
                lease: ProviderOperationLease | None = None,
                **_kwargs: object,
            ) -> PreviewFixtureReceipt:
                calls.append(mode)
                if mode == "apply":
                    assert lease is not None
                    lease.checkpoint_effect("fixture_started")
                    raise ValueError("Receipt lost after the write")
                return fixture_receipt(
                    mode,
                    records=[]
                    if mode == "readback" and "apply" not in calls
                    else fixture_receipt(mode).model_dump(mode="json")["records"],
                )

            def run() -> DurableProviderOperationResult:
                return run_durable_provider_operation(
                    store=store,
                    scope="operator:example",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key="sample",
                    request_fingerprint="same-request",
                    lease_owner="test",
                    response_trace_id="trace",
                    adapter=adapter,
                )

            with patch.object(PreviewFixtureAdapter, "invoke", invoke):
                first = run()
                self.assertEqual(first.status, "reconcile_required")
                second = run()
                third = run()
            self.assertEqual(second.status, "adopted")
            self.assertEqual(third.status, "replayed")
            self.assertEqual(calls, ["readback", "dry-run", "apply", "readback"])
            recovered = PreviewFixtureReceipt.model_validate(
                cast(dict[str, object], second.response_payload["result"])["receipt"]
            )
            self.assertEqual(recovered.records, planned.records)


class PreviewFixtureHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_authority_and_saved_review_control_the_existing_maintenance_path(
        self,
    ) -> None:
        caller = identity()
        for action, mode, expected in (
            ("odoo_app_maintenance.execute", "apply", 403),
            ("odoo_preview_apply.execute", "apply", 202),
            ("operations.read", "apply", 403),
            ("operations.read", "dry-run", 403),
            ("odoo_preview_apply.execute", "dry-run", 202),
        ):
            with self.subTest(action=action, mode=mode), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
                )
                store.ensure_schema()
                self.addCleanup(store.close)
                policy = LaunchplaneAuthzPolicy.model_validate(
                    {
                        "schema_version": 2,
                        "github_actions": [
                            {
                                "repository": caller.repository,
                                "workflow_refs": [caller.workflow_ref],
                                "event_names": [caller.event_name],
                                "products": ["example-product"],
                                "contexts": ["example"],
                                "instances": []
                                if action == "odoo_preview_apply.execute"
                                else ["example-pr-42"],
                                "actions": [action],
                            }
                        ],
                    }
                )
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(caller),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                    control_plane_root_path=Path(directory),
                )
                planned = fixture_receipt("dry-run")

                def invoke_fixture(
                    fixture_mode: str,
                    lease: ProviderOperationLease | None = None,
                    **_kwargs: object,
                ) -> PreviewFixtureReceipt:
                    if lease is not None:
                        lease.checkpoint_effect("fixture_started")
                    return fixture_receipt(
                        fixture_mode,
                        records=[]
                        if fixture_mode == "readback"
                        else fixture_receipt(fixture_mode).model_dump(mode="json")["records"],
                    )

                with (
                    patch.object(
                        PreviewFixtureAdapter,
                        "plan",
                        return_value=(planned, fixture_plan_sha256(planned)),
                    ),
                    patch.object(
                        PreviewFixtureAdapter, "capture_binding", return_value=fixture_binding()
                    ),
                    patch.object(
                        PreviewFixtureAdapter, "invoke", side_effect=invoke_fixture
                    ) as invoke,
                ):
                    response = await request(
                        app,
                        "POST",
                        "/v1/drivers/odoo/app-maintenance",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "fixture-example",
                        },
                        payload={
                            "product": "example-product",
                            "maintenance": fixture_request(
                                mode=mode,
                                reviewed_plan_sha256=fixture_plan_sha256(planned)
                                if mode == "apply"
                                else "",
                            ).model_dump(mode="json"),
                        },
                    )
                self.assertEqual(response.status_code, expected, response.text)
                if expected == 403 or mode == "dry-run":
                    invoke.assert_not_called()


class PreviewFixtureClientTests(unittest.TestCase):
    def test_readback_needs_no_review_and_malformed_response_is_unverified(self) -> None:
        client = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "preview-fixture.py"))
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "request.json"
            payload.write_text(
                json.dumps(
                    {
                        "product": "example-product",
                        "maintenance": fixture_request().model_dump(mode="json"),
                    }
                )
            )
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            transport = {
                "resolve_settings": lambda _args: {
                    "service_url": "https://example.invalid",
                    "token": "fixture-token",
                },
                "validate_service_url": lambda _url: None,
                "request_launchplane": lambda **_kwargs: {"status": "accepted"},
            }
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "preview-fixture.py",
                        "readback",
                        "--payload-file",
                        str(payload),
                        "--operator-helper",
                        str(helper),
                    ],
                ),
                patch.object(runpy, "run_path", return_value=transport),
                patch.object(sys, "stdout", output),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "unverified")
            self.assertNotIn("fixture-token", output.getvalue())


class PreviewFixtureHostTests(unittest.TestCase):
    @contextmanager
    def host(
        self,
        receipt: dict[str, object],
        *,
        stable: bool = False,
        expected_fixture_sha256: str | None = None,
    ) -> Iterator[subprocess.CompletedProcess[str]]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "launchplane.pyz"
            archive.write_bytes(host_package().content)
            docker = root / "docker"
            docker.write_text(
                "#!" + sys.executable + "\nimport json,os,sys\n"
                "print(os.environ['FIXTURE_INSPECT'] if sys.argv[1]=='inspect' else os.environ['FIXTURE_REPLY'])\n"
            )
            docker.chmod(0o700)
            expected_identity = fixture_identity().model_dump(mode="json")
            observed = (
                dict(expected_identity, environment_kind="stable") if stable else expected_identity
            )
            config = {
                "State": {"Running": True},
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "sample-app",
                        "com.docker.compose.service": "web",
                    },
                    "Env": [
                        RUNTIME_IDENTITY_ENV_KEY + "=" + json.dumps(observed),
                        "ODOO_DB_NAME=sample",
                    ],
                    "Image": expected_identity["image_reference"],
                },
            }
            spec = {
                "identity": expected_identity,
                "database": "sample",
                "fixture": "sample-parts",
                "mode": "apply",
                "container_id": "container-example",
                "app_name": "sample-app",
                "expected_fixture_sha256": fixture_receipt("apply").fixture_sha256
                if expected_fixture_sha256 is None
                else expected_fixture_sha256,
            }
            yield subprocess.run(
                [
                    sys.executable,
                    str(archive),
                    "preview_fixture",
                    "--input",
                    base64.b64encode(json.dumps(spec).encode()).decode(),
                ],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.defpath,
                    "FIXTURE_INSPECT": json.dumps([config]),
                    "FIXTURE_REPLY": json.dumps(receipt),
                },
                text=True,
                capture_output=True,
                timeout=15,
            )

    def test_packaged_host_accepts_bound_receipt_and_refuses_target_drift_and_private_output(
        self,
    ) -> None:
        good = fixture_receipt("apply").model_dump(mode="json")
        for digest in ("", "invalid", "a" * 63):
            with (
                self.subTest(digest=digest),
                self.host(good, expected_fixture_sha256=digest) as result,
            ):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(PREVIEW_FIXTURE_RESULT_MARKER + "=", result.stdout)
        with self.host(good) as result:
            self.assertEqual(result.returncode, 0, result.stderr)
            encoded = next(
                line.partition("=")[2]
                for line in result.stdout.splitlines()
                if line.startswith(PREVIEW_FIXTURE_RESULT_MARKER + "=")
            )
            self.assertEqual(json.loads(bytes.fromhex(encoded)), good)
        with self.host(good, stable=True) as result:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(PREVIEW_FIXTURE_RESULT_MARKER + "=", result.stdout)
        with self.host({**good, "private_data": "PRIVATE-CUSTOMER"}) as result:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("PRIVATE-CUSTOMER", result.stdout + result.stderr)


class PreviewFixtureCancellationHostTests(unittest.TestCase):
    def test_cancel_stops_only_the_bound_container_and_repeats_without_another_stop(self) -> None:
        for drift in (False, True):
            with self.subTest(drift=drift), TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "launchplane.pyz"
                archive.write_bytes(host_package().content)
                stopped = root / "stopped"
                calls = root / "calls"
                docker = root / "docker"
                docker.write_text(
                    "#!" + sys.executable + "\nimport json,os,pathlib,sys\n"
                    "with open(os.environ['CALLS'],'a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\n"
                    "stopped=pathlib.Path(os.environ['STOPPED'])\n"
                    "if sys.argv[1]=='container': print(os.environ['CONTAINER'])\n"
                    "elif sys.argv[1]=='stop': stopped.touch()\n"
                    "else:\n"
                    " value=json.loads(os.environ['INSPECT']);value[0]['State']['Running']=not stopped.exists();print(json.dumps(value))\n"
                )
                docker.chmod(0o700)
                runtime = fixture_identity().model_dump(mode="json")
                observed = {**runtime, "environment_kind": "stable"} if drift else runtime
                container = "d" * 64
                spec = {
                    "identity": runtime,
                    "mode": "cancel",
                    "container_id": container,
                    "database": "sample",
                    "app_name": "sample-app",
                }
                inspect = [
                    {
                        "State": {"Running": True, "Restarting": False},
                        "Config": {
                            "Image": runtime["image_reference"],
                            "Labels": {
                                "com.docker.compose.project": "sample-app",
                                "com.docker.compose.service": "web",
                            },
                            "Env": [
                                RUNTIME_IDENTITY_ENV_KEY + "=" + json.dumps(observed),
                                "ODOO_DB_NAME=sample",
                            ],
                        },
                    }
                ]
                for _attempt in range(2):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(archive),
                            "preview_fixture",
                            "--input",
                            base64.b64encode(json.dumps(spec).encode()).decode(),
                        ],
                        env={
                            **os.environ,
                            "PATH": str(root) + os.pathsep + os.defpath,
                            "STOPPED": str(stopped),
                            "CALLS": str(calls),
                            "CONTAINER": container,
                            "INSPECT": json.dumps(inspect),
                        },
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode == 0, not drift, result.stderr)
                actions = [json.loads(line) for line in calls.read_text().splitlines()]
                stops = [action for action in actions if action[0] == "stop"]
                self.assertEqual(stops, [] if drift else [["stop", "--time", "30", container]])
                self.assertEqual(stopped.exists(), not drift)

    def test_original_writer_must_be_stopped_or_independently_absent(self) -> None:
        container = "d" * 64
        for inventory, state, docker_error, permitted in (
            (container, {"Running": True, "Restarting": False}, False, False),
            (container, {"Running": False, "Restarting": False}, False, True),
            (container, {"Running": False, "Restarting": True}, False, False),
            ("", {}, False, True),
            ("", {}, True, None),
            ("malformed", {}, False, None),
        ):
            with (
                self.subTest(inventory=inventory, state=state, docker_error=docker_error),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                archive = root / "launchplane.pyz"
                archive.write_bytes(host_package().content)
                docker = root / "docker"
                docker.write_text(
                    "#!" + sys.executable + "\nimport json,os,sys\n"
                    "if os.environ['DOCKER_ERROR']=='1': sys.exit(1)\n"
                    "sys.stdout.write(os.environ['INVENTORY'] if sys.argv[1]=='container' else os.environ['INSPECT'])\n"
                )
                docker.chmod(0o700)
                spec = {
                    "identity": fixture_identity().model_dump(mode="json"),
                    "mode": "cancel-readback",
                    "container_id": container,
                    "app_name": "sample-app",
                    "database": "sample",
                }
                result = subprocess.run(
                    [
                        sys.executable,
                        str(archive),
                        "preview_fixture",
                        "--input",
                        base64.b64encode(json.dumps(spec).encode()).decode(),
                    ],
                    env={
                        **os.environ,
                        "PATH": str(root) + os.pathsep + os.defpath,
                        "INVENTORY": inventory,
                        "INSPECT": json.dumps(
                            [
                                {
                                    "State": state,
                                    "Config": {
                                        "Image": fixture_identity().image_reference,
                                        "Labels": {
                                            "com.docker.compose.project": "sample-app",
                                            "com.docker.compose.service": "web",
                                        },
                                        "Env": [
                                            RUNTIME_IDENTITY_ENV_KEY
                                            + "="
                                            + json.dumps(
                                                fixture_identity().model_dump(mode="json")
                                            ),
                                            "ODOO_DB_NAME=sample",
                                        ],
                                    },
                                }
                            ]
                        ),
                        "DOCKER_ERROR": "1" if docker_error else "0",
                    },
                    text=True,
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode == 0, permitted is not None, result.stderr)
                self.assertEqual(
                    PREVIEW_FIXTURE_RESULT_MARKER + "=" in result.stdout, permitted is not None
                )
                if permitted is not None:
                    encoded = next(
                        line.partition("=")[2]
                        for line in result.stdout.splitlines()
                        if line.startswith(PREVIEW_FIXTURE_RESULT_MARKER + "=")
                    )
                    self.assertEqual(json.loads(bytes.fromhex(encoded))["stopped"], permitted)


class PreviewFixtureCarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(
            database_url="sqlite+pysqlite:///" + str(self.root / "state.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.now = "2026-10-30T12:00:00Z"
        self.enterContext(
            patch.object(
                self.store, "_database_mutation_timestamp", side_effect=lambda _session: self.now
            )
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.execute = Mock(return_value={"status": "pass", "provider_effect_attempted": True})
        self.quiescence = Mock(return_value=True)
        self.observe = Mock(return_value=("present", {"status": "pass"}, False))
        self.cancellation = self.enterContext(
            patch("control_plane.preview_fixture.require_fixture_container_cancelled")
        )
        for name in ("capture_source_decision", "capture_schedule_binding"):
            self.enterContext(
                patch(
                    "control_plane.odoo_preview_apply_execution."
                    f"OdooPreviewProviderMutationAdapter.{name}"
                )
            )
        self.enterContext(
            patch("control_plane.odoo_preview_apply_execution.require_source_free_preview_history")
        )
        self.enterContext(
            patch(
                "control_plane.odoo_preview_apply_execution.apply_odoo_preview_lifecycle_evidence",
                return_value={"lifecycle_evidence_status": "applied"},
            )
        )
        first = self.operation("first-destroy")
        runtime = fixture_identity().model_copy(
            update={
                "product": self.profile.product,
                "context": self.profile.preview.context,
                "instance": first.adapter._issued_plan.dry_run_plan.compose_name,
            }
        )
        self.fixture = PreviewFixtureIntent(
            product=self.profile.product,
            maintenance=fixture_request(mode="apply", reviewed_plan_sha256="c" * 64).model_copy(
                update={"context": runtime.context, "instance": runtime.instance}
            ),
            fixture_binding=fixture_binding().model_copy(update={"identity": runtime}),
        )

    def operation(
        self,
        key: str,
        *,
        action: str = "destroy",
        delete_volumes: bool = True,
        disposable_cleanup: bool = False,
    ) -> OdooPreviewApplyOperation:
        payload = _refresh_payload()
        plan = cast(dict[str, object], cast(dict[str, object], payload["apply"])["dry_run_plan"])
        plan.update(operation=action, compose_ref="compose-example", delete_volumes=delete_volumes)
        envelope = OdooPreviewApplyEnvelope.model_validate(payload)
        issued = issue_odoo_preview_apply_plan(result=_issued_plan(envelope), plan_id=key)
        return prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=envelope,
            issued_plan=issued,
            reservation_scope="destroyer",
            idempotency_key=key,
            request_fingerprint=key,
            trace_id=key,
            execute_apply=self.execute,
            observe_apply=self.observe,
            supersession_is_quiescent=self.quiescence,
            pre_mutation_guard=lambda: None,
            qualify_existing_preview=True,
            disposable_cleanup=disposable_cleanup,
        )

    def hold_carrier(self, intent: dict[str, object]) -> LaunchplaneIdempotencyRecord:
        operation = self.operation("first-destroy")
        reserved = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=operation.route_path,
            idempotency_key=operation.idempotency_key,
            request_fingerprint=operation.request_fingerprint,
            lease_owner="original",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=intent,
        )
        assert reserved.record is not None
        checkpoint = self.store.checkpoint_mutation_provider_effect(
            reservation=reserved.record, effect_phase=SUPERSEDED_PROVIDER_EFFECT_PHASE
        )
        checkpointed = checkpoint.record
        assert checkpointed is not None
        held = self.store.mark_mutation_reconcile_required(
            reservation=checkpointed,
            reconciliation_key=checkpointed.reconciliation_key,
        )
        held_record = held.record
        assert held_record is not None
        self.now = "2026-10-30T13:00:00Z"
        return held_record

    def carrier_intent(self) -> dict[str, object]:
        return {
            **self.operation("first-destroy").adapter.original_intent(),
            "fixture_cancellation": self.fixture.model_dump(mode="json"),
        }

    def test_different_key_destroy_chain_keeps_original_fixture_until_cancellation(self) -> None:
        original = self.hold_carrier(self.carrier_intent())
        second = self.operation("second-destroy")
        self.quiescence.return_value = False
        self.assertEqual(second.run(self.store).status, "target_busy")
        self.assertEqual(self.store.read_idempotency_record_by_id(original.record_id), original)
        self.cancellation.assert_not_called()
        self.quiescence.return_value = True
        self.cancellation.side_effect = ValueError("container unavailable")
        self.assertEqual(second.run(self.store).status, "reconcile_required")
        held = self.store.list_held_odoo_preview_reservations(product=self.profile.product)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].idempotency_key, second.idempotency_key)
        self.assertEqual(held[0].provider_effect_phase, SUPERSEDED_PROVIDER_EFFECT_PHASE)
        self.assertTrue(held[0].provider_effect_started_at)
        self.assertEqual(
            self.store.read_original_mutation_intent(held[0])["fixture_cancellation"],
            self.fixture.model_dump(mode="json"),
        )
        completed = self.store.read_idempotency_record_by_id(original.record_id)
        assert completed is not None
        self.assertEqual(completed.state, "completed")
        self.execute.assert_not_called()
        self.now = "2026-10-30T14:00:00Z"
        self.cancellation.side_effect = None
        self.assertEqual(self.operation("third-destroy").run(self.store).status, "completed")
        self.assertFalse(
            self.store.list_held_odoo_preview_reservations(product=self.profile.product)
        )
        for call in self.cancellation.call_args_list:
            self.assertEqual(call.kwargs["intent"], self.fixture)
        self.assertFalse(self.cancellation.call_args_list[-1].kwargs.get("cancel", False))

    def test_carrier_refuses_refresh_and_volume_preserving_cleanup(self) -> None:
        original = self.hold_carrier(self.carrier_intent())
        for action, delete_volumes, disposable in (
            ("refresh", True, False),
            ("destroy", False, False),
            ("destroy", False, True),
        ):
            with self.subTest(action=action, disposable=disposable):
                operation = self.operation(
                    "replacement",
                    action=action,
                    delete_volumes=delete_volumes,
                    disposable_cleanup=disposable,
                )
                self.assertEqual(operation.run(self.store).status, "target_busy")
                self.assertEqual(
                    self.store.read_idempotency_record_by_id(original.record_id), original
                )
        self.quiescence.assert_not_called()
        self.cancellation.assert_not_called()
        self.execute.assert_not_called()

    def test_malformed_carrier_cannot_be_replaced_or_observed_as_complete(self) -> None:
        original = self.hold_carrier(self.carrier_intent())
        for custody in (None, {"fixture_binding": None}):
            with self.subTest(custody=custody):
                intent = {**self.carrier_intent(), "fixture_cancellation": custody}
                with patch.object(self.store, "read_original_mutation_intent", return_value=intent):
                    for key in ("first-destroy", "replacement"):
                        with self.assertRaises(ValueError):
                            self.operation(key).run(
                                self.store, allow_mutation=key != "first-destroy"
                            )
                    self.assertEqual(
                        self.store.read_idempotency_record_by_id(original.record_id), original
                    )
        self.observe.assert_not_called()
        self.cancellation.assert_not_called()

    def test_missing_carrier_custody_never_observes_or_supersedes_known_effect(self) -> None:
        original = self.hold_carrier(self.operation("first-destroy").adapter.original_intent())
        for phase in (SUPERSEDED_PROVIDER_EFFECT_PHASE, PREVIEW_FIXTURE_CANCEL_PHASE):
            with self.subTest(phase=phase):
                observation = self.operation("first-destroy").adapter.observe("original", phase, "")
                self.assertEqual(observation.outcome, "unknown")
        for disposable in (False, True):
            with self.subTest(disposable=disposable):
                replacement = self.operation("replacement", disposable_cleanup=disposable)
                self.assertEqual(replacement.run(self.store).status, "target_busy")
        self.assertEqual(self.store.read_idempotency_record_by_id(original.record_id), original)
        self.observe.assert_not_called()
        self.quiescence.assert_not_called()

    def test_missing_carrier_original_cannot_enter_disposable_cleanup(self) -> None:
        original = self.hold_carrier({})
        replacement = self.operation("replacement", disposable_cleanup=True)
        self.assertEqual(replacement.run(self.store).status, "target_busy")
        self.assertEqual(self.store.read_idempotency_record_by_id(original.record_id), original)
        self.quiescence.assert_not_called()
        self.cancellation.assert_not_called()


class PreviewFixtureDestroyTests(unittest.TestCase):
    def test_only_guarded_destroy_takes_custody_and_requires_old_container_absence(self) -> None:
        for scenario in (
            "refresh",
            "preflight",
            "container_unknown",
            "orphan",
            "observation",
            "success",
        ):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
                )
                store.ensure_schema()
                self.addCleanup(store.close)
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _odoo_preview_profile_payload()
                )
                payload = _refresh_payload()
                apply = cast(dict[str, object], payload["apply"])
                plan = cast(dict[str, object], apply["dry_run_plan"])
                plan.update(
                    operation="refresh" if scenario == "refresh" else "destroy",
                    compose_ref="compose-example",
                    delete_volumes=True,
                )
                envelope = OdooPreviewApplyEnvelope.model_validate(payload)
                issued = _issued_plan(envelope)
                assert issued.plan_provenance is not None
                plan_id = issued.plan_provenance.plan_id
                runtime = fixture_identity().model_copy(
                    update={
                        "product": profile.product,
                        "context": profile.preview.context,
                        "instance": issued.dry_run_plan.compose_name,
                    }
                )
                binding = fixture_binding().model_copy(update={"identity": runtime})
                maintenance = fixture_request(
                    mode="apply", reviewed_plan_sha256="c" * 64
                ).model_copy(
                    update={
                        "context": runtime.context,
                        "instance": runtime.instance,
                    }
                )
                original = PreviewFixtureIntent(
                    product=profile.product,
                    maintenance=maintenance,
                    fixture_binding=binding,
                )
                clock = {"now": "2026-10-30T12:00:00Z"}
                refusal = {"active": scenario == "preflight"}
                orphan = {"running": True, "first": True}

                def guard() -> None:
                    current_reservation = store.read_idempotency_record(
                        scope="destroyer",
                        route_path="/v1/drivers/odoo/preview-apply",
                        idempotency_key=plan_id,
                    )
                    if refusal["active"] and current_reservation is not None:
                        raise ValueError("custody changed")

                def execute(**kwargs: object) -> dict[str, object]:
                    cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])(
                        "compose_destroy"
                    )
                    return {"status": "pass", "provider_effect_attempted": True}

                def fixture_container(**kwargs: object) -> None:
                    if scenario == "container_unknown":
                        raise ValueError("Docker unavailable")
                    if scenario not in {"orphan", "observation"}:
                        return
                    if kwargs.get("cancel"):
                        callback = cast(Callable[[str], None], kwargs["before_provider_mutation"])
                        callback("cancel")
                        if orphan["first"]:
                            orphan["first"] = False
                            raise RuntimeError("observer lost before stop")
                        orphan["running"] = False
                    elif orphan["running"]:
                        raise FixtureContainerStillRunning("live original container")

                operation = prepare_odoo_preview_apply_operation(
                    control_plane_root=Path(directory),
                    record_store=store,
                    profile=profile,
                    apply_request=envelope,
                    issued_plan=issued,
                    reservation_scope="destroyer",
                    idempotency_key=plan_id,
                    request_fingerprint="destroy",
                    trace_id="destroy-trace",
                    execute_apply=execute,
                    pre_mutation_guard=guard,
                    qualify_existing_preview=True,
                )
                with (
                    patch.object(
                        store,
                        "_database_mutation_timestamp",
                        side_effect=lambda _session: clock["now"],
                    ),
                    patch(
                        "control_plane.odoo_preview_apply_execution.require_source_free_preview_history"
                    ),
                    patch(
                        "control_plane.odoo_preview_apply_execution.OdooPreviewProviderMutationAdapter.capture_source_decision"
                    ),
                    patch(
                        "control_plane.odoo_preview_apply_execution.apply_odoo_preview_lifecycle_evidence",
                        return_value={"lifecycle_evidence_status": "applied"},
                    ),
                    patch(
                        "control_plane.preview_fixture.require_fixture_container_cancelled",
                        side_effect=fixture_container,
                    ) as absence,
                ):
                    stale = store.reserve_mutation(
                        scope="operator",
                        route_path="/v1/drivers/odoo/app-maintenance",
                        idempotency_key="fixture",
                        request_fingerprint="fixture",
                        lease_owner="old",
                        lease_seconds=60,
                        reconciliation_key=operation.adapter.reconciliation_key(),
                        provider_target_key=operation.adapter.target_key(),
                        original_intent=original.model_dump(mode="json"),
                    )
                    assert stale.record is not None
                    from control_plane.preview_reservation_recovery import (
                        classify_preview_reservation,
                    )

                    classified = classify_preview_reservation(
                        stale.record, profile=profile, store=store
                    )
                    self.assertTrue(classified["original_binding_available"])
                    self.assertEqual(classified["action"], maintenance.intent)
                    store.mark_mutation_reconcile_required(
                        reservation=stale.record, reconciliation_key=stale.record.reconciliation_key
                    )
                    self.assertEqual(
                        store.list_held_odoo_preview_reservations(product=profile.product)[
                            0
                        ].record_id,
                        stale.record.record_id,
                    )
                    clock["now"] = "2026-10-30T13:00:00Z"
                    if scenario in {"orphan", "observation"}:
                        with self.assertRaises(RuntimeError):
                            operation.run(store)
                        self.assertTrue(orphan["running"])
                        if scenario == "observation":
                            from control_plane.preview_reservation_recovery import (
                                recover_held_preview,
                            )

                            orphan["running"] = False
                            clock["now"] = "2026-10-30T14:00:00Z"
                            store.write_product_profile_record(profile)
                            held = store.read_idempotency_record(
                                scope="destroyer",
                                route_path=operation.route_path,
                                idempotency_key=plan_id,
                            )
                            assert held is not None
                            recovered = recover_held_preview(
                                store=store,
                                reservation=held,
                                profile=profile,
                                root=Path(directory),
                                observe=Mock(return_value=("present", {"status": "pass"}, False)),
                                quiescence=Mock(return_value=True),
                            )
                            self.assertEqual(recovered["settlement"], "adopted")
                            self.assertEqual(absence.call_count, 2)
                        else:
                            with (
                                patch(
                                    "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                                    return_value=("https://provider.invalid", "fixture-token"),
                                ),
                                patch(
                                    "control_plane.workflows.odoo_preview_runtime._compose_exists_by_id",
                                    return_value=False,
                                ),
                                patch(
                                    "control_plane.workflows.odoo_preview_runtime._destroyed_preview_absence_verified",
                                    return_value=True,
                                ),
                            ):
                                result = operation.run(store)
                            self.assertEqual(result.status, "completed")
                            self.assertFalse(orphan["running"])
                            self.assertEqual(absence.call_count, 4)
                    else:
                        result = operation.run(store)
                        self.assertEqual(
                            result.status,
                            "target_busy"
                            if scenario == "refresh"
                            else "reconcile_required"
                            if scenario in {"preflight", "container_unknown"}
                            else "completed",
                        )
                        self.assertEqual(
                            absence.call_count,
                            0
                            if scenario in {"refresh", "preflight"}
                            else 1
                            if scenario == "container_unknown"
                            else 2,
                        )
                    held_reservations = store.list_held_odoo_preview_reservations(
                        product=profile.product
                    )
                    self.assertEqual(
                        len(held_reservations),
                        0 if scenario in {"success", "orphan", "observation"} else 1,
                    )
                    if scenario == "preflight":
                        refusal["active"] = False
                        with (
                            patch(
                                "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                                return_value=("https://provider.invalid", "fixture-token"),
                            ),
                            patch(
                                "control_plane.workflows.odoo_preview_runtime._compose_exists_by_id",
                                return_value=True,
                            ),
                        ):
                            resumed = operation.run(store)
                        self.assertEqual(resumed.status, "completed")
                        self.assertFalse(
                            store.list_held_odoo_preview_reservations(product=profile.product)
                        )
                        self.assertEqual(absence.call_count, 2)
