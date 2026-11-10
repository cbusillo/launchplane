import io
import itertools
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, cast
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.preview_parts_import_programs import (
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
    ProviderOperationLease,
    ProviderMutationRejectedError,
)
from tests import test_preview_fixture as fixture_tests
from control_plane.odoo_source_probe import grant_classifier_program
from control_plane.contracts.native_operation_evidence import NATIVE_OPERATION_RESULT_MAX_BYTES
from control_plane.preview_fixture import PREVIEW_FIXTURE_RESULT_MARKER
from tests.test_preview_fixture import (
    fixture_identity,
    fixture_binding,
    fixture_request,
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
        for extra in ({"method": "private"}, {"instance": "testing"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                import_request(**extra)

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

        def lost_writer(*args: object, **_kwargs: object) -> PreviewPartsImportReceipt:
            lease = cast(ProviderOperationLease, args[1])
            lease.checkpoint_effect("triggered")
            raise TimeoutError("private writer output")

        with (
            patch.object(adapter, "plan", return_value=(receipt, import_plan_sha256(receipt))),
            patch.object(adapter, "invoke", side_effect=lost_writer),
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", cast(ProviderOperationLease, cast(object, Mock())))
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
                adapter.apply("key", cast(ProviderOperationLease, cast(object, Mock())))
            invoke.assert_not_called()

    def test_import_original_retains_binding_and_shares_existing_disposable_guards(self) -> None:
        with fixture_tests.PreviewFixtureLaneTests().preview() as (store, fixture, _slot):
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
        self,
        *,
        accepts_parts: bool = True,
        grants: str = "read_only",
        source_error: bool = False,
        writer_error: bool = False,
    ) -> Iterator[tuple[Any, dict[str, Any], list[str]]]:
        events: list[str] = []

        class Parameters:
            values = {"fishbowl.host": "example.invalid", "fishbowl.password": "private-value"}
            witness: dict[str, str] = {}

            def sudo(self) -> "Parameters":
                return self

            def get_str(self, key: str) -> str:
                return self.values.get(key, self.witness.get(key, ""))

            def set_str(self, key: str, value: str) -> None:
                events.append("witness")
                self.witness[key] = value

        parameters = Parameters()

        class Importer:
            def __init__(self) -> None:
                self.events = events

            def run_full_import(self) -> None:
                self.events.append("import")
                if writer_error:
                    raise RuntimeError("Interrupted after import started")

        class UnsupportedImporter:
            @staticmethod
            def run_full_import(required_argument: bool) -> None:
                if required_argument:
                    raise AssertionError("Unsupported invocation")

        importer = Importer() if accepts_parts else UnsupportedImporter()

        class Client:
            def __init__(self, settings: Any) -> None:
                del settings

            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            @staticmethod
            def fetch_all(query: str) -> list[dict[str, str]]:
                events.append(query)
                if source_error:
                    raise OSError("private source address")
                text = {
                    "read_only": "GRANT SELECT ON `source`.* TO 'user'@'host'",
                    "unsafe": "GRANT UPDATE ON `source`.* TO 'user'@'host'",
                    "unknown": "unrecognized role",
                }[grants]
                return [{"grants": text}]

        class Environment:
            cr = Mock()
            cr.execute.side_effect = lambda sql: events.append(
                "read-only" if sql == "SET TRANSACTION READ ONLY" else "unexpected-sql"
            )
            cr.rollback.side_effect = lambda: events.append("rollback")
            cr.commit.side_effect = lambda: events.append("commit")

            def __getitem__(self, key: str) -> Any:
                return parameters if key == "ir.config_parameter" else importer

            @staticmethod
            def invalidate_all() -> None:
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
        setattr(
            modules["odoo.addons.fishbowl_import.services.connection_settings"],
            "PARAMETER_KEYS",
            tuple(parameters.values),
        )
        setattr(
            modules["odoo.addons.fishbowl_import.services.fishbowl_client"],
            "FishbowlClient",
            Client,
        )
        modules[
            "odoo.addons.fishbowl_import.services.connection_settings"
        ].resolve_connection_settings = lambda values: types.SimpleNamespace()
        request = {
            **import_receipt().model_dump(mode="json"),
            "database": "sample",
            "operation": "c" * 64,
            "settings_sha256": canonical_json_sha256(parameters.values),
            "probe_program": grant_classifier_program(),
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
            self.assertEqual(events, ["read-only", "SHOW GRANTS"])
            request["mode"] = "apply"
            applied = run()
            PreviewPartsImportReceipt.model_validate(applied)
            self.assertEqual(
                events,
                [
                    "read-only",
                    "SHOW GRANTS",
                    "read-only",
                    "SHOW GRANTS",
                    "rollback",
                    "import",
                    "witness",
                    "commit",
                ],
            )
            request["mode"] = "readback"
            observed = run()
            self.assertTrue(observed["committed"])
            self.assertEqual(
                events,
                [
                    "read-only",
                    "SHOW GRANTS",
                    "read-only",
                    "SHOW GRANTS",
                    "rollback",
                    "import",
                    "witness",
                    "commit",
                    "read-only",
                ],
            )
            self.assertNotIn("private-value", json.dumps(observed))
            self.assertTrue(all(value is None for value in observed["counts"].values()))

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
            "maximum_result_bytes": NATIVE_OPERATION_RESULT_MAX_BYTES,
            "odoo_program": ODOO_IMPORT_PROGRAM,
        }
        order = []
        child.wait.side_effect = lambda: order.append("wait")
        runner.release_data_workflow_lock.side_effect = lambda: order.append("unlock")
        selector = Mock()
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock(return_value=None)
        selector.get_map.return_value = {"writer": True}
        clock = itertools.count(step=IMPORT_TIMEOUT_SECONDS + 1)

        def spawn(*_args: Any, **arguments: Any) -> Mock:
            script = arguments["stdin"].read()
            compile(script, "<native-odoo-stdin>", "exec")
            return child

        with (
            patch.dict(sys.modules, {"run_odoo_data_workflows": module}),
            patch("sys.stdin", io.StringIO(json.dumps(request))),
            patch.dict(
                "os.environ", {"LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(request["identity"])}
            ),
            patch("subprocess.Popen", side_effect=spawn),
            patch("selectors.DefaultSelector", return_value=selector),
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("os.killpg", side_effect=lambda *_args: order.append("kill")) as kill,
        ):
            with self.assertRaises(TimeoutError):
                exec(CONTAINER_IMPORT_PROGRAM, {})
        kill.assert_called_once()
        self.assertEqual(order, ["kill", "wait", "unlock"])

    def test_source_preflight_error_is_bounded_but_started_import_error_is_unknown(self) -> None:
        with self.native(source_error=True) as (run, request, events):
            request["mode"] = "apply"
            refused = run()
            self.assertEqual(refused["stage"], "grants")
            self.assertEqual(refused["outcome"], "unavailable")
            self.assertNotIn("import", events)
            self.assertNotIn("private source address", json.dumps(refused))
        with self.native(writer_error=True) as (run, request, events):
            request["mode"] = "apply"
            with self.assertRaises(RuntimeError):
                run()
            self.assertIn("import", events)
            self.assertNotIn("witness", events)


class ImportHostReceiptTests(unittest.TestCase):
    def test_host_rejects_business_values_before_emitting_evidence(self) -> None:
        from control_plane.preview_parts_import_programs import IMPORT_RECEIPT_VALIDATOR

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
        connection.evidence = OdooRuntimeEvidence(
            identity=binding.identity,
            target_id=binding.target_id,
            target_name=binding.target_name,
            container_id=binding.container_id,
            observed_at="2026-11-09T00:00:00Z",
        )
        connection.credentials = {"ODOO_DB_NAME": binding.database}
        connection.app_name = binding.app_name
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
            from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers

            return extract_odoo_post_deploy_readback_markers(
                {
                    "log": PREVIEW_FIXTURE_RESULT_MARKER
                    + "="
                    + receipt.model_dump_json().encode().hex()
                }
            )

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
                "control_plane.preview_fixture.post_deploy.run_manual_data_schedule",
                side_effect=schedule,
            ),
        ):
            self.assertEqual(adapter.invoke("dry-run"), receipt)


class ImportOriginalTests(unittest.TestCase):
    def test_source_preparation_generation_and_managed_authority_must_still_agree(self) -> None:
        from control_plane.odoo_instance_overrides import config_parameter_secret_env_key

        identity = fixture_identity()
        password_key = config_parameter_secret_env_key("fishbowl.password")
        store = Mock()
        preparation = types.SimpleNamespace(
            state="completed",
            response_payload={
                "records": {
                    "preview_id": identity.preview_id,
                    "generation_id": identity.preview_generation_id,
                }
            },
        )
        store.read_idempotency_record_by_id.return_value = preparation
        issued = types.SimpleNamespace(
            plan_request=types.SimpleNamespace(
                fishbowl_source=object(),
                source_git_ref=identity.source_git_ref,
                manifest=types.SimpleNamespace(artifact_id=identity.artifact_id),
            ),
            dry_run_plan=types.SimpleNamespace(compose_name=identity.instance),
            preview_slug="pr-42",
            fishbowl_source_sha256="a" * 64,
        )
        selected = types.SimpleNamespace(
            authority_sha256="a" * 64,
            environment={password_key: "synthetic-password-λ"},
            parameters=(
                types.SimpleNamespace(
                    key="fishbowl.password", value=types.SimpleNamespace(source="secret_binding")
                ),
            ),
        )
        connection = Mock()
        connection.selection.identity = identity
        adapter = PreviewPartsImportAdapter(
            store, Path("."), identity.product, import_request(), "trace"
        )
        with (
            patch(
                "control_plane.preview_parts_import.original_preview_intent",
                return_value=types.SimpleNamespace(issued_plan=issued),
            ),
            patch(
                "control_plane.preview_parts_import.resolve_fishbowl_preview_source",
                return_value=selected,
            ),
            patch(
                "control_plane.preview_parts_import.reads.inspect_runtime_container",
                return_value={password_key: "synthetic-password-λ"},
            ),
        ):
            authority, _ = adapter.qualified_source(connection)
            self.assertEqual(authority, selected.authority_sha256)
            for failure in ("preparation", "generation", "managed_source"):
                with self.subTest(failure=failure):
                    preparation.state = "running" if failure == "preparation" else "completed"
                    preparation.response_payload["records"]["generation_id"] = (
                        "changed" if failure == "generation" else identity.preview_generation_id
                    )
                    selected.authority_sha256 = (
                        "b" * 64 if failure == "managed_source" else "a" * 64
                    )
                    with self.assertRaises(ValueError):
                        adapter.qualified_source(connection)

    def test_import_original_fences_recovers_replays_and_retains_cancellation_binding(self) -> None:
        from control_plane.provider_operations import run_durable_provider_operation
        from control_plane.preview_reservation_recovery import classify_preview_reservation
        from control_plane.preview_fixture import require_fixture_container_cancelled

        with fixture_tests.PreviewFixtureLaneTests().preview() as (store, fixture, _slot):
            identity = fixture.selection().identity
            binding = fixture_binding().model_copy(update={"identity": identity})
            planned = import_receipt(identity=identity)
            request = import_request(
                instance=identity.instance,
                mode="apply",
                reviewed_plan_sha256=import_plan_sha256(planned),
            )
            intent = PreviewFixtureIntent(
                product=identity.product,
                maintenance=request,
                fixture_binding=binding,
                source_authority_sha256=planned.source_authority_sha256,
            )
            adapter = PreviewPartsImportAdapter(
                store, fixture.root, identity.product, request, "trace"
            )
            adapter.original_intent(intent.model_dump(mode="json"))
            writes = []

            def invoke(
                mode: str, lease: ProviderOperationLease | None = None, *, operation: str = ""
            ) -> PreviewPartsImportReceipt:
                if mode == "dry-run":
                    return planned
                if mode == "apply":
                    if lease is None:
                        raise AssertionError("Apply requires a lease")
                    lease.checkpoint_effect("import_triggered")
                    writes.append(operation)
                    raise TimeoutError("lost observer")
                return import_receipt(
                    identity=identity,
                    mode="readback",
                    operation=operation,
                    grants="read_only",
                    stage="complete",
                    outcome="committed",
                    committed=True,
                )

            def execute(key: str = "original-import") -> Any:
                return run_durable_provider_operation(
                    store=store,
                    scope="operator:example",
                    route_path="/v1/drivers/odoo/app-maintenance",
                    idempotency_key=key,
                    request_fingerprint="a" * 64,
                    lease_owner="trace",
                    response_trace_id="trace",
                    adapter=adapter,
                    original_intent=intent.model_dump(mode="json"),
                )

            with patch.object(adapter, "invoke", side_effect=invoke):
                first = execute()
                self.assertEqual(first.status, "reconcile_required")
                self.assertIsNotNone(first.record)
                self.assertEqual(execute("different-key").status, "target_busy")
                self.assertEqual(len(writes), 1)
                held = store.list_held_odoo_preview_reservations(product=identity.product)
                self.assertEqual([item.record_id for item in held], [first.record.record_id])
                classification = classify_preview_reservation(
                    first.record,
                    profile=store.read_product_profile_record(identity.product),
                    store=store,
                )
                self.assertEqual(classification["action"], request.action)
                recovered = execute()
                self.assertEqual(recovered.status, "adopted")
                self.assertTrue(recovered.response_payload["result"]["receipt"]["committed"])
                self.assertEqual(execute().status, "replayed")
                self.assertEqual(len(writes), 1)

            def refused(
                mode: str, lease: ProviderOperationLease | None = None, *, operation: str = ""
            ) -> PreviewPartsImportReceipt:
                if mode == "dry-run":
                    return planned
                if lease is None:
                    raise AssertionError("Apply requires a lease")
                lease.checkpoint_effect("import_triggered")
                return import_receipt(
                    identity=identity,
                    mode="apply",
                    operation=operation,
                    stage="grants",
                    outcome="unavailable",
                    grants="unsafe",
                )

            with patch.object(adapter, "invoke", side_effect=refused):
                result = execute("refused-import")
                self.assertEqual(result.status, "completed")
                self.assertEqual(
                    store.list_held_odoo_preview_reservations(product=identity.product), ()
                )
            with (
                patch(
                    "control_plane.preview_fixture.source.read_dokploy_config",
                    return_value=("https://provider.invalid", "synthetic-token"),
                ),
                patch(
                    "control_plane.preview_fixture.run_fixture_schedule",
                    return_value={
                        PREVIEW_FIXTURE_RESULT_MARKER: json.dumps(
                            {"container_id": binding.container_id, "stopped": True}
                        )
                        .encode()
                        .hex()
                    },
                ) as schedule,
            ):
                require_fixture_container_cancelled(store=store, root=fixture.root, intent=intent)
                self.assertEqual(schedule.call_args.kwargs["binding"], binding)
                self.assertEqual(schedule.call_args.kwargs["mode"], "cancel-readback")


class ImportHelperTests(unittest.TestCase):
    def test_uncommitted_apply_returns_nonzero_with_bounded_receipt(self) -> None:
        from tempfile import TemporaryDirectory
        import runpy

        client = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "preview-fixture.py"))
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "request.json"
            draft = import_request()
            payload.write_text(
                json.dumps(
                    {"product": "example-product", "maintenance": draft.model_dump(mode="json")}
                )
            )
            review = Path(directory) / "review.json"
            review.write_text(
                json.dumps(
                    {
                        "intent": {
                            "product": "example-product",
                            "maintenance": draft.model_dump(
                                exclude={"mode", "reviewed_plan_sha256"}
                            ),
                        },
                        "response": {
                            "result": {"plan_sha256": import_plan_sha256(import_receipt())}
                        },
                    }
                )
            )
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            receipt = import_receipt(
                mode="apply",
                operation="b" * 64,
                stage="grants",
                outcome="unavailable",
                grants="unsafe",
            )
            transport = {
                "resolve_settings": lambda _args: {
                    "service_url": "https://example.invalid",
                    "token": "synthetic-token",
                },
                "validate_service_url": lambda _url: None,
                "request_launchplane": lambda **_kwargs: {
                    "status": "accepted",
                    "trace_id": "trace",
                    "result": {"receipt": receipt.model_dump(mode="json")},
                },
                "public_trace_id": lambda value: value,
            }
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "preview-fixture.py",
                        "apply",
                        "--payload-file",
                        str(payload),
                        "--operator-helper",
                        str(helper),
                        "--evidence-file",
                        str(review),
                        "--reviewed-dry-run",
                        "--idempotency-key",
                        "original-key",
                    ],
                ),
                patch.object(runpy, "run_path", return_value=transport),
                patch.object(sys, "stdout", output),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertFalse(json.loads(output.getvalue())["receipt"]["committed"])
            self.assertNotIn("synthetic-token", output.getvalue())
