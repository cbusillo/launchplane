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
from unittest.mock import patch

from control_plane.dokploy import api, native_copy_stream
from control_plane.dokploy import compose_inventory as inventory
from control_plane.dokploy.target_source_setup import COMPOSE_SOURCE_FIELDS
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

    def documents(self, **overrides: Any) -> tuple[dict[str, Any], ...]:
        return inventory.read_volume_documents(
            host="https://provider.invalid",
            token="fixture",
            payload={**self.payload, **overrides},
            raw=json.dumps(self.wrapper),
            server_id="selected-server",
            exact=True,
        )

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
