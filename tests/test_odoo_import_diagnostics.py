import contextlib
import io
import json
import os
import struct
import subprocess
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
import pymysql
from pydantic import SecretStr
from pymysql.constants import CLIENT, CR

from control_plane import odoo_import_diagnostics as diagnostic
from control_plane import odoo_runtime_reads as reads
from control_plane.odoo_diagnostic_identity import (
    OdooDiagnosticIdentity,
    DIAGNOSTIC_IDENTITY_UNAVAILABLE,
)
from control_plane import odoo_source_probe as probe
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetPolicies
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord, ScalarValue
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.runtime_identity import RuntimeIdentity, RUNTIME_IDENTITY_ENV_KEY
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
                container_id="c" * 64,
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
            diagnostic_identity=OdooDiagnosticIdentity(
                product=self.identity.product,
                context=self.identity.context,
                instance=self.identity.instance,
                database="fixture_db",
                login="diagnostic-reader",
                password=SecretStr("diagnostic-password"),
                user_id=7,
            ),
        )


class DiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.environment = {RUNTIME_IDENTITY_ENV_KEY: self.fixture.identity.model_dump_json()}
        for name, prefix in diagnostic.SOURCE_PREFIXES.items():
            for key in (*diagnostic.FIELDS, "password"):
                env_key = (
                    config_parameter_secret_env_key(prefix + ".password")
                    if key == "password"
                    else config_parameter_runtime_key(prefix + "." + key)
                )
                self.environment[env_key] = self.fixture.applied[prefix + "." + key]
        self.runtime_probe = patch.object(
            diagnostic,
            "runtime_grants",
            side_effect=lambda connection, sources, **kw: {name: "read_only" for name in sources},
        )
        self.runtime_mock = self.runtime_probe.start()
        self.addCleanup(self.runtime_probe.stop)
        self.cache = patch.dict(diagnostic._FAILED_PROBES, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.patches = [
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(reads, "inspect_runtime_container", return_value=self.environment),
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

    def test_agreement_and_runtime_grants_are_value_free(self) -> None:
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
            self.assertEqual(source.applied_connection_grants, "read_only")
        for private in (PRIVATE, "fixture_reader", ".invalid", "fixture-ciphertext"):
            self.assertNotIn(
                private, result.model_dump_json() + stdout.getvalue() + stderr.getvalue()
            )
        self.assertFalse(result.applied)
        writes = [
            call for call in self.fixture.store.mock_calls if str(call).startswith("call.write_")
        ]
        self.assertEqual(writes, [])

    def test_origin_privileges_are_independent(self) -> None:
        self.runtime_mock.side_effect = lambda connection, sources, **kw: {
            name: "unsafe" if name == "cm_data" else "read_only" for name in sources
        }
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].managed_connection_grants, "read_only")
        self.assertEqual(result.sources[0].applied_connection_grants, "unsafe")
        self.assertEqual(result.sources[1].applied_connection_grants, "read_only")
        self.runtime_mock.side_effect = lambda connection, sources, **kw: {
            name: "unknown" for name in sources
        }
        result = self.run_diagnostic()
        self.assertTrue(
            all(source.applied_connection_grants == "unknown" for source in result.sources)
        )
        self.assertTrue(
            all(source.managed_connection_grants == "read_only" for source in result.sources)
        )

    def test_runtime_never_receives_mismatched_or_unsafe_sources(self) -> None:
        self.fixture.applied["cm_data.db.user"] = "writer"
        self.environment[config_parameter_secret_env_key("repairshopr.sync_db.password")] = "stale"
        self.fixture.bindings[2] = self.fixture.bindings[2].model_copy(
            update={"declared_secret_class": "prod_only"}
        )
        result = self.run_diagnostic()
        self.assertTrue(
            all(source.applied_connection_grants == "unknown" for source in result.sources)
        )
        self.runtime_mock.assert_not_called()

    def test_runtime_probe_single_flight_cooldown_and_fresh_success(self) -> None:
        def nested(connection: object, sources: tuple[str, ...], **kw: object) -> dict[str, str]:
            self.assertTrue(
                all(
                    value == "unknown"
                    for value in diagnostic.runtime_origin_grants(
                        self.fixture.connection,
                        diagnostic.import_authority(
                            self.fixture.store, self.fixture.profile.product
                        ).sources,
                        self.environment,
                    ).values()
                )
            )
            return {name: "unknown" if name == "cm_data" else "read_only" for name in sources}

        self.runtime_mock.side_effect = nested
        self.run_diagnostic()
        self.runtime_mock.side_effect = lambda connection, sources, **kw: {
            name: "read_only" for name in sources
        }
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].applied_connection_grants, "unknown")
        self.assertEqual(result.sources[1].applied_connection_grants, "read_only")
        self.assertNotIn("cm_data", self.runtime_mock.call_args.args[1])
        with patch(
            "control_plane.odoo_import_diagnostics.time.monotonic", return_value=float("inf")
        ):
            result = self.run_diagnostic()
        self.assertTrue(
            all(source.applied_connection_grants == "read_only" for source in result.sources)
        )

    def test_applied_writer_mismatch_does_not_authenticate_as_writer(self) -> None:
        self.fixture.applied["cm_data.db.user"] = "old_writer"
        self.fixture.applied["cm_data.db.password"] = "old_writer_password"
        result = self.run_diagnostic()
        fields = {item.field: item.agreement for item in result.sources[0].parameters}
        self.assertEqual(fields["user"], "mismatch")
        self.assertEqual(fields["password"], "mismatch")
        self.assertEqual(result.sources[0].applied_connection_grants, "unknown")
        settings = self.mocks[-1].call_args_list[0].args[0]
        self.assertEqual(settings["password"], PRIVATE)
        self.assertEqual(settings["user"], "fixture_reader")

    def test_missing_unsafe_and_unreachable_are_independent(self) -> None:
        self.fixture.bindings[0] = self.fixture.bindings[0].model_copy(
            update={"declared_secret_class": "prod_only"}
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
        self.assertTrue(
            all(
                item.agreement == "match"
                for item in result.sources[0].parameters
                if item.field != "password"
            )
        )
        self.mocks[-2].assert_called_once()
        self.mocks[-1].assert_called_once()

    def test_source_failure_cannot_hide_other_source(self) -> None:
        self.mocks[-2].side_effect = [ValueError(PRIVATE), PRIVATE, PRIVATE]
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].binding, "missing_or_unsafe")
        self.assertEqual(result.sources[1].managed_connection_grants, "read_only")
        self.assertNotIn(PRIVATE, result.model_dump_json())

    def test_non_prod_delivery_and_equivalent_boolean_agree(self) -> None:
        self.fixture.bindings[0] = self.fixture.bindings[0].model_copy(
            update={"declared_secret_class": "non_prod"}
        )
        self.fixture.applied["cm_data.db.ssl_verify"] = "true"
        source = self.run_diagnostic().sources[0]
        self.assertEqual(source.binding, "selected")
        self.assertTrue(all(item.agreement == "match" for item in source.parameters))

    def test_retired_password_is_never_decrypted_or_probed(self) -> None:
        key = self.fixture.bindings[0].binding_key
        self.fixture.runtime = self.fixture.runtime.model_copy(
            update={
                "schema_version": 2,
                "retired_provider_keys": (key,),
            }
        )
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].binding, "missing_or_unsafe")
        self.assertEqual(self.mocks[-2].call_count, 2)
        self.assertEqual(self.mocks[-1].call_count, 2)

    def test_changed_host_cannot_receive_selected_password(self) -> None:
        values = dict(self.fixture.runtime.env)
        values[config_parameter_runtime_key("cm_data.db.host")] = "redirect.invalid"
        self.fixture.runtime = self.fixture.runtime.model_copy(update={"env": values})
        result = self.run_diagnostic()
        self.assertEqual(result.sources[0].managed_connection_grants, "unknown")
        fields = {item.field: item.agreement for item in result.sources[0].parameters}
        self.assertEqual(fields["host"], "mismatch")
        self.assertEqual(self.mocks[-1].call_count, 2)

    def test_same_account_password_mismatch_does_not_attempt_login(self) -> None:
        self.fixture.applied["cm_data.db.password"] = "new-upstream-password-fixture"
        for user in ("fixture_reader", " fixture_reader "):
            with self.subTest(user=user):
                self.fixture.applied["cm_data.db.user"] = user
                self.mocks[-1].reset_mock()
                result = self.run_diagnostic()
                self.assertEqual(result.sources[0].managed_connection_grants, "unknown")
                self.assertEqual(self.mocks[-1].call_count, 2)

    def test_runtime_and_config_drift_discard_evidence(self) -> None:
        for kind in (
            "runtime",
            "settings",
            "binding",
            "container",
            "applied",
            "nonsecret_binding",
            "delivered",
        ):
            with self.subTest(kind=kind):
                fixture = DiagnosticFixture()
                self.fixture = fixture

                def drift(settings: dict[str, str], **reference: object) -> str:
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
                    elif kind == "nonsecret_binding":
                        fixture.bindings.append(
                            fixture.bindings[0].model_copy(
                                update={
                                    "binding_id": "host-binding",
                                    "binding_key": config_parameter_runtime_key("cm_data.db.host"),
                                }
                            )
                        )
                    elif kind == "delivered":
                        self.mocks[1].return_value = {"CHANGED": "fixture"}
                    elif kind == "applied":
                        fixture.applied["fishbowl.password"] = "changed-fixture-password"
                    else:
                        self.mocks[1].side_effect = ValueError(PRIVATE)
                    return "read_only"

                self.mocks[-1].side_effect = drift
                result = self.run_diagnostic()
                self.assertEqual(result.runtime, "unknown")
                self.assertTrue(
                    all(
                        source.managed_connection_grants
                        == source.applied_connection_grants
                        == "unknown"
                        for source in result.sources
                    )
                )
                self.mocks[1].side_effect = None
                self.mocks[1].return_value = self.environment

    def test_binding_drift_during_runtime_read_discards_all_evidence(self) -> None:
        def drift(connection: object, sources: tuple[str, ...], **kwargs: object) -> dict[str, str]:
            self.fixture.bindings.clear()
            return {name: "read_only" for name in sources}

        self.runtime_mock.side_effect = drift
        result = self.run_diagnostic()
        self.assertEqual(result.runtime, "unknown")
        self.assertTrue(
            all(
                source.managed_connection_grants == source.applied_connection_grants == "unknown"
                for source in result.sources
            )
        )

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

    def test_missing_diagnostic_identity_remains_an_explicit_unavailable_error(self) -> None:
        self.mocks[0].side_effect = reads._diagnostic_unavailable()
        with self.assertRaises(reads.OdooRuntimeReadError) as caught:
            self.run_diagnostic()
        self.assertEqual(caught.exception.status_code, 503)


