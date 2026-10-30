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
from typing import Iterator, cast
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.preview_fixture import PreviewFixtureReceipt, PreviewFixtureRequest
from control_plane.contracts.runtime_identity import RuntimeIdentity, RUNTIME_IDENTITY_ENV_KEY
from control_plane.host_package import host_package
from control_plane.odoo_app_maintenance_http import OdooAppMaintenanceEnvelope
from control_plane.preview_fixture import PreviewFixtureAdapter
from control_plane._preview_fixture_program import PREVIEW_FIXTURE_RESULT_MARKER
from control_plane.dokploy.post_deploy import OdooPostDeployReadbackFailure
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderOperationLease,
    ProviderMutationRejectedError,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.http_app import create_launchplane_fastapi_app
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


class PreviewFixtureTests(unittest.TestCase):
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
                adapter.apply("original", Mock())
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ["dry-run"])

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
                    fixture_request(mode="apply", reviewed_plan_sha256="c" * 64),
                    "trace",
                )
                with (
                    patch.object(PreviewFixtureAdapter, "plan", side_effect=error),
                    patch.object(PreviewFixtureAdapter, "invoke") as invoke,
                ):
                    with self.assertRaisesRegex(ValueError, "preflight"):
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
                invoke.assert_not_called()

    def test_absent_atomic_fixture_allows_original_retry_and_conflict_releases_cleanup(
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
                with self.assertRaises(OdooPostDeployReadbackFailure):
                    run()
                with self.assertRaisesRegex(ValueError, "preflight"):
                    run()
            self.assertEqual(calls, ["dry-run", "apply", "readback", "dry-run"])
            self.assertIsNone(
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
            ) -> PreviewFixtureReceipt:
                calls.append(mode)
                if mode == "apply":
                    assert lease is not None
                    lease.checkpoint_effect("fixture_started")
                    raise ValueError("Receipt lost after the write")
                return fixture_receipt(mode)

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
                with self.assertRaisesRegex(ValueError, "Receipt lost"):
                    run()
                second = run()
                third = run()
            self.assertEqual(second.status, "adopted")
            self.assertEqual(third.status, "replayed")
            self.assertEqual(calls, ["dry-run", "apply", "readback"])
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
                    mode: str, lease: ProviderOperationLease | None = None
                ) -> PreviewFixtureReceipt:
                    if lease is not None:
                        lease.checkpoint_effect("fixture_started")
                    return fixture_receipt(mode)

                with (
                    patch.object(PreviewFixtureAdapter, "plan", return_value=(planned, "c" * 64)),
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
                                mode=mode, reviewed_plan_sha256="c" * 64 if mode == "apply" else ""
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
        self, receipt: dict[str, object], *, stable: bool = False
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
            identity = fixture_identity().model_dump(mode="json")
            observed = dict(identity, environment_kind="stable") if stable else identity
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
                    "Image": identity["image_reference"],
                },
            }
            spec = {
                "identity": identity,
                "database": "sample",
                "fixture": "sample-parts",
                "mode": "apply",
                "container_id": "container-example",
                "app_name": "sample-app",
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
                check=False,
                timeout=15,
            )

    def test_packaged_host_accepts_bound_receipt_and_refuses_target_drift_and_private_output(
        self,
    ) -> None:
        good = fixture_receipt("apply").model_dump(mode="json")
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
