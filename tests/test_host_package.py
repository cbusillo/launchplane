import base64
from collections.abc import Iterator
import json
import hashlib
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from control_plane.host_package import (
    compact_host_input,
    host_package,
    host_schedule,
    host_schedules_match,
    HOST_PACKAGE_CHUNK_BYTES,
)
from control_plane import host_package as packages
from control_plane.dokploy import post_deploy
from control_plane.host_package_runtime import HOST_PACKAGE_VERSION_MARKER
from control_plane.host_package_installer import install
from control_plane.host_package_runtime import expand
from control_plane.dokploy import api
from control_plane.dokploy.host_package import ensure_schedule_host_package


class HostPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.package = host_package()

    def upload(self, content: bytes | None = None) -> Path:
        payload = self.package.content if content is None else content
        chunks = [
            payload[offset : offset + HOST_PACKAGE_CHUNK_BYTES]
            for offset in range(0, len(payload), HOST_PACKAGE_CHUNK_BYTES)
        ]
        for chunk in chunks:
            install(
                {
                    "version": self.package.version,
                    "mode": "chunk",
                    "content": base64.b64encode(chunk).decode(),
                }
            )
        result = install(
            {
                "version": self.package.version,
                "mode": "commit",
                "chunks": [hashlib.sha256(chunk).hexdigest() for chunk in chunks],
            }
        )
        self.assertTrue(result["installed"])
        return self.root / ".local/state/launchplane-host" / (self.package.version + ".pyz")

    def test_package_executes_without_service_dependencies_and_reports_its_version(self) -> None:
        archive = self.upload()
        result = subprocess.run(
            [sys.executable, "-I", str(archive), "version"],
            capture_output=True,
            text=True,
            check=True,
        )
        evidence = post_deploy.extract_odoo_post_deploy_readback_markers({"logs": result.stdout})
        self.assertEqual(evidence[HOST_PACKAGE_VERSION_MARKER], self.package.version)
        spec = {
            "runner": self.package.programs["copy_runner"],
            "nested": [self.package.programs["readback"]],
        }
        encoded = compact_host_input(spec)
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(expand(encoded, package), spec)

    def test_corrupt_or_interrupted_upload_never_publishes_a_package(self) -> None:
        with self.assertRaises(ValueError):
            self.upload(b"corrupted archive")
        result = install({"version": self.package.version, "mode": "inspect"})
        self.assertFalse(result["installed"])
        with self.assertRaises(FileNotFoundError):
            install(
                {
                    "version": self.package.version,
                    "mode": "commit",
                    "chunks": [hashlib.sha256(b"missing").hexdigest()],
                }
            )

    def test_installer_repairs_corrupt_package_and_refuses_aliases(self) -> None:
        archive = self.upload()
        archive.write_bytes(b"changed")
        self.assertFalse(install({"version": self.package.version, "mode": "inspect"})["installed"])
        self.upload()
        self.assertEqual(archive.read_bytes(), self.package.content)
        archive.unlink()
        outside = self.root / "outside"
        outside.write_bytes(self.package.content)
        archive.symlink_to(outside)
        with self.assertRaises(OSError):
            install({"version": self.package.version, "mode": "inspect"})
        self.assertEqual(outside.read_bytes(), self.package.content)

    def test_unknown_action_cannot_execute_input_as_code(self) -> None:
        archive = self.upload()
        marker = self.root / "unexpected-effect"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(archive),
                "unknown",
                "--input",
                base64.b64encode(
                    json.dumps({"program": f"open({str(marker)!r},'w')"}).encode()
                ).decode(),
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_schedule_bootstrap_fits_provider_and_installs_exact_package(self) -> None:
        scripts: dict[str, str] = {}
        outputs: dict[str, dict[str, str]] = {}

        def upsert(**arguments: object) -> dict[str, str]:
            payload = arguments["schedule_payload"]
            assert isinstance(payload, dict)
            api.validate_dokploy_schedule_script(payload)
            key = str(len(scripts))
            scripts[key] = str(payload["script"])
            return {"scheduleId": key}

        def run(**arguments: object) -> str:
            script = scripts[str(arguments["schedule_id"])]
            result = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "HOME": str(self.root)},
                capture_output=True,
                check=True,
            )
            outputs["completed"] = post_deploy.extract_odoo_post_deploy_readback_markers(
                {"logs": result.stdout.decode()}
            )
            return "deployment=completed"

        with (
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(api, "upsert_dokploy_schedule", side_effect=upsert),
            patch.object(api, "run_dokploy_schedule", side_effect=run),
            patch.object(api, "dokploy_request", return_value={}),
            patch.object(
                post_deploy,
                "_read_odoo_post_deploy_log_markers",
                side_effect=lambda **_: outputs["completed"],
            ),
        ):
            ensure_schedule_host_package(
                host="https://provider.invalid",
                token="fixture",
                target_id="fixture-user",
                schedule_type="dokploy-server",
                app_name="fixture",
                payload={"script": host_schedule("resources", {}), "serverId": None},
            )
            initial = len(scripts)
            ensure_schedule_host_package(
                host="https://provider.invalid",
                token="fixture",
                target_id="fixture-user",
                schedule_type="dokploy-server",
                app_name="fixture",
                payload={"script": host_schedule("resources", {}), "serverId": None},
            )
            self.assertEqual(len(scripts), initial + 1)
        archive = self.root / ".local/state/launchplane-host" / (self.package.version + ".pyz")
        self.assertEqual(archive.read_bytes(), self.package.content)

    def test_retained_resource_provenance_survives_unrelated_program_update(self) -> None:
        spec: dict[str, object] = {"mode": "prepare", "volumes": {"database": "fixture-database"}}
        original = host_schedule("resources", spec)
        updated = replace(
            self.package,
            version=hashlib.sha256(b"updated package").hexdigest(),
            programs={**self.package.programs, "preflight": "raise RuntimeError()"},
        )
        with patch.object(packages, "host_package", return_value=updated):
            current = host_schedule("resources", spec)
        self.assertNotEqual(original, current)
        self.assertTrue(host_schedules_match(original, current))
        self.assertFalse(
            host_schedules_match(original, host_schedule("resources", {**spec, "mode": "readback"}))
        )
        encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
        legacy = (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_TESTING_RESOURCES'\n"
            + self.package.programs["resources"]
            + "\nLP_TESTING_RESOURCES\n"
        )
        self.assertTrue(host_schedules_match(legacy, current))
        self.assertFalse(host_schedules_match(legacy + "echo unexpected\n", current))

    def test_remote_install_is_reused_and_retained_version_stays_available(self) -> None:
        from control_plane.dokploy import host_package as driver
        from control_plane.dokploy import native_copy_stream

        requests: list[dict[str, object]] = []

        def remote(**arguments: object) -> None:
            payload = arguments["payload"]
            assert isinstance(payload, dict)
            receive = arguments["receive"]
            assert callable(receive)
            requests.append(payload)
            receive(install(payload))

        script = host_schedule("resources", {})

        def ensure() -> None:
            driver.ensure_schedule_host_package(
                host="https://provider.invalid",
                token="fixture",
                target_id="fixture-server",
                schedule_type="server",
                app_name="fixture",
                payload={"script": script, "serverId": "fixture-server"},
            )

        with patch.object(native_copy_stream, "_read_remote", side_effect=remote):
            ensure()
            before = len(requests)
            ensure()
            self.assertEqual(len(requests), before + 1)
            future = replace(self.package, version=hashlib.sha256(b"future package").hexdigest())
            with patch.object(driver, "host_package", return_value=future):
                ensure()
            self.assertEqual(requests[-1]["version"], self.package.version)
            self.assertEqual(requests[-1]["mode"], "require")

    def test_installation_precedes_operation_guard_and_checkpoint(self) -> None:
        from contextlib import contextmanager
        from control_plane.dokploy import host_package as driver, source

        events: list[str] = []

        @contextmanager
        def guard() -> Iterator[None]:
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def installed(**_: object) -> None:
            events.append("install")

        with (
            patch.object(driver, "ensure_schedule_host_package", side_effect=installed),
            patch.object(api, "upsert_dokploy_schedule", return_value={"scheduleId": "fixture"}),
            patch.object(api, "run_dokploy_schedule", return_value="deployment=fixture"),
            patch.object(
                api, "latest_deployment_for_schedule", return_value={"deploymentId": "fixture"}
            ),
            patch.object(
                post_deploy,
                "_read_odoo_post_deploy_log_markers",
                return_value={HOST_PACKAGE_VERSION_MARKER: self.package.version},
            ),
        ):
            post_deploy.run_manual_data_schedule(
                host="https://provider.invalid",
                token="fixture",
                target_definition=source.DokployTargetDefinition(
                    context="fixture", instance="testing", target_id="fixture"
                ),
                schedule_lookup_id="fixture",
                schedule_type="server",
                schedule_name="fixture",
                schedule_app_name="fixture",
                schedule_payload={"script": host_schedule("resources", {})},
                resolved_schedule_execution_timeout_seconds=120,
                before_provider_mutation=lambda _: events.append("effect"),
                provider_mutation_guard=guard,
            )
        self.assertEqual(events, ["install", "enter", "effect", "exit"])

    def test_installer_failure_reports_source_sites_without_path_values(self) -> None:
        from control_plane.dokploy.host_package import bootstrap_script, installer_program
        from control_plane.dokploy.native_copy_stream import NativeProgramFailed

        result = subprocess.run(
            ["bash", "-c", bootstrap_script({"version": "invalid", "mode": "require"})],
            env={**os.environ, "HOME": str(self.root)},
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        frame = json.loads(result.stdout.decode().partition(":")[2])
        failure = NativeProgramFailed(
            "fixture", program=installer_program(), program_sites=frame["sites"]
        )
        self.assertTrue(failure.program_sites)
        self.assertNotIn(str(self.root), result.stdout.decode())