class AppliedReadTests(unittest.TestCase):
    def test_fixed_read_discards_values_and_rechecks_health(self) -> None:
        fixture = DiagnosticFixture()
        calls: list[tuple[str, dict[str, object]]] = []

        def rpc(
            opener: object, origin: str, path: str, params: dict[str, Any], **kwargs: object
        ) -> object:
            calls.append((path, params))
            if path.endswith("authenticate"):
                self.assertEqual(params["login"], "diagnostic-reader")
                self.assertEqual(params["password"], "diagnostic-password")
                return {"uid": 7}
            if path.endswith("destroy"):
                return None
            requested = set(params["args"][0][0][2])
            self.assertLessEqual(requested, set(fixture.applied))
            return [{"key": key, "value": value} for key, value in fixture.applied.items()]

        with (
            patch.object(diagnostic, "odoo_compose_has_domain", return_value=True),
            patch.object(
                reads,
                "_http_json",
                return_value={"runtime_identity": fixture.identity.model_dump()},
            ),
            patch.object(reads, "_rpc", side_effect=rpc),
        ):
            self.assertEqual(
                diagnostic.read_applied_parameters(fixture.connection), fixture.applied
            )
        self.assertTrue(calls[-1][0].endswith("destroy"))


class GrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = patch.dict(
            "control_plane.odoo_import_diagnostics._FAILED_PROBES", {}, clear=True
        )
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def wire_result(self, result: probe.GrantStatus) -> str:
        with (
            patch.object(probe, "probe", return_value=result),
            patch("control_plane.odoo_source_probe.sys.stdin", io.StringIO("{}")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            probe.main()
        return output.getvalue()

    def test_only_configured_ca_paths_reach_child(self) -> None:
        settings = {"host": "source.invalid", "user": "reader", "password": PRIVATE}
        trust = {"SSL_CERT_FILE": "/fixture/ca.pem", "SSL_CERT_DIR": "/fixture/ca"}
        with (
            patch.dict(os.environ, {**trust, "LAUNCHPLANE_MASTER_KEY": PRIVATE}, clear=True),
            patch(
                "control_plane.odoo_import_diagnostics.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=self.wire_result("read_only")
                ),
            ) as run,
        ):
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "read_only",
            )
        self.assertEqual(run.call_args.kwargs["env"], trust)
        self.assertNotIn(PRIVATE, repr(run.call_args.kwargs["env"]))

    def test_server_without_tls_receives_no_authentication(self) -> None:
        # MySQL protocol-10 handshake: normal auth capabilities, with SSL absent.
        capabilities = CLIENT.PROTOCOL_41 | CLIENT.SECURE_CONNECTION | CLIENT.PLUGIN_AUTH
        handshake = (
            b"\x0a8.0.36\0"
            + struct.pack("<I", 1)
            + b"12345678\0"
            + struct.pack("<H", capabilities & 0xFFFF)
            + b"\x21"
            + struct.pack("<H", 2)
            + struct.pack("<H", capabilities >> 16)
            + b"\x15"
            + b"\0" * 10
            + b"abcdefghijkl\0mysql_native_password\0"
        )
        transport = MagicMock()
        transport.makefile.return_value = io.BytesIO(
            len(handshake).to_bytes(3, "little") + b"\0" + handshake
        )
        with (
            patch("socket.create_connection", return_value=transport),
            self.assertRaises(pymysql.OperationalError) as raised,
        ):
            probe.probe({"host": "source.invalid", "user": "reader", "password": PRIVATE})
        self.assertEqual(raised.exception.args[0], CR.CR_SSL_CONNECTION_ERROR)
        transport.sendall.assert_not_called()

    def test_failed_login_polling_is_cooled_down_but_success_is_fresh(self) -> None:
        settings = {"host": "source.invalid", "user": "reader", "password": PRIVATE}
        with (
            patch(
                "control_plane.odoo_import_diagnostics.time.monotonic", return_value=100
            ) as clock,
            patch(
                "control_plane.odoo_import_diagnostics.subprocess.run", side_effect=OSError(PRIVATE)
            ) as run,
        ):
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "unknown",
            )
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "unknown",
            )
            run.assert_called_once()
            self.assertNotIn(PRIVATE, repr(diagnostic._FAILED_PROBES))
            clock.return_value = 1000
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=self.wire_result("read_only")
            )
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "read_only",
            )
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "read_only",
            )
            self.assertEqual(run.call_count, 3)

    def test_same_source_has_one_in_flight_probe(self) -> None:
        settings = {"host": "source.invalid", "user": "reader", "password": PRIVATE}

        def concurrent(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "unknown",
            )
            return subprocess.CompletedProcess([], 0, stdout=self.wire_result("read_only"))

        with patch(
            "control_plane.odoo_import_diagnostics.subprocess.run", side_effect=concurrent
        ) as run:
            self.assertEqual(
                diagnostic.managed_grants(
                    settings, binding_id="fixture-binding", version_id="fixture-version"
                ),
                "read_only",
            )
            run.assert_called_once()

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
        for index, error in enumerate(
            (OSError(PRIVATE), ValueError(PRIVATE), RuntimeError(PRIVATE))
        ):
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
            with patch(
                "control_plane.odoo_import_diagnostics.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=stdout.getvalue()),
            ):
                self.assertEqual(
                    diagnostic.managed_grants(
                        {"host": "source.invalid", "user": "reader", "password": PRIVATE},
                        binding_id="fixture-binding",
                        version_id=f"failure-{index}",
                    ),
                    "unknown",
                )
            self.assertNotIn(PRIVATE, stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_parent_timeout_and_malformed_response_never_leak(self) -> None:
        settings = {"host": "source.invalid", "user": "reader", "password": PRIVATE}
        for index, value in enumerate(
            (
                subprocess.TimeoutExpired("probe", 10, output=PRIVATE),
                OSError(PRIVATE),
                subprocess.CompletedProcess([], 0, stdout=PRIVATE),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        {**json.loads(self.wire_result("read_only")), "unexpected": PRIVATE}
                    ),
                ),
            )
        ):
            settings["user"] = f"reader-{index}"
            kwargs = (
                {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            )
            with patch("control_plane.odoo_import_diagnostics.subprocess.run", **kwargs) as run:
                self.assertEqual(
                    diagnostic.managed_grants(
                        settings, binding_id="fixture-binding", version_id="fixture-version"
                    ),
                    "unknown",
                )
            self.assertNotIn(PRIVATE, repr(run.call_args.args))
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
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertNotIn(PRIVATE, response.text)
            execute.side_effect = reads._diagnostic_unavailable()
            unavailable = await request(app, "GET", path)
            self.assertEqual(unavailable.status_code, 503, unavailable.text)
            self.assertEqual(unavailable.json()["detail"]["code"], DIAGNOSTIC_IDENTITY_UNAVAILABLE)
            self.assertNotIn(PRIVATE, unavailable.text)
            execute.side_effect = None
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
            fixture.store.read_product_profile_record.return_value = fixture.profile.model_copy(
                update={"driver_id": "generic-web"}
            )
            self.assertEqual((await request(app, "GET", path)).status_code, 400)
