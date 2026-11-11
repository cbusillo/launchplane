import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
import yaml
from unittest.mock import patch

from control_plane.dokploy import api, native_copy_stream
from control_plane.dokploy import compose_inventory as inventory
from control_plane.dokploy.target_source_setup import COMPOSE_SOURCE_FIELDS
from control_plane.contracts.candidate_custody_evidence import (
    ConsumerEvidence,
    VolumeDocumentEvidence,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.retained_volume_sweep_execution import _inventory_clear, compose_volume_names


class CachedComposeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / "compose"
        self.base.mkdir()
        self.project = self.base / "selected-project"
        self.code = self.project / "code"
        self.root = self.code / "docker/coolify/compose.yml"
        self.child = self.code / "addons/sync/compose.yml"
        self.root.parent.mkdir(parents=True)
        self.child.parent.mkdir(parents=True)
        self.environment = self.root.parent / ".env"
        self.environment.write_text("SECRET=private-fixture-token\n")
        self.wrapper: dict[str, Any] = {
            "name": "ignored",
            "include": ["../../addons/sync/compose.yml"],
        }
        self.default_name = "default-state"
        self.child_document: dict[str, Any] = {
            "services": {"web": {"image": "example.invalid/fixture", "volumes": ["data:/data"]}},
            "volumes": {"data": {"name": "${STATE_NAME:-" + self.default_name + "}"}},
        }
        self.payload: dict[str, Any] = {
            **dict.fromkeys(COMPOSE_SOURCE_FIELDS),
            "composeId": "selected-target",
            "environmentId": "selected-environment",
            "serverId": "selected-server",
            "appName": self.project.name,
            "sourceType": "git",
            "customGitUrl": "https://example.invalid/product.git",
            "composeType": "docker-compose",
            "composePath": "docker/coolify/compose.yml",
            "createEnvFile": True,
            "command": None,
            "randomize": False,
            "isolatedDeployment": False,
            "env": "",
            "mounts": [],
            "domains": [],
            "backups": [],
            "deployments": [],
        }
        self.write_sources()
        self.enterContext(patch.object(native_copy_stream, "run_managed_server_program", self.read))
        self.schedules = self.enterContext(
            patch.object(api, "list_dokploy_schedules", return_value=())
        )

    def write_sources(self) -> None:
        self.root.write_text(json.dumps(self.wrapper))
        self.child.write_text(json.dumps(self.child_document))

    def read(self, **arguments: Any) -> None:
        result = inventory.read_cached_files(self.base, arguments["payload"])
        arguments["receive"]({"type": "complete", "result": result})

    def documents(
        self, *, failures: list[str] | None = None, **overrides: Any
    ) -> tuple[dict[str, Any], ...]:
        return inventory.read_volume_documents(
            host="https://provider.invalid",
            token="fixture",
            payload={**self.payload, **overrides},
            raw=json.dumps(self.wrapper),
            server_id="selected-server",
            exact=True,
            failures=failures,
        )

    def test_cached_packet_keeps_each_include_when_one_document_is_unreadable(self) -> None:
        bad = self.child.parent / "bad.yml"
        bad.write_text("null")
        self.wrapper["include"].append("../../addons/sync/bad.yml")
        self.root.write_text(json.dumps(self.wrapper))
        evidence = ConsumerEvidence(kind="compose")
        with self.assertRaises(ValueError):
            inventory.read_volume_documents(
                host="provider",
                token="private",
                payload=self.payload,
                raw=json.dumps(self.wrapper),
                server_id="selected-server",
                exact=True,
                evidence=evidence,
                failures=[],
            )
        self.assertEqual(len(evidence.documents), 3)
        by_path = {row.path_sha256: row for row in evidence.documents}
        self.assertEqual(
            by_path[canonical_json_sha256("addons/sync/compose.yml")].checks["acquisition"], "pass"
        )
        self.assertEqual(
            by_path[canonical_json_sha256("addons/sync/bad.yml")].checks["acquisition"],
            "unavailable",
        )
        self.assertTrue(all(row.document_sha256 for row in evidence.documents))
        self.assertNotIn("private-fixture-token", evidence.model_dump_json())

    def test_packet_keeps_both_failed_consumers_for_each_protected_volume(self) -> None:
        first = {**self.payload, "composeId": "remote-consumer", "serverId": "another-server"}
        second = {
            **self.payload,
            **dict.fromkeys(COMPOSE_SOURCE_FIELDS),
            "composeId": "null-consumer",
            "command": "private-command",
            "domains": [{"host": "private.invalid"}],
        }
        environment: dict[str, Any] = {key: [] for key in TARGET_COLLECTIONS}
        environment.update({"environmentId": "environment", "env": "", "compose": [first, second]})
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.source.read_dokploy_config",
                return_value=("https://provider.invalid", "private-token"),
            ),
            patch.object(
                api,
                "fetch_dokploy_projects",
                return_value=[{"projectId": "project", "env": "", "environments": [environment]}],
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **kwargs: (
                    first if kwargs["target_id"] == "remote-consumer" else second
                ),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                side_effect=lambda **kwargs: (
                    json.dumps(self.wrapper)
                    if kwargs["compose_id"] == "remote-consumer"
                    else "null"
                ),
            ),
        ):
            for volume in ("protected-db", "protected-data", "protected-logs"):
                evidence: list[ConsumerEvidence] = []
                references: list[dict[str, str]] = []
                binding = RetainedVolumeBinding(
                    volume_name=volume,
                    server_id="selected-server",
                    application_id="candidate",
                    application_name="candidate",
                    volume_identity_sha256="a" * 64,
                    engine_identity_sha256="b" * 64,
                )
                self.assertFalse(
                    _inventory_clear(
                        self.base, binding, exact=True, references=references, evidence=evidence
                    )
                )
                consumers = [item for item in evidence if item.kind == "compose"]
                self.assertEqual(len(consumers), 2)
                self.assertEqual(consumers[0].checks["placement"], "fail")
                self.assertEqual(consumers[1].conversion_shape, "null")
                self.assertEqual(consumers[1].checks["command_empty"], "fail")
                self.assertEqual(consumers[1].checks["domains_empty"], "fail")
                self.assertEqual(consumers[1].checks["source_fields_empty"], "pass")
                self.assertEqual(consumers[1].checks["schedules_empty"], "not_reached")
                self.assertEqual(
                    {item.record_sha256 for item in consumers},
                    {
                        canonical_json_sha256({"kind": "compose", "id": row["composeId"]})
                        for row in (first, second)
                    },
                )
                self.assertEqual(
                    {row["record_id"] for row in references}, {"remote-consumer", "null-consumer"}
                )
                encoded = json.dumps([item.model_dump(mode="json") for item in evidence])
                for secret in (
                    "private-token",
                    "private-command",
                    "private.invalid",
                    "another-server",
                    "selected-server",
                    "customGitUrl",
                ):
                    self.assertNotIn(secret, encoded)

    def test_packet_distinguishes_local_missing_malformed_placement_and_empty_prerequisites(
        self,
    ) -> None:
        for placement, value, expected in (
            (True, None, "local"),
            (True, "", "malformed"),
            (False, None, "missing"),
        ):
            payload = {**self.payload, **dict.fromkeys(COMPOSE_SOURCE_FIELDS)}
            payload.pop("serverId")
            if placement:
                payload["serverId"] = value
            evidence = ConsumerEvidence(kind="compose")
            with self.assertRaises(ValueError):
                inventory.read_volume_documents(
                    host="provider",
                    token="private",
                    payload=payload,
                    raw="null",
                    server_id="selected-server",
                    exact=True,
                    evidence=evidence,
                )
            self.assertEqual(evidence.server_identity, expected)
            self.assertEqual(evidence.conversion_shape, "null")
            self.assertEqual(evidence.checks["cached_project_absent"], "not_reached")
        payload = {**self.payload, **dict.fromkeys(COMPOSE_SOURCE_FIELDS)}
        shutil.rmtree(self.project)
        evidence = ConsumerEvidence(kind="compose")
        self.assertEqual(
            inventory.read_volume_documents(
                host="provider",
                token="private",
                payload=payload,
                raw="null",
                server_id="selected-server",
                exact=True,
                evidence=evidence,
            ),
            (),
        )
        self.assertEqual(evidence.checks["schedules_empty"], "pass")
        self.assertEqual(evidence.checks["cached_project_absent"], "pass")

    def test_resolution_packet_keeps_valid_siblings_after_bad_inputs(self) -> None:
        document = {
            "volumes": {
                "bad": {"name": "${MISSING}"},
                "external": {"external": True},
                "local": {},
                "override": {"name": "protected-data"},
            }
        }
        evidence = VolumeDocumentEvidence()
        with self.assertRaises(ValueError):
            compose_volume_names(document, self.payload, evidence=evidence)
        self.assertEqual(len(evidence.volumes), 4)
        self.assertEqual(evidence.volumes[0].checks["unique_inputs"], "unavailable")
        self.assertEqual(
            set(evidence.resolved_name_sha256),
            {
                canonical_json_sha256(name)
                for name in ("external", self.payload["appName"] + "_local", "protected-data")
            },
        )
        self.assertEqual(evidence.checks["resolution"], "fail")

    def test_packet_does_not_hold_readable_numeric_or_date_documents(self) -> None:
        from datetime import date

        self.child_document["services"]["web"].update(cpus=0.5)
        self.child_document["volumes"]["data"]["name"] = self.default_name
        self.child_document["x-built"] = date(2026, 1, 2)
        self.payload["cpuLimit"] = 0.5
        self.child.write_text(yaml.safe_dump(self.child_document))
        for raw in (yaml.safe_dump(self.child_document), json.dumps(self.wrapper)):
            with self.subTest(raw=raw):
                args: dict[str, Any] = dict(
                    host="provider",
                    token="private",
                    payload=self.payload,
                    raw=raw,
                    server_id="selected-server",
                    exact=True,
                )
                expected = inventory.read_volume_documents(**args)
                evidence = ConsumerEvidence(kind="compose")
                actual = inventory.read_volume_documents(**args, evidence=evidence)
                self.assertEqual(actual, expected)
                self.assertEqual(evidence.checks["conversion_parse"], "pass")
                self.assertTrue(evidence.payload_sha256)
                for index, document in enumerate(actual):
                    entry = next(row for row in evidence.documents if row.resolution_index == index)
                    self.assertEqual(
                        compose_volume_names(document, self.payload, evidence=entry),
                        compose_volume_names(document, self.payload),
                    )

    def test_packet_does_not_confuse_parsing_paths_or_inputs_with_source_proof(self) -> None:
        for raw, payload in (
            ("null", {**self.payload, "serverId": None}),
            (json.dumps(self.wrapper), {**self.payload, "serverId": "elsewhere"}),
        ):
            evidence = ConsumerEvidence(kind="compose")
            with self.assertRaises(ValueError):
                inventory.read_volume_documents(
                    host="provider",
                    token="private",
                    payload=payload,
                    raw=raw,
                    server_id="selected-server",
                    exact=True,
                    evidence=evidence,
                )
            self.assertEqual(evidence.checks["conversion_parse"], "pass")
            self.assertNotEqual(evidence.checks["root"], "pass")
            self.assertNotEqual(evidence.checks["default_absence"], "pass")
            self.assertEqual(evidence.checks["source_fields_empty"], "fail")
            self.assertEqual(evidence.checks["command_empty"], "pass")
            self.assertIsNone(evidence.cached_endpoint_sha256)
            if raw == "null":
                self.assertEqual(evidence.checks["source_scope"], "not_reached")
            else:
                self.assertEqual(evidence.checks["source_scope"], "pass")
                self.assertEqual(evidence.checks["includes"], "pass")
        evidence = ConsumerEvidence(kind="compose")
        original = inventory.read_cached_compose_source
        calls = 0

        def read(**kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("private failed proof")
            return original(**kwargs)

        with (
            patch.object(inventory, "read_cached_compose_source", side_effect=read),
            self.assertRaises(OSError),
        ):
            inventory.read_volume_documents(
                host="provider",
                token="private",
                payload=self.payload,
                raw=json.dumps(self.wrapper),
                server_id="selected-server",
                exact=True,
                evidence=evidence,
            )
        self.assertEqual(evidence.checks["default_acquisition"], "unavailable")
        self.assertNotEqual(evidence.checks["default_absence"], "pass")
        self.assertEqual(evidence.documents[1].checks["default_inputs"], "pass")

    def test_diagnostics_distinguish_conversion_source_and_acquisition_without_replacing_errors(
        self,
    ) -> None:
        for raw, overrides, reason in (
            ("[broken", {}, "inventory_converted_document_malformed"),
            ("[]", {}, "inventory_converted_document_malformed"),
            ('{"services": []}', {}, "inventory_converted_document_malformed"),
            ("null", {}, "inventory_converted_document_absent"),
            (
                json.dumps(self.wrapper),
                {"serverId": "elsewhere"},
                "inventory_source_placement_unqualified",
            ),
            (
                json.dumps(self.wrapper),
                {"command": "unsupported"},
                "inventory_source_scope_unqualified",
            ),
        ):
            failures: list[str] = []
            arguments: dict[str, Any] = dict(
                host="https://provider.invalid",
                token="fixture",
                payload={**self.payload, **overrides},
                raw=raw,
                server_id="selected-server",
                exact=True,
            )
            with self.subTest(reason=reason):
                with self.assertRaises((ValueError, yaml.YAMLError)) as legacy:
                    inventory.read_volume_documents(**arguments)
                with self.assertRaises(type(legacy.exception)):
                    inventory.read_volume_documents(**arguments, failures=failures)
                self.assertIn(reason, failures)
        for error, reason in (
            (OSError("private source path"), "inventory_source_acquisition_failed"),
            (ValueError("private invalid content"), "inventory_source_observation_unqualified"),
            (
                native_copy_stream.NativeSourceAuthorizationDenied("secret denial"),
                "inventory_acquisition_denied",
            ),
            (
                api.DokployRequestFailed(
                    method="GET", path="/private", detail="secret denial", status_code=403
                ),
                "inventory_acquisition_denied",
            ),
        ):
            failures = []
            with patch.object(inventory, "read_cached_compose_source", side_effect=error):
                for diagnostics in (None, failures):
                    with self.assertRaises(type(error)) as caught:
                        self.documents(failures=diagnostics)
                    self.assertIs(caught.exception, error)
            self.assertIn(reason, failures)
            self.assertNotIn("inventory_source_scope_unqualified", failures)
            self.assertNotIn(str(error), json.dumps(failures))

    def test_diagnostic_include_qualification_preserves_readable_siblings(self) -> None:
        self.wrapper["include"].insert(0, "broken.yml")
        self.write_sources()
        (self.root.parent / "broken.yml").write_text('{"services": []}')
        with self.assertRaises(ValueError):
            self.documents()
        failures: list[str] = []
        documents = self.documents(failures=failures)
        self.assertEqual(compose_volume_names(documents[0], self.payload), (self.default_name,))
        self.assertIn("inventory_included_document_unqualified", failures)

    def test_include_defaults_need_independent_absence_and_leave_sources_unchanged(self) -> None:
        before = (self.root.read_bytes(), self.child.read_bytes(), self.environment.read_bytes())
        documents = self.documents()
        self.assertEqual(
            compose_volume_names(documents[0], self.payload),
            (self.default_name,),
        )
        self.assertEqual(
            (self.root.read_bytes(), self.child.read_bytes(), self.environment.read_bytes()), before
        )
        for raw in ("STATE_NAME=other", "STATE_NAME=", "STATE_NAME=one\nSTATE_NAME=two"):
            with self.subTest(api_environment=raw), self.assertRaises(ValueError):
                self.documents(env=raw)
        self.environment.write_text("STATE_NAME=old-state\n")
        with self.assertRaises(ValueError):
            self.documents()

    def test_cached_volume_declarations_retain_literal_and_project_identities(self) -> None:
        for definition in ({"name": "retained-state"}, {}, {"external": True}):
            self.child_document["volumes"]["data"] = definition
            self.write_sources()
            document = self.documents()[0]
            expected = definition.get("name") or (
                "data" if definition.get("external") else self.project.name + "_data"
            )
            self.assertEqual(compose_volume_names(document, self.payload), (expected,))

    def test_missing_inputs_cannot_hide_bare_required_or_nested_occurrences(self) -> None:
        for expression in (
            "${STATE_NAME:-safe}-${STATE_NAME}",
            "${STATE_NAME:?required}",
            "${STATE_NAME:-${SECOND}}",
            "${APP_NAME:-safe}",
        ):
            self.child_document["volumes"]["data"]["name"] = expression
            self.write_sources()
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.documents()

    def test_complete_cached_inventory_finds_resolved_references_and_preserves_denials(
        self,
    ) -> None:
        binding = RetainedVolumeBinding(
            volume_name="retained-state",
            server_id=self.payload["serverId"],
            application_id="retired-application",
            application_name="retired-application",
        )
        environment: dict[str, Any] = {**{kind: [] for kind in TARGET_COLLECTIONS}, "env": ""}
        environment["compose"] = [{"composeId": self.payload["composeId"]}]
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch.object(
                api,
                "fetch_dokploy_projects",
                return_value=({"env": "", "environments": [environment]},),
            ),
            patch.object(api, "fetch_dokploy_target_payload", return_value=self.payload),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                return_value=json.dumps(self.wrapper),
            ) as converted,
        ):
            self.assertTrue(_inventory_clear(self.base, binding, exact=True))
            for name in (
                binding.volume_name,
                "${STATE_NAME:-" + binding.volume_name + "}",
            ):
                self.child_document["volumes"]["data"]["name"] = name
                self.write_sources()
                with self.subTest(name=name):
                    self.assertFalse(_inventory_clear(self.base, binding, exact=True))
            self.child_document["volumes"]["data"]["name"] = self.default_name
            self.write_sources()
            denied = native_copy_stream.NativeSourceAuthorizationDenied("fixture denial")
            with (
                patch.object(native_copy_stream, "run_managed_server_program", side_effect=denied),
                self.assertRaises(type(denied)),
            ):
                _inventory_clear(self.base, binding, exact=True)
            converted.side_effect = api.DokployRequestFailed(
                method="GET", path="/fixture", detail="fixture denial", status_code=403
            )
            with self.assertRaises(api.DokployRequestFailed):
                _inventory_clear(self.base, binding, exact=True)

    def test_incomplete_cached_include_scopes_remain_held(self) -> None:
        child = deepcopy(self.child_document)
        for change in (
            {"volumes": {"data": True}},
            {"volumes": {"data": "invalid"}},
            {"volumes": {"data": ["invalid"]}},
            {"include": ["another.yml"]},
            {"services": {"web": {"extends": {"file": "another.yml", "service": "web"}}}},
            {"services": {"web": {"env_file": "another.env"}}},
            {"services": {"web": {"volumes": ["./state:/data"]}}},
        ):
            self.child_document = {**child, **change}
            self.write_sources()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.documents()
        self.child_document = child
        self.write_sources()
        for change in (
            {"services": {"web": {"volumes": ["retained:/data"]}}},
            {"volumes": {"retained": {"name": "retained-state"}}},
            {"include": ["another.yml"]},
        ):
            self.root.write_text(json.dumps({**self.wrapper, **change}))
            with self.subTest(cached_root=change), self.assertRaises(ValueError):
                self.documents()
        self.write_sources()
        for changes in (
            {"serverId": "other-server"},
            {"isolatedDeployment": True},
            {"mounts": [{"type": "file"}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.documents(**changes)
        (self.child.parent / ".env").write_text("STATE_NAME=other")
        with self.assertRaises(ValueError):
            self.documents()
        (self.child.parent / ".env").unlink()
        for include in (
            [{"path": "../../addons/sync/compose.yml", "env_file": "extra.env"}],
            ["oci://example/compose"],
            ["../../../../outside.yml"],
        ):
            self.wrapper["include"] = include
            self.write_sources()
            with self.subTest(include=include), self.assertRaises(ValueError):
                self.documents()

    def test_empty_provider_source_needs_the_entire_cached_project_to_be_absent(self) -> None:
        payload = {
            **self.payload,
            **dict.fromkeys(COMPOSE_SOURCE_FIELDS),
            "sourceType": "github",
        }

        def read(changes: dict[str, Any]) -> tuple[dict[str, Any], ...]:
            return inventory.read_volume_documents(
                host="https://provider.invalid",
                token="fixture",
                payload={**payload, **changes},
                raw="null",
                server_id="selected-server",
                exact=True,
            )

        with self.assertRaises(ValueError):
            read({})
        shutil.rmtree(self.project)
        self.assertEqual(read({}), ())
        for changes in (
            {"customGitUrl": "https://example.invalid/product.git"},
            {"command": "compose up -d"},
            {"deployments": [{"status": "running"}]},
            {"backups": [{"backupId": "retained"}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read(changes)
        self.schedules.return_value = ({"scheduleId": "active"},)
        with self.assertRaises(ValueError):
            read({})

    def test_native_reader_confines_regular_files_and_never_returns_environment_values(
        self,
    ) -> None:
        request = {
            "app_name": self.project.name,
            "files": ["addons/sync/compose.yml"],
            "env_file": "docker/coolify/.env",
            "input_names": ["STATE_NAME"],
        }
        result = inventory.read_cached_files(self.base, request)
        self.assertEqual(set(result["absent_inputs"]), set(request["input_names"]))
        self.assertNotIn("private-fixture-token", json.dumps(result))
        for filename in ("../outside.yml", "/outside.yml", "docker/coolify/.env"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                inventory.read_cached_files(self.base, {**request, "files": [filename]})
        linked = self.code / "linked"
        linked.symlink_to(self.child.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            inventory.read_cached_files(self.base, {**request, "files": ["linked/compose.yml"]})
        fifo = self.code / "fifo.yml"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            inventory.read_cached_files(self.base, {**request, "files": ["fifo.yml"]})
        self.child.unlink()
        self.child.symlink_to(self.root)
        with self.assertRaises((OSError, ValueError)):
            inventory.read_cached_files(self.base, request)
        self.child.unlink()
        self.child.write_bytes(b"x" * (inventory.SOURCE_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            inventory.read_cached_files(self.base, request)

    def test_generated_reader_refuses_an_invalid_request_without_host_runtime_assumptions(
        self,
    ) -> None:
        prefix = "fixture-receipt:"
        request = base64.b64encode(
            json.dumps({"app_name": "../escape", "files": []}).encode()
        ).decode()
        result = subprocess.run(
            [sys.executable, "-c", inventory.CACHED_COMPOSE_PROGRAM, request, prefix],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(result.stdout.startswith(prefix))
        self.assertTrue(json.loads(result.stdout.removeprefix(prefix))["result"]["unavailable"])

    def test_changed_source_observation_is_not_reused(self) -> None:
        original = inventory._read_regular
        changed = False

        def read(path: Path, bound: int) -> bytes:
            nonlocal changed
            result = original(path, bound)
            if path == self.child and not changed:
                changed = True
                self.child.write_text(json.dumps({**self.child_document, "name": "changed"}))
            return result

        with patch.object(inventory, "_read_regular", read), self.assertRaises(ValueError):
            inventory.read_cached_files(
                self.base, {"app_name": self.project.name, "files": ["addons/sync/compose.yml"]}
            )
