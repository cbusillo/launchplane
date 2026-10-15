import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException

from control_plane import odoo_import_diagnostics as diagnostic
from control_plane import odoo_runtime_reads as reads
from control_plane import odoo_source_probe as probe
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetPolicies
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord, ScalarValue
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.http_routes.odoo_runtime import (
    OdooRuntimeReadDependencies,
    register_odoo_runtime_read_routes,
)
from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from tests.support.auth import _identity
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane

STAMP = "2026-10-15T12:00:00Z"
PRIVATE = "fixture-password-never-output"


class DiagnosticFixture:
    def __init__(self) -> None:
        self.store = MagicMock()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        )
        lane = next(lane for lane in self.profile.lanes if lane.instance == "testing")
        self.context = lane.context
        self.identity = RuntimeIdentity(
            product=self.profile.product,
            context=self.context,
            instance="testing",
            environment_kind="stable",
            deployment_record_id="fixture-deployment",
            artifact_id="fixture-artifact",
            source_git_ref="a" * 40,
            image_reference="ghcr.io/example/site@sha256:" + "b" * 64,
        )
        self.store.read_product_profile_record.return_value = self.profile
        self.store.list_product_profile_records.return_value = (self.profile,)
        self.inventory = EnvironmentInventory(
            deploy=DeploymentEvidence(
                status="pass",
                deploy_mode="dokploy-compose-api",
                target_name="fixture-target",
                target_type="compose",
            ),
            context=self.context,
            instance="testing",
            source_git_ref=self.identity.source_git_ref,
            deployment_record_id=self.identity.deployment_record_id,
            runtime_identity=self.identity,
            updated_at=STAMP,
        )
        self.store.read_environment_inventory.return_value = self.inventory
        target = DokployTargetRecord(
            context=self.context,
            instance="testing",
            target_name="fixture-target",
            policies=DokployTargetPolicies.model_validate(
                {
                    "integration_allowances": [
                        {
                            "integration": name,
                            "kind": "read_only_source",
                            "reason": "fixture",
                            "evidence": "fixture",
                        }
                        for name in diagnostic.SOURCE_PREFIXES
                    ]
                }
            ),
            updated_at=STAMP,
        )
        self.store.read_dokploy_target_record.return_value = target
        self.store.read_dokploy_target_id_record.return_value = DokployTargetIdRecord(
            context=self.context,
            instance="testing",
            target_id="fixture-target-id",
            updated_at=STAMP,
        )
        self.records = []
        self.bindings = []
        self.versions = {}
        self.applied = {}
        runtime_values: dict[str, ScalarValue] = {}
        for name, prefix in diagnostic.SOURCE_PREFIXES.items():
            key = config_parameter_secret_env_key(prefix + ".password")
            self.records.append(
                SecretRecord(
                    secret_id=name,
                    scope="context_instance",
                    integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name=name,
                    context=self.context,
                    instance="testing",
                    current_version_id=name + "-v1",
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            self.bindings.append(
                SecretBinding(
                    binding_id=name,
                    secret_id=name,
                    integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    binding_key=key,
                    context=self.context,
                    instance="testing",
                    declared_secret_class="testing",
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            self.versions[name + "-v1"] = SecretVersion(
                version_id=name + "-v1",
                secret_id=name,
                created_at=STAMP,
                ciphertext="fixture-ciphertext",
            )
            for field_name, value in {
                "host": name + ".invalid",
                "user": "fixture_reader",
                "db": "fixture_db",
                "port": "3306",
                "ssl_verify": "True",
            }.items():
                runtime_values[config_parameter_runtime_key(prefix + "." + field_name)] = value
                self.applied[prefix + "." + field_name] = value
            self.applied[prefix + ".password"] = PRIVATE
        self.store.list_secret_records.side_effect = lambda **kwargs: tuple(self.records)
        self.store.list_secret_bindings.side_effect = lambda **kwargs: tuple(self.bindings)
        self.store.read_secret_version.side_effect = lambda version: self.versions[version]
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
            context=self.context,
            instance="testing",
            env=runtime_values,
            updated_at=STAMP,
        )
        self.store.list_runtime_environment_records.side_effect = lambda **kwargs: (self.runtime,)
        self.connection = reads.OdooRuntimeConnection(
            selection=reads.OdooRuntimeSelection(self.identity, lane.base_url),
            evidence=reads.OdooRuntimeEvidence(
                identity=self.identity,
                target_id="fixture-target-id",
                target_name="fixture-target",
                container_id="fixture-container",
                observed_at=STAMP,
            ),
            host="https://provider.invalid",
            token="fixture-provider-token",
            server_id="fixture-server",
            app_name="fixture-app",
            credentials={
                "ODOO_DB_NAME": "fixture_db",
                "ODOO_ADMIN_LOGIN": "fixture_admin",
                "ODOO_ADMIN_PASSWORD": PRIVATE,
            },
        )


class DiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.patches = [
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(reads, "inspect_runtime_container", return_value={}),
            patch.object(
                diagnostic,
                "read_applied_parameters",
                side_effect=lambda _: dict(self.fixture.applied),
            ),
            patch.object(diagnostic, "_decrypt_secret_value", return_value=PRIVATE),
            patch.object(diagnostic, "managed_grants", return_value="read_only"),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_diagnostic(self) -> diagnostic.ImportDiagnosticResult:
        return diagnostic.diagnose_imports(
            store=self.fixture.store,
            product=self.fixture.profile.product,
            control_plane_root=Path("fixture"),
            database_url=None,
        )

    def test_agreement_is_value_free_and_not_runtime_grant_proof(self) -> None:
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = self.run_diagnostic()
        self.assertEqual(result.runtime, "verified")
        for source in result.sources:
            self.assertEqual(source.binding, "selected")
            self.assertTrue(all(item.agreement == "match" for item in source.parameters))
            self.assertEqual(source.managed_connection_grants, "read_only")
            self.assertEqual(source.applied_connection_grants, "unknown")
        for private in (PRIVATE, "fixture_reader", ".invalid", "fixture-ciphertext"):
            self.assertNotIn(
                private, result.model_dump_json() + stdout.getvalue() + stderr.getvalue()
            )
        self.assertFalse(result.applied)
        writes = [
            call for call in self.fixture.store.mock_calls if str(call).startswith("call.write_")
        ]
        self.assertEqual(writes, [])

    def test_applied_writer_mismatch_does_not_authenticate_as_writer(self) -> None:
        self.fixture.applied["cm_data.db.user"] = "old_writer"
        self.fixture.applied["cm_data.db.password"] = "old_writer_password"
        result = self.run_diagnostic()
        fields = {item.field: item.agreement for item in result.sources[0].parameters}
        self.assertEqual(fields["user"], "mismatch")
        self.assertEqual(fields["password"], "mismatch")
        settings = self.mocks[-1].call_args_list[0].args[0]
        self.assertEqual(settings["password"], PRIVATE)
        self.assertEqual(settings["user"], "fixture_reader")

    def test_missing_unsafe_and_unreachable_are_independent(self) -> None:
        self.fixture.bindings[0] = self.fixture.bindings[0].model_copy(
            update={"declared_secret_class": "prod"}
        )
        self.fixture.records[:] = [
            record for record in self.fixture.records if record.secret_id != "repairshopr"
        ]
        self.mocks[-1].return_value = "unknown"
        result = self.run_diagnostic()
        self.assertEqual(
            [source.binding for source in result.sources],
            ["missing_or_unsafe", "missing_or_unsafe", "selected"],
        )
        self.assertEqual(result.sources[-1].managed_connection_grants, "unknown")
        self.mocks[-2].assert_called_once()
        self.mocks[-1].assert_called_once()

    def test_source_failure_cannot_hide_other_source(self) -> None:
        self.mocks[-2].side_effect = [ValueError(PRIVATE), PRIVATE, PRIVATE]
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].binding, "missing_or_unsafe")
        self.assertEqual(result.sources[1].managed_connection_grants, "read_only")
        self.assertNotIn(PRIVATE, result.model_dump_json())

    def test_runtime_and_config_drift_discard_evidence(self) -> None:
        for kind in ("runtime", "settings", "binding", "container", "applied"):
            with self.subTest(kind=kind):
                fixture = DiagnosticFixture()
                self.fixture = fixture

                def drift(settings: dict[str, str]) -> str:
                    if kind == "runtime":
                        fixture.store.read_environment_inventory.return_value = (
                            fixture.inventory.model_copy(update={"deployment_record_id": "changed"})
                        )
                    elif kind == "settings":
                        fixture.runtime = fixture.runtime.model_copy(
                            update={"env": {"OTHER": "changed"}}
                        )
                    elif kind == "binding":
                        fixture.bindings.clear()
                    elif kind == "applied":
                        fixture.applied["fishbowl.password"] = "changed-fixture-password"
                    else:
                        self.mocks[1].side_effect = ValueError(PRIVATE)
                    return "read_only"

                self.mocks[-1].side_effect = drift
                result = self.run_diagnostic()
                self.assertEqual(result.runtime, "unknown")
                self.assertTrue(
                    all(source.managed_connection_grants == "unknown" for source in result.sources)
                )
                self.mocks[1].side_effect = None

    def test_duplicate_binding_missing_version_and_missing_parameter(self) -> None:
        self.fixture.bindings.append(
            self.fixture.bindings[0].model_copy(update={"binding_id": "duplicate"})
        )
        self.fixture.versions["repairshopr-v1"] = self.fixture.versions[
            "repairshopr-v1"
        ].model_copy(update={"secret_id": "foreign"})
        del self.fixture.applied["fishbowl.password"]
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].binding, "missing_or_unsafe")
        self.assertEqual(result.sources[1].binding, "missing_or_unsafe")
        self.assertEqual(result.sources[2].parameters[-1].agreement, "missing")

    def test_http_provider_and_decrypt_errors_do_not_escape(self) -> None:
        for mocked in self.mocks[:4]:
            with self.subTest(mocked=mocked):
                old = mocked.side_effect
                mocked.side_effect = RuntimeError(PRIVATE)
                with (
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    result = self.run_diagnostic()
                self.assertNotIn(
                    PRIVATE, result.model_dump_json() + stdout.getvalue() + stderr.getvalue()
                )
                mocked.side_effect = old


class AppliedReadTests(unittest.TestCase):
    def test_fixed_read_discards_values_and_rechecks_health(self) -> None:
        fixture = DiagnosticFixture()
        calls: list[tuple[str, dict[str, object]]] = []

        def rpc(
            opener: object, origin: str, path: str, params: dict[str, Any], **kwargs: object
        ) -> object:
            calls.append((path, params))
            if path.endswith("authenticate"):
                return {"uid": 7}
            if path.endswith("destroy"):
                return None
            self.assertEqual(params["model"], "ir.config_parameter")
            self.assertEqual(params["method"], "search_read")
            self.assertEqual(params["kwargs"]["fields"], ["key", "value"])
            return [{"key": key, "value": value} for key, value in fixture.applied.items()]

        with (
            patch.object(diagnostic, "odoo_compose_has_domain", return_value=True),
            patch.object(
                reads,
                "_http_json",
                return_value={"runtime_identity": fixture.identity.model_dump()},
            ) as health,
            patch.object(reads, "_rpc", side_effect=rpc),
        ):
            self.assertEqual(
                diagnostic.read_applied_parameters(fixture.connection), fixture.applied
            )
        self.assertEqual(health.call_count, 2)
        self.assertTrue(calls[-1][0].endswith("destroy"))


class GrantTests(unittest.TestCase):
    def test_privilege_categories_roles_and_grant_options_fail_closed(self) -> None:
        for privileges, expected in (
            ("SELECT, SHOW VIEW", "read_only"),
            ("SELECT, INSERT", "unsafe"),
            ("ALL PRIVILEGES", "unsafe"),
            ("EXECUTE", "unsafe"),
            ("FILE", "unsafe"),
            ("PROXY", "unsafe"),
            ("SELECT, CREATE USER", "unsafe"),
        ):
            with self.subTest(privileges=privileges):
                self.assertEqual(
                    probe.classify_grants([(f"GRANT {privileges} ON *.* TO 'fixture'@'%'",)]),
                    expected,
                )
        self.assertEqual(
            probe.classify_grants([("GRANT SELECT ON *.* TO 'fixture'@'%' WITH GRANT OPTION",)]),
            "unsafe",
        )
        self.assertEqual(
            probe.classify_grants(
                [
                    ("GRANT SELECT ON *.* TO 'fixture'@'%'",),
                    ("GRANT `writer_role`@`%` TO `fixture`@`%`",),
                ]
            ),
            "unknown",
        )
        for rows in (
            [],
            [("GRANT USAGE ON *.* TO 'fixture'@'%'",)],
            [("REVOKE SELECT ON `private`.* FROM 'fixture'@'%'",)],
            [("unexpected " + PRIVATE,)],
            [("x", "y")],
        ):
            self.assertEqual(probe.classify_grants(rows), "unknown")

    def test_probe_executes_metadata_only_and_closes(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchmany.return_value = [("GRANT SELECT ON *.* TO 'fixture'@'%'",)]
        with patch(
            "control_plane.odoo_source_probe.pymysql.connect", return_value=connection
        ) as connect:
            self.assertEqual(
                probe.probe({"host": "source.invalid", "user": "reader", "password": PRIVATE}),
                "read_only",
            )
        self.assertEqual(cursor.execute.call_args_list[0].args, ("SHOW GRANTS",))
        self.assertTrue(
            all(
                call.args[0].startswith(("SELECT ", "SHOW "))
                for call in cursor.execute.call_args_list
            )
        )
        self.assertFalse(connect.call_args.kwargs["local_infile"])
        self.assertTrue(connect.call_args.kwargs["ssl"].check_hostname)
        connection.close.assert_called_once()

    def test_child_masks_auth_dns_and_driver_failures(self) -> None:
        for error in (OSError(PRIVATE), ValueError(PRIVATE), RuntimeError(PRIVATE)):
            with (
                patch.object(probe, "probe", side_effect=error),
                patch(
                    "control_plane.odoo_source_probe.sys.stdin",
                    io.StringIO(
                        json.dumps(
                            {"host": "source.invalid", "user": "reader", "password": PRIVATE}
                        )
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                probe.main()
            self.assertEqual(json.loads(stdout.getvalue()), {"grants": "unknown"})
            self.assertEqual(stderr.getvalue(), "")

    def test_parent_timeout_and_malformed_response_never_leak(self) -> None:
        settings = {"host": "source.invalid", "user": "reader", "password": PRIVATE}
        for value in (
            subprocess.TimeoutExpired("probe", 10, output=PRIVATE),
            OSError(PRIVATE),
            subprocess.CompletedProcess([], 0, stdout=PRIVATE),
            subprocess.CompletedProcess(
                [], 0, stdout='{"grants":"read_only","secret":"' + PRIVATE + '"}'
            ),
        ):
            kwargs = (
                {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            )
            with patch("control_plane.odoo_import_diagnostics.subprocess.run", **kwargs) as run:
                self.assertEqual(diagnostic.managed_grants(settings), "unknown")
            self.assertNotIn(PRIVATE, repr(run.call_args.args))
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertGreater(run.call_args.kwargs["timeout"], 0)


class DiagnosticHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_testing_route_auth_and_no_overrides(self) -> None:
        fixture = DiagnosticFixture()
        allowed = MagicMock(return_value=True)

        def http_error(**kwargs: Any) -> HTTPException:
            return HTTPException(
                status_code=kwargs["status_code"],
                detail={"code": kwargs["code"], "message": kwargs["message"]},
            )

        common = ReadRouteDependencies(
            read_identity=lambda: _identity(),
            get_record_store=lambda: fixture.store,
            next_trace_id=lambda: "fixture-trace",
            authorization_allows=allowed,
            http_error=http_error,
            error_response_model=diagnostic.ImportDiagnosticResult,
        )
        app = FastAPI()
        register_odoo_runtime_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=OdooRuntimeReadDependencies(
                common=common, control_plane_root=Path("fixture"), database_url=None
            ),
        )
        path = (
            f"/v1/products/{fixture.profile.product}/environments/testing/odoo-import-diagnostics"
        )
        with patch(
            "control_plane.http_routes.odoo_runtime.diagnose_imports",
            return_value=diagnostic.unknown_result(),
        ) as execute:
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["action"], "product_environment.read")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertNotIn(PRIVATE, response.text)
            execute.reset_mock()
            for bad_path in (
                path.replace("/testing/", "/prod/"),
                path + "?host=override.invalid",
                path + "?sql=SELECT",
            ):
                self.assertEqual((await request(app, "GET", bad_path)).status_code, 400)
            self.assertEqual(
                (await request(app, "POST", path, payload={"password": PRIVATE})).status_code, 405
            )
            allowed.return_value = False
            self.assertEqual((await request(app, "GET", path)).status_code, 403)
            execute.assert_not_called()
