from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.preview_clean_snapshot import build_preview_clean_snapshot_script
from control_plane.dokploy.api import validate_dokploy_schedule_script


class PreviewCleanSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.snapshot_id = "a" * 64
        self.source = (
            self.root / ".local/state/launchplane-clean-testing-snapshots" / self.snapshot_id
        )
        self.source.mkdir(parents=True)
        (self.source / "database.dump").write_bytes(b"clean snapshot database")
        (self.source / "filestore").mkdir()
        (self.source / "filestore/attachment").write_bytes(b"clean snapshot attachment")
        library: dict[str, Any] = {}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        receipt = {
            "snapshot_id": self.snapshot_id,
            "database_sha256": hashlib.sha256(
                (self.source / "database.dump").read_bytes()
            ).hexdigest(),
            "filestore_sha256": library["snapshot_tree_digest"](self.source / "filestore"),
        }
        (self.source / "receipt.json").write_text(json.dumps(receipt))
        self.snapshot = CleanTestingSnapshot(
            product="example-odoo",
            context="example",
            server_id="fixture-server",
            captured_at=datetime.now(timezone.utc).isoformat(),
            **receipt,
        )
        self.project = "example-pr-23"
        self.database = "example_pr_23_db"
        self.image = "registry.invalid/odoo@sha256:" + "b" * 64
        self.data = self.root / "preview-data"
        self.data.mkdir()
        self.calls: list[list[str]] = []
        self.present = False
        self.sanitized = True
        self.paused = False
        self.containers = {
            role: {
                "Config": {
                    "Image": self.image,
                    "Labels": {
                        "com.docker.compose.project": self.project,
                        "com.docker.compose.service": role,
                    },
                    "Env": [
                        "PLATFORM_INSTANCE=preview",
                        "ODOO_DB_HOST=database",
                        "ODOO_DB_NAME=" + self.database,
                    ],
                },
                "Mounts": [
                    {"Type": "volume", "Name": "preview-data", "Destination": "/volumes/data"}
                ],
            }
            for role in ("database", "web", "script-runner")
        }

    def run_script(self, **updates: Any) -> str:
        arguments = {
            "snapshot": self.snapshot,
            "server_id": self.snapshot.server_id,
            "compose_project": self.project,
            "database_name": self.database,
            "image_reference": self.image,
            "workflow_environment": {"ODOO_UPDATE_MODULES": "website"},
            "readback_spec": "fixture-policy",
            "readback_program": "fixture-readback",
            "web_gate_path": "/volumes/data/fixture-gate",
            **updates,
        }
        script = build_preview_clean_snapshot_script(**arguments)
        validate_dokploy_schedule_script({"script": script, "appName": self.project})
        program = script.partition("<<'LP_PREVIEW_SNAPSHOT'\n")[2].rsplit(
            "\nLP_PREVIEW_SNAPSHOT", 1
        )[0]
        output = io.StringIO()
        with (
            patch("pathlib.Path.home", return_value=self.root),
            patch("subprocess.run", side_effect=self.command),
            redirect_stdout(output),
        ):
            exec(compile(program, "<preview-snapshot-fixture>", "exec"), {})
        return output.getvalue()

    def command(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(arguments)
        output = b""
        if arguments[0] in {"ssh", "rsync"}:
            self.fail("A preview must not read an upstream source")
        if arguments[0] == "cp":
            self.assertTrue(self.paused)
            self.assertEqual(Path(arguments[-2]), self.source / "filestore")
            shutil.copytree(arguments[-2], arguments[-1])
        elif arguments[:2] == ["docker", "ps"]:
            output = (
                b"web\nscript-runner"
                if any(value.startswith("volume=") for value in arguments)
                else "\n".join(self.containers).encode()
            )
        elif arguments[:2] == ["docker", "inspect"]:
            output = json.dumps([self.containers[arguments[-1]]]).encode()
        elif arguments[:3] == ["docker", "volume", "inspect"]:
            output = json.dumps(
                [
                    {
                        "Driver": "local",
                        "Options": {},
                        "Mountpoint": str(self.data),
                        "Labels": {"com.docker.compose.project": self.project},
                    }
                ]
            ).encode()
        elif arguments[:2] == ["docker", "pause"]:
            self.paused = True
        elif arguments[:2] == ["docker", "unpause"]:
            self.paused = False
        elif arguments[:2] == ["docker", "exec"]:
            if "psql " in " ".join(arguments):
                output = b"1" if self.present else b"0"
            elif arguments[-2:] in (["id", "-u"], ["id", "-g"]):
                output = b"1000"
            elif kwargs.get("input") == b"fixture-readback":
                output = b"integration_readback_ok=true" if self.sanitized else b""
            elif kwargs.get("input"):
                self.assertFalse(self.paused)
                output = b"\n".join(
                    key + b"=true"
                    for key in (
                        b"testing_copy_sanitized",
                        b"testing_copy_pre_registry_sanitized",
                        b"odoo_module_update_completed",
                        b"odoo_module_update_modules_configured",
                    )
                )
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

    def test_preview_clones_only_frozen_bytes_and_sanitizes_before_web(self) -> None:
        live = self.root / "live-testing"
        live.mkdir()
        (live / "attachment").write_bytes(b"staff edits")
        output = self.run_script()
        self.assertIn("clean_preview_snapshot_restored=true", output.splitlines())
        self.assertEqual(
            (self.data / "filestore" / self.database / "attachment").read_bytes(),
            (self.source / "filestore/attachment").read_bytes(),
        )
        self.assertIn(["docker", "start", "web"], self.calls)
        self.assertFalse(any(str(live) in argument for call in self.calls for argument in call))

    def test_existing_preview_data_and_staff_edits_are_never_overwritten(self) -> None:
        self.present = True
        with self.assertRaisesRegex(RuntimeError, "cannot be overwritten"):
            self.run_script()
        self.assertFalse(any(call[0] == "cp" for call in self.calls))

    def test_failed_sanitizer_or_snapshot_integrity_never_starts_web(self) -> None:
        self.sanitized = False
        with self.assertRaisesRegex(RuntimeError, "connection readback"):
            self.run_script()
        self.assertNotIn(["docker", "start", "web"], self.calls)
        (self.source / "database.dump").write_bytes(b"corrupted snapshot")
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            self.run_script()
        self.assertFalse(self.calls)

    def test_production_reader_and_cross_host_fallback_are_refused(self) -> None:
        for updates in (
            {"workflow_environment": {"ODOO_UPSTREAM_HOST": "production.invalid"}},
            {"workflow_environment": {"DATA_WORKFLOW_SSH_DIR": "/existing-reader"}},
            {"server_id": "another-server"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.run_script(**updates)
        self.assertFalse(self.calls)
