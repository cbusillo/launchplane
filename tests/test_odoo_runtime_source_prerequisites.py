"""Execute the fixed provider/host/container chain against synthetic installed producers."""

import base64
import builtins
import contextlib
import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException

from control_plane import odoo_runtime_source_prerequisites as reader, odoo_runtime_reads as reads
from control_plane.odoo_import_diagnostics import SOURCE_PREFIXES
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.contracts.artifact_release_compatibility import ReleaseInputFile
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.http_routes.odoo_runtime import (
    OdooRuntimeReadDependencies,
    register_odoo_runtime_read_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from tests.support.auth import _identity
from tests.support.http import request
from tests.test_odoo_import_diagnostics import DiagnosticFixture, PRIVATE
from tests.test_release_compatibility import artifact


class PrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tenant = self.root / "tenant"
        self.framework = self.root / "framework" / "odoo"
        self.framework.mkdir(parents=True)
        (self.framework / "__init__.py").write_text(
            "raise AssertionError('Odoo must not be imported')"
        )
        self.config = self.root / "odoo.conf"
        self.data = self.root / "data"
        self.data.mkdir()
        self.config.write_text(
            "[options]\ndb_name=fixture_db\ndb_host=database\ndb_user=local\n"
            f"db_password={PRIVATE}\naddons_path={self.tenant / 'addons'}\ndata_dir={self.data}\n"
        )
        template = (
            Path(__file__).parent / "fixtures/source_prerequisites/connection_settings.py"
        ).read_text()
        files = []
        for name, (addon, model, client) in reader.SOURCE_ADDONS.items():
            for relative in (
                "__init__.py",
                "__manifest__.py",
                "models/__init__.py",
                f"models/{model}.py",
                "services/__init__.py",
                f"services/{client}.py",
                "services/connection_settings.py",
            ):
                path = self.tenant / "addons" / addon / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    template.replace("__PREFIX__", SOURCE_PREFIXES[name])
                    .replace("__CONSUME_USE_SSL__", "enabled" if name != "fishbowl" else "disabled")
                    .replace("__HAS_BATCH__", "enabled" if name == "repairshopr" else "disabled")
                    if relative == "services/connection_settings.py"
                    else "# synthetic caller\n"
                )
                files.append(
                    ReleaseInputFile(
                        path=f"addons/{addon}/{relative}",
                        kind="unknown",
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
        self.manifest = artifact("fixture-artifact", files=tuple(files))
        self.fixture.profile = self.fixture.profile.model_copy(
            update={"repository": "example/site"}
        )
        self.fixture.store.read_product_profile_record.return_value = self.fixture.profile
        self.fixture.identity = self.fixture.identity.model_copy(
            update={
                "source_git_ref": self.manifest.source_commit,
                "image_reference": f"{self.manifest.image.repository}@{self.manifest.image.digest}",
            }
        )
        self.fixture.connection = replace(
            self.fixture.connection,
            selection=replace(self.fixture.connection.selection, identity=self.fixture.identity),
            evidence=self.fixture.connection.evidence.model_copy(
                update={"identity": self.fixture.identity}
            ),
        )
        self.fixture.store.read_artifact_manifest.return_value = self.manifest
        self.fixture.store.read_odoo_instance_override_record.side_effect = FileNotFoundError()
        self.fixture.runtime = self.fixture.runtime.model_copy(update={"env": {}})
        self.env = {
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_NAME": "fixture_db",
            RUNTIME_IDENTITY_ENV_KEY: self.fixture.identity.model_dump_json(),
        }
        self.values = {}
        for prefix in SOURCE_PREFIXES.values():
            for field in ("host", "user", "password"):
                key = prefix + "." + field
                value = PRIVATE if field == "password" else "synthetic-" + field
                self.values[key] = value
                encode = (
                    config_parameter_secret_env_key
                    if field == "password"
                    else config_parameter_runtime_key
                )
                self.env[encode(key)] = value
        self.pg = MagicMock()
        self.cursor = self.pg.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = ("on",)
        self.cursor.fetchmany.side_effect = self.fetchmany
        self.cursor.execute.side_effect = self.execute_sql
        self.pg.set_session.side_effect = self.set_session
        self.readonly = False
        self.queries: list[tuple[str, Any]] = []
        self.connects: list[dict[str, Any]] = []
        self.mounts: list[dict[str, str]] = []
        self.drift = ""
        self.inspections = 0
        self.container_calls = 0
        self.parameter_reads = 0
        self.installed = {value[0]: "installed" for value in reader.SOURCE_ADDONS.values()}
        self.payloads: list[dict[str, Any]] = []

    def set_session(self, **kwargs: Any) -> None:
        self.assertEqual(kwargs, {"readonly": True, "autocommit": False})
        self.readonly = True

    def execute_sql(self, query: str, parameters: Any = None) -> None:
        self.assertTrue(self.readonly)
        self.queries.append((query, parameters))
        self.assertTrue(query == "SHOW transaction_read_only" or query.startswith("SELECT "))
        if query.startswith("SELECT key"):
            self.parameter_reads += 1
            if self.drift == "parameter" and self.parameter_reads > 1:
                self.values[next(iter(self.values))] = "changed"
            if self.drift == "file" and self.parameter_reads > 1:
                self.producer("cm_data").write_text("# changed")
            if self.drift == "config" and self.parameter_reads > 1:
                self.config.write_text(self.config.read_text() + "\n# changed\n")

    def fetchmany(self, bound: int) -> list[tuple[str, str]]:
        query, params = self.queries[-1]
        if query.startswith("SELECT name"):
            self.assertEqual(bound, len(params[0]) + 1)
            return list(self.installed.items())[:bound]
        keys = params[0]
        self.assertEqual(bound, len(keys) + 1)
        self.assertLessEqual(len(keys), len(reader.SOURCE_ADDONS) * 16)
        return [(key, self.values[key]) for key in keys if key in self.values][:bound]

    def pg_connect(self, **kwargs: Any) -> Any:
        self.assertEqual(kwargs["host"], "database")
        self.assertIn("default_transaction_read_only=on", kwargs["options"])
        self.assertIn("statement_timeout=", kwargs["options"])
        self.readonly = False
        self.connects.append(kwargs)
        return self.pg

    def path(self, raw: Any) -> Path:
        value = str(raw)
        if value == ODOO_WEB_CONFIG_PATH:
            return self.config
        for prefix, target in (
            ("/opt/project", self.tenant),
            ("/opt/launchplane", self.root / "launchplane"),
            ("/odoo", self.framework.parent),
        ):
            if value == prefix or value.startswith(prefix + "/"):
                return Path(str(target) + value[len(prefix) :])
        return Path(raw)

    def producer(self, name: str) -> Path:
        return (
            self.tenant
            / "addons"
            / reader.SOURCE_ADDONS[name][0]
            / "services/connection_settings.py"
        )

    def update_producer(self, name: str, text: str) -> None:
        path = self.producer(name)
        path.write_text(text)
        declaration = self.manifest.release_compatibility
        assert declaration is not None
        sources = list(declaration.sources)
        sources[0] = sources[0].model_copy(
            update={
                "files": tuple(
                    file.model_copy(
                        update={"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    )
                    if file.path == str(path.relative_to(self.tenant))
                    else file
                    for file in sources[0].files
                )
            }
        )
        self.manifest.release_compatibility = declaration.model_copy(
            update={"sources": tuple(sources)}
        )

    def execute_program(self, program: str, input_text: str, argv: list[str] | None = None) -> str:
        original_import = builtins.__import__

        def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] in {"odoo", "pymysql"}:
                raise AssertionError("Source clients and Odoo initialization are forbidden")
            return original_import(name, *args, **kwargs)

        with (
            patch.object(sys, "stdin", io.StringIO(input_text)),
            patch.object(sys, "argv", argv or ["fixed-reader"]),
            patch("builtins.__import__", side_effect=guarded_import),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            exec(compile(program, "<fixed-reader>", "exec"), {"__name__": "__main__"})
        return output.getvalue()

    def subprocess_run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            self.inspections += 1
            value: dict[str, Any] = {
                "Id": self.fixture.connection.evidence.container_id,
                "Image": "sha256:" + "d" * 64,
                "Config": {
                    "Image": self.fixture.identity.image_reference,
                    "Labels": {
                        "com.docker.compose.project": self.fixture.connection.app_name,
                        "com.docker.compose.service": "web",
                    },
                    "Env": [key + "=" + value for key, value in self.env.items()],
                },
                "State": {"Running": True, "StartedAt": "synthetic-start", "Pid": 12},
                "Mounts": self.mounts,
                "HostConfig": {},
                "NetworkSettings": {},
            }
            if self.drift == "container" and self.inspections > 1:
                value["State"]["Pid"] = 13
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([value]))
        if command[:3] == ["docker", "image", "inspect"]:
            reference = (
                self.fixture.identity.image_reference
                if self.drift != "image"
                else "foreign@sha256:" + "f" * 64
            )
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps([{"Id": command[-1], "RepoDigests": [reference]}])
            )
        self.assertEqual(command[:2], ["docker", "exec"])
        self.assertIn("--kill-after=2s", command)
        self.assertGreater(kwargs["timeout"], 0)
        self.container_calls += 1
        if self.drift == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        spec = json.loads(kwargs["input"])
        spec["test_only"] = True
        # Paths in the fixed program resolve into this isolated synthetic filesystem.
        with (
            patch("pathlib.Path", side_effect=self.path),
            patch.dict(os.environ, self.env, clear=True),
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=self.pg_connect)}),
        ):
            output = self.execute_program(command[-1], json.dumps(spec))
        if self.drift == "frame":
            output = json.dumps(
                {
                    "runtime": "verified",
                    "sources": [{"source": name, "raw": PRIVATE} for name in reader.SOURCE_ADDONS],
                }
            )
        if self.drift == "oversized":
            output = "x" * 16385
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def remote(self, **kwargs: Any) -> None:
        self.payloads.append(kwargs["payload"])
        encoded = base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode()
        with patch("subprocess.run", side_effect=self.subprocess_run):
            output = self.execute_program(kwargs["program"], "", ["host", encoded, "FRAME:"])
        for line in output.splitlines():
            if line.startswith("FRAME:"):
                kwargs["receive"](json.loads(line.removeprefix("FRAME:")))

    def read(self) -> reader.SourcePrerequisiteResult:
        with (
            patch.object(reader, "_read_remote", side_effect=self.remote),
            patch.object(
                reads,
                "select_stable_runtime",
                return_value=self.fixture.connection.selection,
            ),
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(
                reads, "inspect_runtime_container", side_effect=lambda *args: self.env.copy()
            ),
            patch.object(reads, "assert_selection_current"),
        ):
            return reader.diagnose_source_prerequisites(
                store=self.fixture.store,
                product=self.fixture.profile.product,
                control_plane_root=self.root,
                database_url=None,
            )

    def test_installed_defaults_resolve_without_authority_credentials_or_exports(self) -> None:
        result = self.read()
        self.assertEqual(result.runtime, "verified")
        for source in result.sources:
            self.assertEqual(source.producer, "verified")
            self.assertEqual(source.addon_state, "installed")
            self.assertEqual(source.binding, "selected")
            self.assertEqual(source.applied_delivered_password, "match")
            self.assertEqual(source.managed_password_value_agreement, "unknown")
            for option in source.options:
                self.assertEqual(option.authority, "absent")
                self.assertEqual(option.authority_applied, "match")
        self.assertNotIn(PRIVATE, result.model_dump_json())
        self.assertNotIn(PRIVATE, json.dumps(self.payloads))
        self.assertEqual(self.pg.rollback.call_count, len(self.connects))
        self.assertEqual(self.pg.close.call_count, len(self.connects))
        self.assertEqual(self.container_calls, 1)

    def test_runtime_source_layout_does_not_require_importable_odoo(self) -> None:
        with patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(self.read().runtime, "verified")

    def test_password_reference_metadata_is_separate_from_local_password_agreement(self) -> None:
        record = OdooInstanceOverrideRecord(
            context=self.fixture.context,
            instance="testing",
            config_parameters=tuple(
                OdooConfigParameterOverride(
                    key=SOURCE_PREFIXES[binding.binding_id] + ".password",
                    value=OdooOverrideValue(
                        source="secret_binding", secret_binding_id=binding.binding_id
                    ),
                )
                for binding in self.fixture.bindings
            ),
            updated_at=self.fixture.runtime.updated_at,
        )
        self.fixture.store.read_odoo_instance_override_record.side_effect = None
        self.fixture.store.read_odoo_instance_override_record.return_value = record
        with (
            patch("control_plane.odoo_import_diagnostics._decrypt_secret_value") as decrypt,
            patch("control_plane.secrets._decrypt_secret_value") as base_decrypt,
        ):
            result = self.read()
            self.assertTrue(all(source.secret_reference == "match" for source in result.sources))
            lane_binding = self.fixture.bindings[0]
            lane_secret = self.fixture.records[0]
            context_binding = lane_binding.model_copy(
                update={
                    "binding_id": "context-source",
                    "secret_id": "context-source",
                    "instance": "",
                    "declared_secret_class": None,
                }
            )
            self.fixture.bindings.append(context_binding)
            self.fixture.records.append(
                lane_secret.model_copy(
                    update={
                        "secret_id": "context-source",
                        "name": "context-source",
                        "scope": "context",
                        "instance": "",
                        "current_version_id": "context-source-v1",
                    }
                )
            )
            stale_parameter = record.config_parameters[0].model_copy(
                update={
                    "value": OdooOverrideValue(
                        source="secret_binding", secret_binding_id=context_binding.binding_id
                    )
                }
            )
            self.fixture.store.read_odoo_instance_override_record.return_value = record.model_copy(
                update={"config_parameters": (stale_parameter, *record.config_parameters[1:])}
            )
            source = self.read().sources[0]
            self.assertEqual(source.secret_reference, "mismatch")
            self.assertEqual(source.applied_delivered_password, "match")
            self.assertEqual(source.managed_password_value_agreement, "unknown")
            decrypt.assert_not_called()
            base_decrypt.assert_not_called()

    def test_constant_ssl_and_batch_options_follow_their_producer_shapes(self) -> None:
        fishbowl = SOURCE_PREFIXES["fishbowl"]
        repairshopr = SOURCE_PREFIXES["repairshopr"]
        self.values[fishbowl + ".use_ssl"] = "disabled"
        self.env[config_parameter_runtime_key(fishbowl + ".use_ssl")] = "disabled"
        self.values[repairshopr + ".batch_size"] = "17"
        self.env[config_parameter_runtime_key(repairshopr + ".batch_size")] = "17"
        self.fixture.runtime = self.fixture.runtime.model_copy(
            update={
                "env": {
                    config_parameter_runtime_key(fishbowl + ".use_ssl"): "disabled",
                    config_parameter_runtime_key(repairshopr + ".batch_size"): "17",
                }
            }
        )
        result = self.read()
        fish = result.sources[2]
        ssl = next(option for option in fish.options if option.field == "use_ssl")
        self.assertEqual(ssl.applied, "absent")
        self.assertEqual(ssl.authority_applied, "match")
        self.assertEqual(fish.applied_tls_configuration, "enabled_with_verification")
        batch = next(option for option in result.sources[1].options if option.field == "batch_size")
        self.assertEqual(batch.applied, "explicit")
        self.assertEqual(batch.authority_applied, "match")

    def test_excluded_authority_is_unknown_instead_of_an_omitted_default(self) -> None:
        key = config_parameter_runtime_key(SOURCE_PREFIXES["cm_data"] + ".port")
        original = self.fixture.store.read_dokploy_target_record.return_value
        for kind in ("target", "retired"):
            with self.subTest(kind=kind):
                self.fixture.store.read_dokploy_target_record.return_value = original.model_copy(
                    update={"env": {key: "17"}} if kind == "target" else {}
                )
                self.fixture.runtime = self.fixture.runtime.model_copy(
                    update={"retired_provider_keys": (key,) if kind == "retired" else ()}
                )
                source = self.read().sources[0]
                self.assertEqual(source.options[0].authority, "unknown")
                self.assertEqual(source.options[0].authority_applied, "unknown")

    def test_authority_changes_after_the_runtime_read_discard_evidence(self) -> None:
        original_remote = self.remote

        def changing_remote(**kwargs: Any) -> None:
            original_remote(**kwargs)
            self.fixture.runtime = self.fixture.runtime.model_copy(
                update={
                    "env": {
                        config_parameter_runtime_key(SOURCE_PREFIXES["cm_data"] + ".port"): "17"
                    }
                }
            )

        with patch.object(self, "remote", side_effect=changing_remote):
            self.assertEqual(self.read().runtime, "unknown")

    def test_symlink_or_modified_caller_disqualifies_only_that_source(self) -> None:
        addon = reader.SOURCE_ADDONS["cm_data"][0]
        path = self.tenant / "addons" / addon / "__init__.py"
        original = path.read_bytes()
        path.write_text("# modified caller")
        result = self.read()
        self.assertEqual(result.sources[0].producer, "unknown")
        self.assertEqual(result.sources[1].producer, "verified")
        path.unlink()
        target = self.root / "foreign.py"
        target.write_bytes(original)
        path.symlink_to(target)
        self.assertEqual(self.read().sources[0].producer, "unknown")

    def test_server_independently_rejects_nonclosed_or_duplicate_provider_frames(self) -> None:
        authority = reader._authority(self.fixture.store, self.fixture.profile.product)
        files = reader.producer_files(
            self.manifest, self.fixture.connection, self.fixture.profile.repository
        )
        minimal = {
            "runtime": "unknown",
            "sources": [{"source": name} for name in reader.SOURCE_ADDONS],
        }
        for frames in (
            [{"type": "complete", "result": {**minimal, "raw": PRIVATE}}],
            [{"type": "complete", "result": minimal}, {"type": "complete", "result": minimal}],
            [{"type": "complete", "result": {**minimal, "applied": True}}],
            [{"type": "complete", "result": {**minimal, "values_redacted": False}}],
        ):
            with self.subTest(frames=frames):

                def remote(**kwargs: Any) -> None:
                    for frame in frames:
                        kwargs["receive"](frame)

                with (
                    patch.object(reader, "_read_remote", side_effect=remote),
                    self.assertRaises(ValueError),
                ):
                    reader.runtime_prerequisites(
                        self.fixture.connection,
                        files=files,
                        authority=authority,
                        observed_identity=self.env[RUNTIME_IDENTITY_ENV_KEY],
                    )

    def test_producer_default_changes_are_consumed_and_not_copied(self) -> None:
        prefix = SOURCE_PREFIXES["cm_data"]
        port = prefix + ".port"
        replacement = runpy.run_path(str(self.producer("cm_data")))["DEFAULT_PORT"] + 37
        self.values[port] = str(replacement)
        self.assertEqual(self.read().sources[0].options[0].delivered_applied, "mismatch")
        path = self.producer("cm_data")
        self.update_producer("cm_data", path.read_text() + f"\nDEFAULT_PORT = {replacement}\n")
        self.assertEqual(self.read().sources[0].options[0].delivered_applied, "match")

    def test_full_resolver_tls_disagreement_cannot_report_verified_configuration(self) -> None:
        self.update_producer(
            "cm_data",
            self.producer("cm_data").read_text()
            + """
from dataclasses import replace
original_full = resolve_connection_settings
def resolve_connection_settings(values):
    return replace(original_full(values), use_ssl=False)
""",
        )
        source = self.read().sources[0]
        self.assertEqual(source.producer, "verified")
        self.assertEqual(source.applied_settings, "unknown")
        self.assertEqual(source.delivered_settings, "unknown")
        self.assertEqual(source.applied_tls_configuration, "unknown")
        self.assertEqual(source.delivered_tls_configuration, "unknown")

    def test_invalid_option_keeps_origin_and_other_source_evidence(self) -> None:
        self.values[SOURCE_PREFIXES["cm_data"] + ".port"] = "bad"
        result = self.read()
        source = result.sources[0]
        self.assertEqual(source.producer, "verified")
        self.assertEqual(source.options[0].applied, "explicit")
        self.assertEqual(source.options[0].delivered_applied, "unknown")
        self.assertEqual(source.applied_tls_configuration, "unknown")
        self.assertEqual(result.sources[1].applied_settings, "valid")

    def test_password_comparison_is_local_and_does_not_attest_managed_value(self) -> None:
        self.values[SOURCE_PREFIXES["cm_data"] + ".password"] = "other-synthetic-password"
        source = self.read().sources[0]
        self.assertEqual(source.binding, "selected")
        self.assertEqual(source.applied_delivered_password, "mismatch")
        self.assertEqual(source.managed_password_value_agreement, "unknown")
        self.assertNotIn("other-synthetic-password", json.dumps(self.payloads))

    def test_explicit_empty_and_unknown_authority_and_disabled_tls(self) -> None:
        prefix = SOURCE_PREFIXES["cm_data"]
        self.fixture.runtime = self.fixture.runtime.model_copy(
            update={
                "env": {
                    config_parameter_runtime_key(prefix + ".port"): "",
                    config_parameter_runtime_key(prefix + ".use_ssl"): "disabled",
                }
            }
        )
        self.values[prefix + ".use_ssl"] = "disabled"
        self.env[config_parameter_runtime_key(prefix + ".use_ssl")] = "disabled"
        source = self.read().sources[0]
        self.assertEqual(source.options[0].authority, "empty")
        self.assertEqual(source.options[1].authority, "explicit")
        self.assertEqual(source.authority_tls_configuration, "disabled")
        self.assertEqual(source.applied_tls_configuration, "disabled")
        global_record = RuntimeEnvironmentRecord(
            scope="global",
            env={config_parameter_runtime_key(prefix + ".port"): "123"},
            updated_at=self.fixture.runtime.updated_at,
        )
        self.fixture.store.list_runtime_environment_records.side_effect = lambda **kwargs: (
            global_record,
            self.fixture.runtime,
        )
        source = self.read().sources[0]
        self.assertEqual(source.options[0].authority, "empty")
        self.assertEqual(source.options[0].authority_applied, "match")

    def test_provenance_protocol_and_drift_fail_closed(self) -> None:
        original_values = self.values.copy()
        original_config = self.config.read_bytes()
        original_producer = self.producer("cm_data").read_bytes()
        for drift in (
            "image",
            "container",
            "parameter",
            "file",
            "config",
            "frame",
            "oversized",
            "timeout",
        ):
            with self.subTest(drift=drift):
                self.drift = drift
                result = self.read()
                if drift == "file":
                    self.assertEqual(result.sources[0].producer, "unknown")
                else:
                    self.assertEqual(result.runtime, "unknown")
                self.assertNotIn(PRIVATE, result.model_dump_json())
                self.drift = ""
                self.inspections = 0
                self.parameter_reads = 0
                self.values = original_values.copy()
                self.config.write_bytes(original_config)
                self.producer("cm_data").write_bytes(original_producer)

    def test_readonly_refusal_rolls_back_and_closes_without_querying_parameters(self) -> None:
        self.cursor.fetchone.return_value = ("off",)
        self.assertEqual(self.read().runtime, "unknown")
        self.assertEqual(self.queries[-1][0], "SHOW transaction_read_only")
        self.pg.rollback.assert_called_once()
        self.pg.close.assert_called_once()

    def test_shadow_mount_and_uninstalled_source_are_independent(self) -> None:
        shadow = self.data / "addons" / "future-series" / reader.SOURCE_ADDONS["cm_data"][0]
        shadow.mkdir(parents=True)
        result = self.read()
        self.assertEqual(result.sources[0].producer, "unknown")
        self.assertEqual(result.sources[1].producer, "verified")
        shadow.rmdir()
        self.installed[reader.SOURCE_ADDONS["cm_data"][0]] = "uninstalled"
        result = self.read()
        self.assertEqual(result.sources[0].addon_state, "unknown")
        self.assertEqual(result.sources[1].addon_state, "installed")
        self.mounts = [{"Destination": str(self.tenant / "addons")}]
        self.assertEqual(self.read().runtime, "unknown")

    def test_complete_source_inventory_and_artifact_identity_are_required(self) -> None:
        for update in (
            {"source_commit": "f" * 40},
            {"release_compatibility": None},
            {"source_build": None},
        ):
            with self.subTest(update=update):
                manifest = self.manifest.model_copy(update=update)
                with self.assertRaises(ValueError):
                    reader.producer_files(
                        manifest, self.fixture.connection, self.fixture.profile.repository
                    )


class PrerequisiteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_testing_product_read_authority_no_overrides_and_no_store(self) -> None:
        fixture = DiagnosticFixture()
        allowed = MagicMock(return_value=True)

        def http_error(**kwargs: Any) -> HTTPException:
            return HTTPException(status_code=kwargs["status_code"], detail={"code": kwargs["code"]})

        app = FastAPI()
        register_odoo_runtime_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=OdooRuntimeReadDependencies(
                common=ReadRouteDependencies(
                    read_identity=lambda: _identity(),
                    get_record_store=lambda: fixture.store,
                    next_trace_id=lambda: "fixture",
                    authorization_allows=allowed,
                    http_error=http_error,
                    error_response_model=reader.SourcePrerequisiteResult,
                ),
                control_plane_root=Path("fixture"),
                database_url=None,
            ),
        )
        path = (
            f"/v1/products/{fixture.profile.product}/environments/testing/odoo-source-prerequisites"
        )
        with patch(
            "control_plane.http_routes.odoo_runtime.diagnose_source_prerequisites",
            return_value=reader.unknown_result(),
        ) as execute:
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertEqual(allowed.call_args.kwargs["action"], "operations.read")
            execute.reset_mock()
            for invalid in (
                path.replace("/testing/", "/prod/"),
                path + "?sql=SELECT",
                path + "?host=foreign",
            ):
                self.assertEqual((await request(app, "GET", invalid)).status_code, 400)
            self.assertEqual((await request(app, "POST", path, payload={})).status_code, 405)
            allowed.return_value = False
            self.assertEqual((await request(app, "GET", path)).status_code, 403)
            execute.assert_not_called()
