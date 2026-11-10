import io
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane._preview_parts_import_program import (
    ODOO_IMPORT_PROGRAM,
    CONTAINER_IMPORT_PROGRAM,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.preview_fixture import PreviewPartsImportRequest, PreviewFixtureIntent
from control_plane.contracts.preview_parts_import import PreviewPartsImportReceipt
from control_plane.odoo_app_maintenance_http import OdooAppMaintenanceEnvelope
from control_plane.preview_parts_import import PreviewPartsImportAdapter, import_plan_sha256
from control_plane.provider_operations import (
    ProviderMutationUnknownError,
    ProviderMutationRejectedError,
)
from tests.test_preview_fixture import (
    fixture_identity,
    fixture_binding,
    fixture_request,
    PreviewFixtureLaneTests,
)


def import_request(**updates: object) -> PreviewPartsImportRequest:
    value = fixture_request().model_dump(exclude={"fixture"})
    return PreviewPartsImportRequest.model_validate(
        {**value, "action": "preview-parts-import", "intent": "preview-parts-import", **updates}
    )


def import_receipt(**updates: object) -> PreviewPartsImportReceipt:
    return PreviewPartsImportReceipt.model_validate(
        {
            "identity": fixture_identity(),
            "source_authority_sha256": "a" * 64,
            "operation": "",
            "mode": "dry-run",
            "grants": "read_only",
            "stage": "qualified",
            "outcome": "qualified",
            "elapsed_seconds": 0.0,
            **updates,
        }
    )


class PreviewPartsImportTests(unittest.TestCase):
    def test_fixed_request_refuses_code_connections_and_stable_targets(self) -> None:
        envelope = OdooAppMaintenanceEnvelope.model_validate(
            {"product": "example-product", "maintenance": import_request().model_dump()}
        )
        self.assertIsInstance(envelope.maintenance, PreviewPartsImportRequest)
        for extra in (
            {"method": "private"},
            {"connection": "x"},
            {"parts_only": False},
            {"timeout_seconds": 601},
            {"fixture": "fake"},
            {"instance": "testing"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                import_request(**extra)

    def test_unknown_counts_remain_unavailable_and_wire_values_are_closed(self) -> None:
        receipt = import_receipt()
        self.assertIsNone(receipt.counts.parts)
        self.assertIsNone(receipt.counts.conflicts)
        with self.assertRaises(ValidationError):
            import_receipt(stage="private-source-host")
        with self.assertRaises(ValidationError):
            import_receipt(committed=True)

    def test_review_binds_source_generation_and_limit_but_not_observation_time(self) -> None:
        receipt = import_receipt()
        self.assertEqual(
            import_plan_sha256(receipt),
            import_plan_sha256(receipt.model_copy(update={"elapsed_seconds": 3.0})),
        )
        self.assertNotEqual(
            import_plan_sha256(receipt),
            import_plan_sha256(receipt.model_copy(update={"source_authority_sha256": "b" * 64})),
        )
        changed = receipt.identity.model_copy(update={"preview_generation_id": "next-generation"})
        self.assertNotEqual(
            import_plan_sha256(receipt),
            import_plan_sha256(receipt.model_copy(update={"identity": changed})),
        )

    def test_unknown_writer_requires_original_witness_and_never_retries(self) -> None:
        receipt = import_receipt()
        adapter = PreviewPartsImportAdapter(
            Mock(),
            Path("."),
            "example-product",
            import_request(mode="apply", reviewed_plan_sha256=import_plan_sha256(receipt)),
            "trace",
        )

        def lost_writer(*args: object, **kwargs: object) -> PreviewPartsImportReceipt:
            adapter.effect_started = True
            raise TimeoutError("private writer output")

        with (
            patch.object(adapter, "plan", return_value=(receipt, import_plan_sha256(receipt))),
            patch.object(adapter, "invoke", side_effect=lost_writer),
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", Mock())
        with patch.object(
            adapter, "invoke", return_value=import_receipt(mode="readback", outcome="unknown")
        ) as invoke:
            observation = adapter.observe("original-key", "triggered", adapter.reconciliation_key())
            self.assertEqual(observation.outcome, "unknown")
            self.assertFalse(observation.retry_safe)
            invoke.assert_called_once()
        with patch.object(adapter, "invoke") as invoke:
            observation = adapter.observe("original-key", "", adapter.reconciliation_key())
            self.assertEqual(observation.outcome, "absent")
            self.assertTrue(observation.retry_safe)
            invoke.assert_not_called()

    def test_preflight_refusal_never_dispatches_import(self) -> None:
        receipt = import_receipt(outcome="unavailable", stage="entrypoint", grants="unknown")
        adapter = PreviewPartsImportAdapter(
            Mock(),
            Path("."),
            "example-product",
            import_request(mode="apply", reviewed_plan_sha256=import_plan_sha256(receipt)),
            "trace",
        )
        with (
            patch.object(adapter, "plan", return_value=(receipt, import_plan_sha256(receipt))),
            patch.object(adapter, "invoke") as invoke,
        ):
            with self.assertRaises(ProviderMutationRejectedError):
                adapter.apply("key", Mock())
            invoke.assert_not_called()

    def test_import_original_retains_binding_and_shares_existing_disposable_guards(self) -> None:
        with PreviewFixtureLaneTests().preview() as (store, fixture, _slot):
            request = import_request(
                instance=fixture.request.instance, mode="apply", reviewed_plan_sha256="a" * 64
            )
            adapter = PreviewPartsImportAdapter(
                store, fixture.root, fixture.product, request, "trace"
            )
            self.assertEqual(adapter.selection(mutation=True), fixture.selection(mutation=True))
            intent = PreviewFixtureIntent(
                product=fixture.product,
                maintenance=request,
                fixture_binding=fixture_binding().model_copy(
                    update={"identity": adapter.selection().identity}
                ),
                source_authority_sha256="b" * 64,
            )
            self.assertEqual(
                adapter.original_intent(intent.model_dump(mode="json")),
                intent.model_dump(mode="json"),
            )
            changed = request.model_copy(update={"generation_id": "other"})
            other = PreviewPartsImportAdapter(
                store, fixture.root, fixture.product, changed, "trace"
            )
            with self.assertRaises(ValueError):
                other.selection(mutation=True)


class NativeImportTests(unittest.TestCase):
    @contextmanager
    def native(
        self, *, accepts_parts: bool = True, grants: str = "read_only"
    ) -> Iterator[tuple[Any, dict[str, Any], list[str]]]:
        events: list[str] = []

        class Parameters:
            values = {"fishbowl.host": "example.invalid", "fishbowl.password": "private-value"}
            witness: dict[str, str] = {}

            def sudo(self) -> "Parameters":
                return self

            def get_str(self, key: str) -> str:
                return self.values[key]

            def get_param(self, key: str) -> str | None:
                return self.witness.get(key)

            def set_param(self, key: str, value: str) -> None:
                events.append("witness")
                self.witness[key] = value

        parameters = Parameters()

        class Importer:
            def _get_fishbowl_settings(self) -> Any:
                return types.SimpleNamespace(use_ssl=True)

            def run_full_import(self, *, parts_only: bool) -> None:
                if parts_only is not True:
                    raise ValueError()
                events.append("import")

        class LegacyImporter:
            def _get_fishbowl_settings(self) -> Any:
                return types.SimpleNamespace(use_ssl=True)

            def run_full_import(self) -> None:
                events.append("legacy-import")

        importer = Importer() if accepts_parts else LegacyImporter()

        class Client:
            def __init__(self, settings: Any) -> None:
                del settings

            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def fetch_all(self, query: str) -> list[dict[str, str]]:
                events.append(query)
                return [{"grants": "private-grant-text"}]

        class Environment:
            cr = Mock()

            def __getitem__(self, key: str) -> Any:
                return parameters if key == "ir.config_parameter" else importer

            def invalidate_all(self) -> None:
                return None

        modules: dict[str, Any] = {}
        for name in (
            "odoo",
            "odoo.addons",
            "odoo.addons.fishbowl_import",
            "odoo.addons.fishbowl_import.services",
            "odoo.addons.fishbowl_import.services.connection_settings",
            "odoo.addons.fishbowl_import.services.fishbowl_client",
        ):
            modules[name] = types.ModuleType(name)
        modules["odoo.addons.fishbowl_import.services.connection_settings"].PARAMETER_KEYS = tuple(
            parameters.values
        )
        modules["odoo.addons.fishbowl_import.services.fishbowl_client"].FishbowlClient = Client
        request = {
            **import_receipt().model_dump(mode="json"),
            "database": "sample",
            "operation": "c" * 64,
            "settings_sha256": canonical_json_sha256(parameters.values),
            "probe_program": f"def classify_grants(rows):\n    return {grants!r}\n",
        }
        namespace: dict[str, Any] = {}
        exec(ODOO_IMPORT_PROGRAM, namespace)
        with patch.dict(sys.modules, modules):
            yield lambda: namespace["execute_import"](Environment(), request), request, events

    def test_grants_precede_fixed_import_and_readback_uses_commit_witness_without_write(
        self,
    ) -> None:
        with self.native() as (run, request, events):
            planned = run()
            self.assertEqual(planned["outcome"], "qualified")
            self.assertEqual(events, ["SHOW GRANTS"])
            request["mode"] = "apply"
            applied = run()
            PreviewPartsImportReceipt.model_validate(applied)
            self.assertEqual(events, ["SHOW GRANTS", "SHOW GRANTS", "import", "witness"])
            request["mode"] = "readback"
            observed = run()
            self.assertTrue(observed["committed"])
            self.assertEqual(events, ["SHOW GRANTS", "SHOW GRANTS", "import", "witness"])
            self.assertNotIn("private-value", json.dumps(observed))

    def test_unsupported_entrypoint_unsafe_grants_and_source_drift_never_import(self) -> None:
        for accepts, grants, drift in (
            (False, "read_only", False),
            (True, "unsafe", False),
            (True, "unknown", False),
            (True, "read_only", True),
        ):
            with (
                self.subTest(accepts=accepts, grants=grants, drift=drift),
                self.native(accepts_parts=accepts, grants=grants) as (run, request, events),
            ):
                request["mode"] = "apply"
                if drift:
                    request["settings_sha256"] = "d" * 64
                result = run()
                self.assertEqual(result["outcome"], "unavailable")
                self.assertNotIn("import", events)
                self.assertNotIn("witness", events)

    def test_native_deadline_kills_writer_and_waits_before_releasing_lock(self) -> None:
        from control_plane.contracts.preview_parts_import import IMPORT_TIMEOUT_SECONDS

        module = types.ModuleType("run_odoo_data_workflows")
        child = Mock()
        child.stdin = io.BytesIO()
        child.stdout = io.BytesIO()
        runner = Mock()
        runner._resolve_addons_paths.return_value = []
        runner._odoo_shell_command.return_value = ["fixed-odoo-shell"]
        module.__dict__["LocalServerSettings"] = lambda: types.SimpleNamespace(
            host="database", platform_instance="preview", db_name="sample"
        )
        module.__dict__["OdooDataWorkflowRunner"] = lambda *args, **kwargs: runner
        request = {
            "identity": fixture_identity().model_dump(mode="json"),
            "database": "sample",
            "mode": "apply",
            "timeout_seconds": IMPORT_TIMEOUT_SECONDS,
            "odoo_program": "fixed",
        }
        order = []
        child.wait.side_effect = lambda: order.append("wait")
        runner.release_data_workflow_lock.side_effect = lambda: order.append("unlock")
        selector = Mock()
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock(return_value=None)
        selector.get_map.return_value = {"writer": True}
        with (
            patch.dict(sys.modules, {"run_odoo_data_workflows": module}),
            patch("sys.stdin", io.StringIO(json.dumps(request))),
            patch.dict(
                "os.environ", {"LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(request["identity"])}
            ),
            patch("subprocess.Popen", return_value=child),
            patch("selectors.DefaultSelector", return_value=selector),
            patch("time.monotonic", side_effect=[0, IMPORT_TIMEOUT_SECONDS + 1]),
            patch("os.killpg") as kill,
        ):
            with self.assertRaises(TimeoutError):
                exec(CONTAINER_IMPORT_PROGRAM, {})
        kill.assert_called_once()
        self.assertEqual(order, ["wait", "unlock"])


class ImportHostReceiptTests(unittest.TestCase):
    def test_host_rejects_business_values_before_emitting_evidence(self) -> None:
        from control_plane._preview_parts_import_program import IMPORT_RECEIPT_VALIDATOR

        namespace: dict[str, Any] = {}
        exec(IMPORT_RECEIPT_VALIDATOR, namespace)
        value = import_receipt().model_dump(mode="json")
        validate = namespace["validate_receipt"]
        validate(value, value)
        for changes in (
            {"private": "customer data"},
            {"stage": "private server text"},
            {"counts": {"parts": "private supplier"}},
            {"elapsed_seconds": float("nan")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**value, **changes}, value)


class PreviewPartsImportHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_preview_authority_selects_import_adapter_and_requires_key(self) -> None:
        from tempfile import TemporaryDirectory
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
        from tests.support.auth import StubVerifier, identity
        from tests.support.http import request

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url="sqlite+pysqlite:///" + str(Path(directory) / "state.db")
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"schema_version": 2}),
                record_store_factory=lambda: store,
                control_plane_root_path=Path(directory),
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="operator-example",
                    local_operator_subject="director-example",
                    local_operator_token_label="operator-example",
                ),
            )
            planned = import_receipt()
            with patch.object(
                PreviewPartsImportAdapter,
                "plan",
                return_value=(planned, import_plan_sha256(planned)),
            ) as plan:
                response = await request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/app-maintenance",
                    headers={"Authorization": "Bearer operator-example"},
                    payload={
                        "product": "example-product",
                        "maintenance": import_request().model_dump(mode="json"),
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["result"]["receipt"]["parts_only"], True)
                plan.assert_called_once()
                response = await request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/app-maintenance",
                    headers={"Authorization": "Bearer operator-example"},
                    payload={
                        "product": "example-product",
                        "maintenance": import_request(
                            mode="apply", reviewed_plan_sha256=import_plan_sha256(planned)
                        ).model_dump(mode="json"),
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")


class ImportScheduleTests(unittest.TestCase):
    def test_selected_runtime_reaches_real_host_packaging_and_validates_receipt(self) -> None:
        from control_plane.odoo_runtime_reads import (
            OdooRuntimeConnection,
            OdooRuntimeEvidence,
            OdooRuntimeSelection,
        )
        from control_plane.host_package import host_invocation

        binding = fixture_binding()
        connection = Mock(spec=OdooRuntimeConnection)
        connection.selection = Mock(spec=OdooRuntimeSelection)
        connection.selection.identity = binding.identity
        connection.evidence = Mock(spec=OdooRuntimeEvidence)
        connection.host = "https://provider.invalid"
        connection.token = "private-token"
        adapter = PreviewPartsImportAdapter(
            Mock(), Path("."), "example-product", import_request(), "trace", binding=binding
        )
        receipt = import_receipt()

        def schedule(**kwargs: Any) -> dict[str, str]:
            invocation = host_invocation(kwargs["schedule_payload"]["script"])
            self.assertIsNotNone(invocation)
            kwargs["before_provider_mutation"]("schedule_setup")
            return {"preview_parts_import_result": receipt.model_dump_json().encode().hex()}

        with (
            patch.object(adapter, "connect", return_value=connection),
            patch.object(adapter, "runtime_binding", return_value=binding),
            patch.object(
                adapter,
                "qualified_source",
                return_value=(receipt.source_authority_sha256, "c" * 64),
            ),
            patch("control_plane.preview_parts_import.reads.assert_selection_current"),
            patch(
                "control_plane.preview_parts_import.post_deploy.run_manual_data_schedule",
                side_effect=schedule,
            ),
        ):
            self.assertEqual(adapter.invoke("dry-run"), receipt)
