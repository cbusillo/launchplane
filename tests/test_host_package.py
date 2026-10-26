import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from control_plane.host_package import compact_host_input, host_package, host_schedule
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
            payload[offset : offset + 24 * 1024] for offset in range(0, len(payload), 24 * 1024)
        ]
        for index, chunk in enumerate(chunks):
            install(
                {
                    "version": self.package.version,
                    "mode": "chunk",
                    "index": index,
                    "content": base64.b64encode(chunk).decode(),
                }
            )
        result = install({"version": self.package.version, "mode": "commit", "count": len(chunks)})
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
        self.assertEqual(result.stdout.strip(), "launchplane_host_version=" + self.package.version)
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
            install({"version": self.package.version, "mode": "commit", "count": 2})

    def test_installer_refuses_aliases_and_changed_existing_versions(self) -> None:
        archive = self.upload()
        archive.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            install({"version": self.package.version, "mode": "inspect"})
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

        def upsert(**arguments: object) -> dict[str, str]:
            payload = arguments["schedule_payload"]
            assert isinstance(payload, dict)
            api.validate_dokploy_schedule_script(payload)
            key = str(len(scripts))
            scripts[key] = str(payload["script"])
            return {"scheduleId": key}

        def run(**arguments: object) -> str:
            script = scripts[str(arguments["schedule_id"])]
            subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "HOME": str(self.root)},
                capture_output=True,
                check=True,
            )
            return "completed"

        with (
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(api, "upsert_dokploy_schedule", side_effect=upsert),
            patch.object(api, "run_dokploy_schedule", side_effect=run),
        ):
            ensure_schedule_host_package(
                host="https://provider.invalid",
                token="fixture",
                target_id="fixture-user",
                schedule_type="dokploy-server",
                app_name="fixture",
                payload={"script": host_schedule("resources", {}), "serverId": None},
            )
        archive = self.root / ".local/state/launchplane-host" / (self.package.version + ".pyz")
        self.assertEqual(archive.read_bytes(), self.package.content)
