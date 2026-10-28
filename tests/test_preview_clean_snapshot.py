from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
import base64
import click
import hashlib
import fcntl
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml

from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane._odoo_copy_runtime_guard import VOLUME_BACKING_INSPECTION_LIBRARY
from control_plane.copy_readback_diagnostics import (
    COPY_FAILURE_EXIT_MARKER,
    COPY_FAILURE_EVIDENCE_MARKER,
    COPY_FAILURE_STAGE_MARKER,
    COPY_FAILURE_STATE_DIRECTORY,
    CopyFailureStage,
    CopyReadbackRefused,
    ReadbackPredicate,
)
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.preview_clean_snapshot import build_preview_clean_snapshot_script
from control_plane.dokploy.api import validate_dokploy_schedule_script
from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import (
    DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS,
    DokployTargetDefinition,
)
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY, render_odoo_raw_compose_file
from control_plane.integration_readback import INTEGRATION_READBACK_PROGRAM
from control_plane.preview_filestore import (
    PREPARE_PREVIEW_FILESTORE_PROGRAM,
    PreviewRestoreCustodyUnknown,
    PreviewRestoreDisposalRequired,
    prepare_preview_filestore,
)
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER


class PreviewCleanSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.snapshot_id = "a" * 64
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        with patch("pathlib.Path.home", return_value=self.root):
            self.source = library["clean_snapshot_root"](self.snapshot_id)
        self.source.mkdir()
        (self.source / "database.dump").write_bytes(b"clean snapshot database")
        (self.source / "filestore").mkdir()
        (self.source / "filestore/attachment").write_bytes(b"clean snapshot attachment")
        receipt = {
            "snapshot_id": self.snapshot_id,
            "database_sha256": hashlib.sha256(
                (self.source / "database.dump").read_bytes()
            ).hexdigest(),
            "filestore_sha256": library["snapshot_tree_digest"](self.source / "filestore"),
        }
        (self.source / "receipt.json").write_text(json.dumps(receipt))
        self.snapshot = CleanTestingSnapshot(
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
            ),
            product="example-odoo",
            context="example",
            server_id="fixture-server",
            captured_at=datetime.now(timezone.utc).isoformat(),
            **receipt,
        )
        self.project = "example-pr-23"
        self.database = "example_pr_23_db"
        self.image = "registry.invalid/odoo@sha256:" + "b" * 64
        self.data = self.root / "volumes/preview-data/_data"
        self.data.mkdir(parents=True)
        self.volumes = {
            "preview-data": self.data,
            "preview-db": self.root / "preview-db",
            "preview-logs": self.root / "preview-logs",
            "preview-data-filestore": self.root / "volumes/preview-data-filestore/_data",
        }
        for path in self.volumes.values():
            path.mkdir(parents=True, exist_ok=True)
        self.calls: list[list[str]] = []
        self.present = False
        self.sanitized = True
        self.runner_failure = False
        self.killed_during_restore = False
        self.database_witness = ""
        self.module_lock_available = False
        self.ready_before_web = False
        self.witness_probe_failure = False
        self.paused = False
        self.overlay = self.root / "runner-overlay"
        self.overlay.mkdir()
        self.volume_created_at = datetime.now(timezone.utc).isoformat()
        self.containers: dict[str, dict[str, Any]] = {
            role: {
                "Id": role,
                "ExecIDs": None,
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
                        "ODOO_MAX_CRON_THREADS=0",
                    ],
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": name,
                        "Source": str(self.volumes[name]),
                        "Destination": destination,
                    }
                    for name, destination in (
                        (("preview-db", ODOO_DATABASE_DATA_DIRECTORY),)
                        if role == "database"
                        else (("preview-data", "/volumes/data"), ("preview-logs", "/volumes/logs"))
                    )
                ],
                "NetworkSettings": {"Networks": {"fixture-data": {}}},
            }
            for role in ("database", "web", "script-runner")
        }
        self.containers["script-runner"]["GraphDriver"] = {"Data": {"UpperDir": str(self.overlay)}}
        self.containers["web"]["NetworkSettings"]["Networks"]["dokploy-network"] = {}
        self.private = self.data.parent / "launchplane-preview-filestore"
        for directory in (self.private / "upper", self.private / "work"):
            directory.mkdir(parents=True)
        self.overlay_options = {
            "type": "overlay",
            "device": "overlay",
            "o": f"lowerdir={self.source}/filestore,upperdir={self.private}/upper,workdir={self.private}/work,userxattr",
        }
        self.mounted_filestore = self.root / "runner-filestore"
        (self.data / "filestore").mkdir()
        self.remount_filestore()
        self.volume_options = {"preview-data-filestore": self.overlay_options}
        self.volume_labels = {
            name: {"com.docker.compose.project": self.project} for name in self.volumes
        }
        self.volume_labels["preview-data-filestore"]["io.launchplane.preview-snapshot"] = (
            self.snapshot_id
        )
        self.container_inspection_error = b""
        for role in ("web", "script-runner"):
            self.containers[role]["Mounts"].append(
                {
                    "Type": "volume",
                    "Name": "preview-data-filestore",
                    "Source": str(self.volumes["preview-data-filestore"]),
                    "Destination": "/volumes/data/filestore/" + self.database,
                }
            )

    def remount_filestore(self) -> None:
        destination = self.mounted_filestore
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(self.source / "filestore", destination)

    def run_script(self, **updates: Any) -> str:
        arguments = {
            "snapshot": self.snapshot,
            "server_id": self.snapshot.server_id,
            "compose_project": self.project,
            "database_name": self.database,
            "image_reference": self.image,
            "workflow_environment": {"ODOO_UPDATE_MODULES": "website"},
            "runtime_environment": {
                "ODOO_DATA_VOLUME": "preview-data",
                "ODOO_DB_VOLUME": "preview-db",
                "ODOO_LOG_VOLUME": "preview-logs",
            },
            "readback_spec": "fixture-policy",
            "readback_program": "fixture-readback",
            "web_gate_path": "/volumes/data/fixture-gate",
            **updates,
        }
        script = build_preview_clean_snapshot_script(**arguments)
        validate_dokploy_schedule_script({"script": script, "appName": self.project})
        return self.execute_script(script)

    def prepare_filestore(
        self,
        *,
        snapshot: CleanTestingSnapshot | None,
        apply: bool = True,
        require_existing: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "project": self.project,
            "data_volume": "preview-data",
            "volumes": {"data": "preview-data", "database": "preview-db", "logs": "preview-logs"},
            "database_name": self.database,
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
            "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
            "apply": apply,
            "require_existing": require_existing,
        }
        output = io.StringIO()
        with (
            patch("pathlib.Path.home", return_value=self.root),
            patch("subprocess.run", side_effect=self.command),
            patch(
                "sys.argv",
                ["proof", base64.b64encode(json.dumps(payload).encode()).decode(), "frame:"],
            ),
            redirect_stdout(output),
        ):
            exec(PREPARE_PREVIEW_FILESTORE_PROGRAM, {})
        result = json.loads(output.getvalue().removeprefix("frame:"))
        assert isinstance(result, dict)
        return result

    def assert_native_refusal(self, frame: dict[str, Any], expected: type[Exception]) -> None:
        from control_plane.dokploy import native_copy_stream
        from tests.test_odoo_testing_native_source import Connection

        with (
            patch.object(
                native_copy_stream, "connect", return_value=nullcontext(Connection(frame))
            ),
            self.assertRaises(expected) as caught,
        ):
            prepare_preview_filestore(
                host="https://provider.invalid",
                token="fixture",
                server_id=self.snapshot.server_id,
                project=self.project,
                data_volume="preview-data",
                database_volume="preview-db",
                logs_volume="preview-logs",
                database_name=self.database,
                snapshot=self.snapshot,
                apply=False,
                dispatch_guard=lambda dispatch: dispatch(),
            )
        self.assertIs(type(caught.exception), expected)

    def execute_script(self, script: str) -> str:
        output = io.StringIO()
        self.script_output = output
        with (
            patch("pathlib.Path.home", return_value=self.root),
            patch("subprocess.run", side_effect=self.command),
            patch("time.sleep"),
            redirect_stdout(output),
        ):
            from tests.support.host_package import execute_host_schedule

            execute_host_schedule(script)
        return output.getvalue()

    def command(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(arguments)
        output = b""
        if arguments[0] in {"ssh", "rsync"}:
            self.fail("A preview must not read an upstream source")
        if arguments[0] == "cp":
            self.fail("A preview must not copy its filestore")
        elif arguments[:2] == ["docker", "cp"]:
            self.assertEqual(Path(arguments[2]), self.source / "database.dump")
        elif arguments[:2] == ["docker", "ps"]:
            output = (
                b"web\nscript-runner"
                if any(value.startswith("volume=") for value in arguments)
                else "\n".join(self.containers).encode()
            )
            if self.container_inspection_error:
                output += b"\nretired-preview"
        elif arguments[:2] == ["docker", "inspect"]:
            output = json.dumps(
                [
                    {
                        "Internal": False,
                        "Labels": {"com.docker.compose.project": self.project},
                        "Containers": {role: {} for role in ("web", "database", "script-runner")},
                    }
                    if arguments[-1] == "fixture-data"
                    else self.containers[arguments[-1]]
                ]
                if len(arguments) == 3
                else [
                    self.containers[identifier]
                    for identifier in arguments[2:]
                    if identifier in self.containers
                ]
            ).encode()
            if self.container_inspection_error:
                result = subprocess.CompletedProcess(
                    arguments, 1, stdout=output, stderr=self.container_inspection_error
                )
                if kwargs.get("check"):
                    result.check_returncode()
                return result
        elif arguments[:3] == ["docker", "volume", "create"]:
            self.volume_options[arguments[-1]] = dict(
                arguments[index + 1].split("=", 1)
                for index, value in enumerate(arguments[:-1])
                if value == "--opt"
            )
            self.volume_labels[arguments[-1]] = dict(
                arguments[index + 1].split("=", 1)
                for index, value in enumerate(arguments[:-1])
                if value == "--label"
            )
            self.volumes[arguments[-1]] = self.root / "volumes" / arguments[-1] / "_data"
            self.volumes[arguments[-1]].mkdir(parents=True, exist_ok=True)
        elif arguments[:3] == ["docker", "volume", "ls"]:
            names = (
                [
                    name
                    for name in self.volumes
                    if "io.launchplane.preview-snapshot" in self.volume_labels.get(name, {})
                ]
                if "label=io.launchplane.preview-snapshot" in arguments
                else list(self.volumes)
            )
            output = "\n".join(names).encode()
        elif arguments[:3] == ["docker", "volume", "inspect"]:
            output = json.dumps(
                [
                    {
                        "Name": arguments[-1],
                        "Driver": "local",
                        "Options": self.volume_options.get(arguments[-1], {}),
                        "Mountpoint": str(self.volumes[arguments[-1]]),
                        "CreatedAt": self.volume_created_at,
                        "Labels": self.volume_labels.get(arguments[-1], {}),
                    }
                ]
            ).encode()
        elif arguments[:2] == ["docker", "pause"]:
            self.paused = True
        elif arguments[:2] == ["docker", "unpause"]:
            self.paused = False
        elif arguments == ["docker", "start", "script-runner"]:
            self.remount_filestore()
        elif arguments[:2] == ["docker", "start"]:
            self.ready_before_web = any(
                json.loads(path.read_text()).get("completed")
                for path in (self.source.parent / "preview-attempts").glob("*.json")
            )
        elif arguments[:2] == ["docker", "exec"]:
            if "dropdb " in " ".join(arguments):
                self.present = False
                self.database_witness = ""
            elif "psql " in " ".join(arguments):
                sql = kwargs.get("input", b"")
                if b"pg_database" in sql:
                    output = b"1" if self.present else b"0"
                elif b"to_regclass" in sql:
                    if self.witness_probe_failure:
                        return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"")
                    output = b"t"
                elif b"SELECT value" in sql:
                    output = self.database_witness.encode()
                else:
                    self.fail("Unexpected preview SQL probe")
            elif arguments[-2:] in (["id", "-u"], ["id", "-g"]):
                output = str(
                    self.source.stat().st_uid
                    if arguments[-1] == "-u"
                    else self.source.stat().st_gid
                ).encode()
            elif kwargs.get("input") in (
                b"fixture-readback",
                INTEGRATION_READBACK_PROGRAM.encode(),
            ):
                output = b"integration_readback_ok=true" if self.sanitized else b""
            elif kwargs.get("input"):
                self.assertFalse(self.paused)
                with (self.source.parent / "snapshot.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        self.module_lock_available = True
                if self.killed_during_restore:
                    self.present = True
                    self.database_witness = self.snapshot.snapshot_id
                    return subprocess.CompletedProcess(arguments, 137, stdout=b"", stderr=b"")
                if arguments[-3] == "resume" and self.database_witness != arguments[-2]:
                    return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"")
                if self.runner_failure:
                    return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"")
                self.present = True
                self.database_witness = arguments[-2]
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

    def test_real_readback_program_materializes_with_the_snapshot_transport(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetPolicies
        from control_plane.integration_readback import integration_readback_policy

        policy = integration_readback_policy(
            instance_name="pr-23", policies=DokployTargetPolicies(), workflow_mode="restore"
        )
        self.run_script(
            readback_program=INTEGRATION_READBACK_PROGRAM,
            readback_spec=policy.encoded_spec(),
        )
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_preview_uses_shared_frozen_bytes_and_sanitizes_before_web(self) -> None:
        output = self.run_script()
        self.assertIn("clean_preview_snapshot_restored=true", output.splitlines())
        self.assertEqual(
            (self.mounted_filestore / "attachment").read_bytes(),
            (self.source / "filestore/attachment").read_bytes(),
        )
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_preparation_allocates_overlay_and_retains_its_original_source(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        created = self.prepare_filestore(snapshot=self.snapshot)
        self.assertEqual(created["type"], "complete")
        options = dict(
            field.split("=", 1)
            for field in created["declaration"]["driver_opts"]["o"].split(",")
            if "=" in field
        )
        self.assertEqual(Path(options["lowerdir"]), self.source / "filestore")
        self.assertEqual(Path(options["upperdir"]), self.private / "upper")
        self.calls.clear()
        newer = self.snapshot.model_copy(update={"snapshot_id": "b" * 64})
        reused = self.prepare_filestore(snapshot=newer)
        self.assertEqual(reused, created)
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        self.volumes.pop("preview-data-filestore")
        self.assertEqual(self.prepare_filestore(snapshot=newer, require_existing=True), created)
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        with patch("pathlib.Path.home", return_value=self.root):
            library["retire_clean_snapshots"](
                {
                    "copy_binding_sha256": "c" * 64,
                    "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
                }
            )
            self.assertTrue(self.source.is_dir())
            self.volumes.pop("preview-data-filestore")
            for path in self.volumes.values():
                shutil.rmtree(path)
            self.volumes.clear()
            library["retire_clean_snapshots"](
                {
                    "copy_binding_sha256": "c" * 64,
                    "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
                }
            )
        self.assertFalse(self.source.exists())

    def test_preparation_uses_docker_full_container_identities(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        command = self.command
        identities = {role: hashlib.sha256(role.encode()).hexdigest() for role in self.containers}

        def realistic_ids(
            arguments: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[:2] == ["docker", "ps"]:
                width = 64 if "--no-trunc" in arguments else 12
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout="\n".join(value[:width] for value in identities.values()).encode(),
                    stderr=b"",
                )
            if arguments[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=json.dumps(
                        [
                            {**container, "Id": identities[role]}
                            for role, container in self.containers.items()
                            if any(
                                identities[role].startswith(identifier)
                                for identifier in arguments[2:]
                            )
                        ]
                    ).encode(),
                    stderr=b"",
                )
            if arguments[:2] == ["docker", "exec"]:
                arguments = list(arguments)
                arguments[3] = next(
                    role for role, identity in identities.items() if identity == arguments[3]
                )
            return command(arguments, **kwargs)

        with patch.object(self, "command", side_effect=realistic_ids):
            self.assertEqual(
                self.prepare_filestore(snapshot=self.snapshot, apply=False)["type"], "complete"
            )

    def test_declared_overlay_before_first_allocation_can_resume(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        result = self.prepare_filestore(snapshot=self.snapshot, require_existing=True)
        self.assertEqual(result["type"], "complete")
        self.assertIsNotNone(result["declaration"])
        self.remount_filestore()
        self.run_script()
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_fresh_generation_preserves_surviving_upper_before_any_data_changes(self) -> None:
        attachment = self.private / "upper" / "staff-attachment"
        attachment.write_bytes(b"staff writes")
        with self.assertRaises(CopyReadbackRefused) as raised:
            self.run_script()
        self.assertEqual(raised.exception.predicate, ReadbackPredicate.PRIVATE_STATE)
        self.assertNotIn(["docker", "stop", "web"], self.calls)
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        self.calls.clear()
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, require_existing=True)["type"], "failed"
        )
        self.assertFalse(
            (self.source.parent / "preview-attempts").exists()
            and list((self.source.parent / "preview-attempts").glob("*.json"))
        )
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        self.assertEqual(attachment.read_bytes(), b"staff writes")

    def test_preparation_preserves_legacy_database_and_filestore(self) -> None:
        self.volumes.pop("preview-data-filestore")
        legacy = self.data / "filestore" / self.database / "legacy-attachment"
        legacy.parent.mkdir()
        legacy.write_bytes(b"needed legacy data")
        self.present = True
        result = self.prepare_filestore(snapshot=self.snapshot)
        self.assertEqual(result["type"], "failed")
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, require_existing=True)["type"],
            "failed",
        )
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        self.present = False
        result = self.prepare_filestore(snapshot=self.snapshot)
        self.assertEqual(result["type"], "failed")
        self.assertEqual(legacy.read_bytes(), b"needed legacy data")

    def test_hidden_legacy_filestore_cannot_become_disposable_restore_evidence(self) -> None:
        legacy = self.data / "filestore" / self.database / "legacy-attachment"
        legacy.parent.mkdir()
        legacy.write_bytes(b"only retained legacy attachment")
        self.assertEqual(self.prepare_filestore(snapshot=self.snapshot)["type"], "failed")
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertEqual(legacy.read_bytes(), b"only retained legacy attachment")
        self.assertFalse((self.source.parent / "preview-attempts").exists())
        self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))

    def test_overlay_declaration_does_not_admit_an_unknown_database(self) -> None:
        self.present = True
        attachment = self.mounted_filestore / "attachment"
        original = attachment.read_bytes()
        result = self.prepare_filestore(snapshot=self.snapshot, apply=False)
        self.assertEqual(result["type"], "failed")
        self.assertEqual(attachment.read_bytes(), original)
        self.assertFalse((self.source.parent / "preview-attempts").exists())
        self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))

    def test_completed_native_custody_preserves_edits_with_a_newer_snapshot(self) -> None:
        self.run_script()
        attachment = self.mounted_filestore / "staff-attachment"
        attachment.write_bytes(b"staff edits after native completion")
        newer = self.snapshot.model_copy(update={"snapshot_id": "b" * 64})
        self.calls.clear()
        result = self.prepare_filestore(snapshot=newer, apply=False)
        self.assertEqual(result["type"], "complete")
        self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))
        self.run_script(snapshot=newer)
        self.assertEqual(attachment.read_bytes(), b"staff edits after native completion")
        self.assertFalse(any("dropdb " in " ".join(call) for call in self.calls))
        self.assertFalse(any(call[:2] == ["docker", "cp"] for call in self.calls))

    def test_interrupted_restore_requires_disposal_before_rebuilding(self) -> None:
        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        self.assert_native_refusal(
            self.prepare_filestore(snapshot=self.snapshot, apply=False),
            PreviewRestoreDisposalRequired,
        )
        newer = self.snapshot.model_copy(update={"snapshot_id": "b" * 64})
        self.assert_native_refusal(
            self.prepare_filestore(snapshot=newer, apply=False),
            PreviewRestoreDisposalRequired,
        )
        attempt = next((self.source.parent / "preview-attempts").glob("*.json"))
        legacy = json.loads(attempt.read_text())
        legacy.pop("started")
        attempt.write_text(json.dumps(legacy))
        self.assert_native_refusal(
            self.prepare_filestore(snapshot=self.snapshot, apply=False),
            PreviewRestoreCustodyUnknown,
        )
        self.calls.clear()
        self.assert_native_refusal(
            self.prepare_filestore(snapshot=newer, apply=False),
            PreviewRestoreCustodyUnknown,
        )
        with self.assertRaises(RuntimeError):
            self.run_script(snapshot=newer)
        self.assertTrue(self.present)
        self.assertFalse(any("dropdb " in " ".join(call) for call in self.calls))
        self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))

    def test_native_transport_preserves_interrupted_restore_classification(self) -> None:
        from control_plane.dokploy import native_copy_stream

        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        receipt = next((self.source.parent / "preview-attempts").glob("*.json"))
        for legacy, refusal in (
            (False, PreviewRestoreDisposalRequired),
            (True, PreviewRestoreCustodyUnknown),
        ):
            with self.subTest(legacy=legacy):
                if legacy:
                    retained = json.loads(receipt.read_text())
                    retained.pop("started")
                    receipt.write_text(json.dumps(retained))
                frame = self.prepare_filestore(snapshot=self.snapshot, apply=False)
                self.calls.clear()
                for candidate, expected in (
                    (frame, refusal),
                    ({**frame, "unexpected": True}, click.ClickException),
                    ({**frame, "declaration": None}, click.ClickException),
                    ({"type": "complete"}, click.ClickException),
                    (
                        {"type": "complete", "restore_disposition": "unknown"},
                        click.ClickException,
                    ),
                    ({"type": "failed"}, native_copy_stream.NativeProgramFailed),
                ):
                    with self.subTest(frame=candidate):
                        self.assert_native_refusal(candidate, expected)

    def test_interrupted_receipt_never_qualifies_shared_or_hidden_data(self) -> None:
        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        self.containers["foreign"] = {
            "Id": "foreign",
            "Config": {"Labels": {"com.docker.compose.project": "another-project"}},
            "Mounts": [{"Type": "volume", "Name": "preview-data-filestore"}],
        }
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, apply=False), {"type": "failed"}
        )
        del self.containers["foreign"]
        attachment = self.data / "filestore" / "needed-attachment"
        attachment.write_bytes(b"retain this data")
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, apply=False), {"type": "failed"}
        )
        self.assertEqual(attachment.read_bytes(), b"retain this data")

    def test_receipt_alone_never_admits_changed_or_missing_native_data(self) -> None:
        self.run_script()
        marker = self.database_witness
        mount = self.containers["database"]["Mounts"][0]
        source = mount["Source"]
        for failure in ("marker", "unreadable", "database", "binding", "container_mount"):
            with self.subTest(failure=failure):
                self.database_witness = "foreign-copy" if failure == "marker" else marker
                self.witness_probe_failure = failure == "unreadable"
                self.present = failure != "database"
                mount["Source"] = (
                    str(self.root / "foreign-database") if failure == "container_mount" else source
                )
                created = self.volume_created_at
                if failure == "binding":
                    self.volume_created_at += "-replacement"
                self.calls.clear()
                self.assertEqual(
                    self.prepare_filestore(snapshot=self.snapshot, apply=False)["type"], "failed"
                )
                self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))
                self.volume_created_at = created

    def test_refused_preparation_never_allocates_or_pins_storage(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        for refusal in ("disk", "receipt"):
            with self.subTest(refusal=refusal):
                snapshot = (
                    self.snapshot
                    if refusal == "disk"
                    else self.snapshot.model_copy(update={"database_sha256": "b" * 64})
                )
                with patch(
                    "os.statvfs",
                    return_value=SimpleNamespace(
                        f_bavail=0 if refusal == "disk" else 2**40, f_frsize=1
                    ),
                ):
                    self.assertEqual(self.prepare_filestore(snapshot=snapshot)["type"], "failed")
                self.assertFalse(
                    any(call[:3] == ["docker", "volume", "create"] for call in self.calls)
                )
                self.assertFalse((self.source.parent / "preview-attempts").exists())

    def test_preparation_charges_the_dump_without_testing_filestore_staging(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        budget = self.snapshot.storage_budget.model_copy(update={"staging_bytes": 2**20})
        snapshot = self.snapshot.model_copy(update={"storage_budget": budget})
        required = (
            budget.database_bytes
            + (self.source / "database.dump").stat().st_size
            + budget.reserve_bytes
        )
        for available in (required - 1, required):
            with (
                self.subTest(available=available),
                patch("os.statvfs", return_value=SimpleNamespace(f_bavail=available, f_frsize=1)),
            ):
                result = self.prepare_filestore(snapshot=snapshot, apply=False)
                self.assertEqual(result["type"], "complete" if available == required else "failed")
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        self.assertFalse((self.source.parent / "preview-attempts").exists())

    def test_preparation_refuses_a_dump_outside_its_bounds_or_custody(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        dump = self.source / "database.dump"
        original = dump.read_bytes()
        dump.write_bytes(b"x" * (self.snapshot.storage_budget.database_bytes + 1))
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, apply=False)["type"], "failed"
        )
        dump.write_bytes(original)
        retained = dump.with_name("retained.dump")
        dump.rename(retained)
        dump.symlink_to(retained)
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, apply=False)["type"], "failed"
        )
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))

    def test_missing_retained_overlay_refuses_instead_of_hiding_attachments(self) -> None:
        self.volumes.pop("preview-data-filestore")
        legacy = self.data / "filestore" / self.database / "legacy-attachment"
        legacy.parent.mkdir()
        legacy.write_bytes(b"needed legacy data")
        for snapshot in (None, self.snapshot):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(
                    self.prepare_filestore(snapshot=snapshot, require_existing=True)["type"],
                    "failed",
                )

    def test_completed_host_restore_cannot_reconstruct_a_missing_declared_overlay(self) -> None:
        self.run_script()
        self.volumes.pop("preview-data-filestore")
        self.calls.clear()
        self.assertEqual(
            self.prepare_filestore(snapshot=self.snapshot, require_existing=True)["type"],
            "failed",
        )
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))

    def test_preparation_tolerates_only_missing_container_inventory_errors(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        for error in (b"Error: No such container: retired-preview\n", b"daemon unavailable\n"):
            with self.subTest(error=error):
                self.container_inspection_error = error
                result = self.prepare_filestore(snapshot=self.snapshot, apply=False)
                self.assertEqual(result["type"], "complete" if b"No such" in error else "failed")

    def test_interrupted_legacy_restore_preserves_writes_without_an_overlay(self) -> None:
        self.sanitized = False
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.sanitized = True
        self.volumes.pop("preview-data-filestore")
        for role in ("web", "script-runner"):
            self.containers[role]["Mounts"].pop()
        attachment = self.mounted_filestore / "attempt-attachment"
        attachment.write_bytes(b"attachment created during module update")
        self.calls.clear()
        result = self.prepare_filestore(snapshot=self.snapshot)
        self.assert_native_refusal(result, PreviewRestoreDisposalRequired)
        self.assert_native_refusal(
            self.prepare_filestore(snapshot=self.snapshot, require_existing=True),
            PreviewRestoreDisposalRequired,
        )
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertEqual(attachment.read_bytes(), b"attachment created during module update")
        self.assertFalse(any("dropdb " in " ".join(call) for call in self.calls))
        self.assertNotIn(["docker", "start", "web"], self.calls)

    def test_overlay_plan_has_no_application_storage_effects(self) -> None:
        self.volumes.pop("preview-data-filestore")
        shutil.rmtree(self.data / "filestore")
        plan = self.prepare_filestore(snapshot=self.snapshot, apply=False)
        self.assertEqual(plan["type"], "complete")
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))
        self.assertFalse((self.source.parent / "preview-attempts").exists())

    def test_interrupted_attempt_never_reconstructs_missing_overlay(self) -> None:
        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        self.volumes.pop("preview-data-filestore")
        for role in ("web", "script-runner"):
            self.containers[role]["Mounts"].pop()
        result = self.prepare_filestore(snapshot=self.snapshot)
        self.assert_native_refusal(result, PreviewRestoreDisposalRequired)
        self.assertFalse(any(call[:3] == ["docker", "volume", "create"] for call in self.calls))

    def test_foreign_overlay_or_aliased_upper_refuses_before_data_changes(self) -> None:
        self.overlay_options["o"] += ",upperdir=/another-preview"
        self.assertEqual(self.prepare_filestore(snapshot=None)["type"], "failed")
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "stop", "web"], self.calls)

    def test_foreign_volume_readback_batches_and_preserves_consumer_checks(self) -> None:
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(VOLUME_BACKING_INSPECTION_LIBRARY, library)
        foreign = {
            "Id": "foreign",
            "Mounts": [
                {"Type": "volume", "Name": name, "Source": str(self.root / name)}
                for name in ("foreign-a", "foreign-b")
            ],
        }
        cases: tuple[tuple[list[dict[str, Any]], bytes, ReadbackPredicate | None], ...] = (
            (
                [],
                b"Error response from daemon: volume foreign-a not found\nError response from daemon: volume foreign-b not found\n",
                None,
            ),
            ([], b"daemon unavailable\n", ReadbackPredicate.BACKING_STORAGE),
            (
                [
                    {"Name": "foreign-a", "Mountpoint": "", "Options": {}},
                    {"Name": "foreign-b", "Mountpoint": str(self.root), "Options": {}},
                ],
                b"",
                ReadbackPredicate.BACKING_STORAGE,
            ),
            (
                [
                    {
                        "Name": "foreign-a",
                        "Mountpoint": str(self.root / "alias"),
                        "Options": {"device": str(self.data)},
                    },
                    {"Name": "foreign-b", "Mountpoint": str(self.root), "Options": {}},
                ],
                b"",
                ReadbackPredicate.STORAGE_CONSUMERS,
            ),
        )
        for rows, stderr, predicate in cases:
            with self.subTest(predicate=predicate):
                result = subprocess.CompletedProcess(
                    [], int(bool(stderr)), stdout=json.dumps(rows).encode(), stderr=stderr
                )
                with patch("subprocess.run", return_value=result) as batch:
                    if predicate is None:
                        self.assertIn(
                            "preview-data",
                            library["inspect_volume_backings"]({"preview-data"}, [foreign], set()),
                        )
                    else:
                        with self.assertRaises(library["CopyReadbackRefused"]) as refused:
                            library["inspect_volume_backings"]({"preview-data"}, [foreign], set())
                        self.assertEqual(refused.exception.predicate, predicate)
                batch.assert_called_once()

    def test_private_preview_render_passes_snapshot_runtime_inspection(self) -> None:
        rendered = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=self.image,
                hold_web_until_integration_readback=True,
                publish_host_ports=False,
                mount_workflow_ssh=False,
            )
        )
        for role in ("web", "script-runner"):
            self.containers[role]["HostConfig"] = {
                "ExtraHosts": rendered["services"][role].get("extra_hosts", [])
            }
        self.run_script()
        self.assertFalse(any(call[0] == "cp" for call in self.calls))
        self.assertTrue(self.ready_before_web)

    def service_schedule(
        self, snapshot: CleanTestingSnapshot, *, elapsed_seconds: int = 0
    ) -> dict[str, str]:
        environment = {
            "ODOO_DB_NAME": self.database,
            "ODOO_DATA_VOLUME": "preview-data",
            "ODOO_DB_VOLUME": "preview-db",
            "ODOO_LOG_VOLUME": "preview-logs",
            "ODOO_INSTALL_MODULES": "website",
        }

        def schedule(**arguments: Any) -> dict[str, str]:
            if elapsed_seconds > arguments["resolved_schedule_execution_timeout_seconds"]:
                raise TimeoutError("The snapshot job outlived its observation budget")
            output = self.execute_script(arguments["schedule_payload"]["script"])
            logs: list[api.JsonValue] = list(output.splitlines())
            return {
                "log_available": "true",
                **post_deploy.extract_odoo_post_deploy_readback_markers({"logs": logs}),
            }

        with (
            patch("control_plane.dokploy.host_package.ensure_schedule_host_package"),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"env": api.serialize_dokploy_env_text(environment)},
            ),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(
                post_deploy,
                "_resolve_dokploy_schedule_runtime",
                return_value=(
                    "dokploy-server",
                    self.snapshot.server_id,
                    self.project,
                    self.snapshot.server_id,
                ),
            ),
            patch.object(
                post_deploy,
                "resolve_upstream_restore_workflow_environment",
                side_effect=AssertionError("A snapshot preview cannot resolve a production reader"),
            ),
            patch.object(post_deploy, "INTEGRATION_READBACK_PROGRAM", "fixture-readback"),
            patch.object(post_deploy, "run_manual_data_schedule", side_effect=schedule),
        ):
            evidence = post_deploy.run_compose_post_deploy_update(
                host="https://provider.invalid",
                token="fixture",
                target_definition=DokployTargetDefinition(
                    context=self.snapshot.context, instance="pr-23", target_id="fixture-preview"
                ),
                env_file=None,
                clean_preview_snapshot=snapshot,
                clean_preview_image_reference=self.image,
            )
        return evidence

    def test_service_schedule_uses_the_snapshot_without_resolving_an_upstream(self) -> None:
        evidence = self.service_schedule(self.snapshot)
        self.assertEqual(evidence["clean_preview_snapshot_restored"], "true")
        self.assertEqual(evidence["clean_preview_snapshot_id"], self.snapshot.snapshot_id)
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_snapshot_restore_outlives_the_deployment_wait_budget(self) -> None:
        evidence = self.service_schedule(
            self.snapshot,
            elapsed_seconds=DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS + 1,
        )
        self.assertEqual(evidence["clean_preview_snapshot_restored"], "true")
        self.assertTrue(self.ready_before_web)

    def test_completed_restore_retry_records_actual_and_requested_snapshot_provenance(self) -> None:
        self.run_script()
        requested = self.snapshot.model_copy(update={"snapshot_id": "b" * 64})
        evidence = self.service_schedule(requested)
        self.assertEqual(evidence["clean_preview_snapshot_id"], self.snapshot.snapshot_id)
        self.assertEqual(evidence["clean_preview_snapshot_requested_id"], requested.snapshot_id)
        self.assertEqual(evidence["clean_preview_snapshot_retained_attempt"], "true")

    def test_foreign_or_unreadable_database_witness_never_resets_data(self) -> None:
        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        self.database_witness = "foreign-copy"
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.witness_probe_failure = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertTrue(self.present)
        self.assertFalse(any("dropdb " in " ".join(call) for call in self.calls))

    def test_ready_data_without_a_witness_is_preserved(self) -> None:
        self.run_script()
        attachment = self.mounted_filestore / "attachment"
        attachment.write_bytes(b"Client edit")
        self.database_witness = ""
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertEqual(attachment.read_bytes(), b"Client edit")
        self.assertFalse(any("dropdb " in " ".join(call) for call in self.calls))

    def test_malformed_committed_attempt_defers_all_retirement(self) -> None:
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        attempts = self.source.parent / "preview-attempts"
        attempts.mkdir()
        (attempts / ("c" * 64 + ".json")).write_text("{}")
        with patch("pathlib.Path.home", return_value=self.root):
            library["retire_clean_snapshots"](
                {
                    "copy_binding_sha256": "b" * 64,
                    "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
                }
            )
        self.assertTrue(self.source.exists())

    def test_ready_receipt_is_durable_before_web_is_released(self) -> None:
        self.run_script()
        self.assertTrue(self.ready_before_web)

    def test_interrupted_obsolete_retirement_is_resumable_and_nonfatal(self) -> None:
        self.volumes.pop("preview-data-filestore")
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        spec = {
            "copy_binding_sha256": "b" * 64,
            "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
        }

        def interrupt(path: Path) -> None:
            (path / "database.dump").unlink()
            raise OSError("fixture interrupted deletion")

        with patch("pathlib.Path.home", return_value=self.root):
            with patch("shutil.rmtree", side_effect=interrupt):
                library["retire_clean_snapshots"](spec)
            library["retire_clean_snapshots"](spec)
        self.assertFalse(self.source.exists())

    def test_atomic_writer_temporary_file_does_not_block_snapshot_retirement(self) -> None:
        self.volumes.pop("preview-data-filestore")
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        attempts = self.source.parent / "preview-attempts"
        attempts.mkdir()
        temporary = attempts / ".pending-fixture"
        temporary.write_text("incomplete atomic metadata")
        with patch("pathlib.Path.home", return_value=self.root):
            library["retire_clean_snapshots"](
                {
                    "copy_binding_sha256": "b" * 64,
                    "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
                }
            )
        self.assertFalse(self.source.exists())
        self.assertTrue(temporary.exists())

    def test_module_update_does_not_hold_the_shared_source_lock(self) -> None:
        self.run_script()
        self.assertTrue(self.module_lock_available)

    def test_same_target_refuses_another_snapshot_before_inspecting_runtime(self) -> None:
        command = self.command
        checked = False

        def concurrent(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            nonlocal checked
            if not checked:
                checked = True
                with self.assertRaises(BlockingIOError):
                    self.run_script(
                        snapshot=self.snapshot.model_copy(update={"snapshot_id": "b" * 64})
                    )
                self.assertFalse(self.calls)
                with (
                    patch.object(
                        self, "command", side_effect=RuntimeError("independent inspection")
                    ),
                    self.assertRaisesRegex(RuntimeError, "independent inspection"),
                ):
                    self.run_script(compose_project="another-preview")
            return command(arguments, **kwargs)

        with patch.object(self, "command", side_effect=concurrent):
            self.run_script()
        self.assertTrue(checked)

    def test_orphaned_container_exec_blocks_restore_until_it_exits(self) -> None:
        runner = self.containers["script-runner"]
        for active in (["surviving-restore"], "unreadable"):
            with self.subTest(active=active):
                runner["ExecIDs"] = active
                with self.assertRaisesRegex(RuntimeError, "Preview runner execution custody"):
                    self.run_script()
                self.assertNotIn(["docker", "stop", "web"], self.calls)
        runner.pop("ExecIDs")
        with self.assertRaisesRegex(RuntimeError, "Preview runner execution custody"):
            self.run_script()
        runner["ExecIDs"] = None
        self.run_script()
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_transient_health_probe_finishes_before_restore(self) -> None:
        command = self.command
        runner = self.containers["script-runner"]
        runner["ExecIDs"] = ["health-probe"]
        observations = 0

        def health_probe(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            nonlocal observations
            if arguments == ["docker", "inspect", "script-runner"]:
                observations += 1
                if observations > 2:
                    runner["ExecIDs"] = []
            return command(arguments, **kwargs)

        with patch.object(self, "command", side_effect=health_probe):
            self.run_script()
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_retry_recovers_paused_runner_before_exec_or_copy(self) -> None:
        command = self.command
        runner = self.containers["script-runner"]
        runner["State"] = {"Status": "paused"}
        self.paused = True

        def paused_runner(
            arguments: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments[:2] == ["docker", "exec"] and "script-runner" in arguments:
                self.assertFalse(self.paused)
            return command(arguments, **kwargs)

        with patch.object(self, "command", side_effect=paused_runner):
            self.run_script()
        self.assertLess(
            self.calls.index(["docker", "unpause", "script-runner"]),
            self.calls.index(["docker", "stop", "web"]),
        )

    def test_runner_failure_reports_only_closed_diagnostics_and_keeps_web_stopped(self) -> None:
        original = self.command
        stage = CopyFailureStage.MODULE_UPDATE
        exit_status = 7

        def refuse(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if arguments[-1:] == ["clean-snapshot"]:
                return subprocess.CompletedProcess(
                    arguments,
                    exit_status,
                    stdout=(
                        f"{COPY_FAILURE_STAGE_MARKER}={stage}\n"
                        f"{COPY_FAILURE_STAGE_MARKER}=private-fixture-detail\n"
                        "password=private-fixture-detail\n"
                    ).encode(),
                    stderr=b"private-fixture-detail",
                )
            return original(arguments, **kwargs)

        with (
            patch.object(self, "command", side_effect=refuse),
            self.assertRaises(RuntimeError),
        ):
            self.run_script()
        self.assertIn(
            f"{COPY_FAILURE_STAGE_MARKER}={stage}", self.script_output.getvalue().splitlines()
        )
        self.assertIn(
            f"{COPY_FAILURE_EXIT_MARKER}={exit_status}", self.script_output.getvalue().splitlines()
        )
        self.assertNotIn("private-fixture-detail", self.script_output.getvalue())
        self.assertNotIn(["docker", "start", "web"], self.calls)
        evidence = post_deploy.extract_odoo_post_deploy_readback_markers(
            {"logs": self.script_output.getvalue()}
        )
        self.assertEqual(evidence[COPY_FAILURE_STAGE_MARKER], stage)
        self.assertEqual(evidence[COPY_FAILURE_EXIT_MARKER], str(exit_status))
        evidence = post_deploy.extract_odoo_post_deploy_readback_markers(
            {
                "logs": self.script_output.getvalue()
                + f"{COPY_FAILURE_STAGE_MARKER}={CopyFailureStage.RESTORE}\n"
                + f"{COPY_FAILURE_EXIT_MARKER}=-9\n"
                + f"{COPY_FAILURE_STAGE_MARKER}=private-fixture-detail\n"
                + f"{COPY_FAILURE_EXIT_MARKER}=private-fixture-detail\n"
            }
        )
        self.assertEqual(evidence[COPY_FAILURE_STAGE_MARKER], stage)
        self.assertEqual(evidence[COPY_FAILURE_EXIT_MARKER], str(exit_status))
        self.assertNotIn("private-fixture-detail", str(evidence))

    def test_foreign_readonly_mount_cannot_reach_private_copy_failure_evidence(self) -> None:
        self.containers["foreign"] = {
            "Id": "foreign",
            "Config": {"Labels": {"com.docker.compose.project": "another-project"}},
            "Mounts": [{"Source": str(self.root / COPY_FAILURE_STATE_DIRECTORY), "RW": False}],
        }
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "stop", "web"], self.calls)

    def test_failed_runner_retains_private_traceback_without_publishing_it(self) -> None:
        original = self.command
        child_stdout = b"copy_failure_stage=module_update\nprivate fixture output\n"
        child_stderr = b"Traceback (most recent call last):\nValueError: private fixture cause\n"

        def refuse(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if arguments[-1:] == ["clean-snapshot"]:
                return subprocess.CompletedProcess(
                    arguments, 1, stdout=child_stdout, stderr=child_stderr
                )
            return original(arguments, **kwargs)

        with (
            patch.object(self, "command", side_effect=refuse),
            self.assertRaises(RuntimeError),
        ):
            self.run_script()
        markers = post_deploy.extract_odoo_post_deploy_readback_markers(
            {"logs": self.script_output.getvalue()}
        )
        evidence_id = markers[COPY_FAILURE_EVIDENCE_MARKER]
        path = self.root / COPY_FAILURE_STATE_DIRECTORY / (evidence_id + ".json")
        evidence = json.loads(path.read_text())
        self.assertEqual(base64.b64decode(evidence["stdout"]), child_stdout)
        self.assertEqual(base64.b64decode(evidence["stderr"]), child_stderr)
        self.assertEqual(evidence["exit_status"], 1)
        self.assertEqual(evidence["stage"], CopyFailureStage.MODULE_UPDATE)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("private fixture", self.script_output.getvalue())
        self.assertNotIn(["docker", "start", "web"], self.calls)

    def test_every_command_inherits_target_lock_and_child_blocks_retry_after_failure(self) -> None:
        command = self.command
        child: subprocess.Popen[bytes] | None = None
        self.runner_failure = True

        def inherited(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            nonlocal child
            descriptors = kwargs.get("pass_fds", ())
            self.assertTrue(descriptors, arguments)
            if arguments[-1:] == ["clean-snapshot"]:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('ready', flush=True); sys.stdin.read()",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    pass_fds=descriptors,
                )
                self.assertIsNotNone(child.stdout)
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline(), b"ready\n")
            return command(arguments, **kwargs)

        try:
            with (
                patch.object(self, "command", side_effect=inherited),
                self.assertRaises(RuntimeError),
            ):
                self.run_script()
            self.assertIsNotNone(child)
            self.calls.clear()
            with self.assertRaises(BlockingIOError):
                self.run_script()
            self.assertFalse(self.calls)
        finally:
            if child is not None:
                child.communicate(timeout=10)
        self.runner_failure = False
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "start", "web"], self.calls)

    def test_abandoned_attempt_releases_snapshot_only_after_every_backing_is_gone(self) -> None:
        self.runner_failure = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        spec = {
            "copy_binding_sha256": "b" * 64,
            "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
        }
        with patch("pathlib.Path.home", return_value=self.root):
            inspect = library["docker"]
            library["docker"] = Mock(side_effect=RuntimeError("fixture unavailable inventory"))
            library["retire_clean_snapshots"](spec)
            self.assertTrue(self.source.exists())
            library["docker"] = inspect
            shutil.rmtree(self.volumes.pop("preview-logs"))
            library["retire_clean_snapshots"](spec)
            self.assertTrue(self.source.exists())
            for path in self.volumes.values():
                shutil.rmtree(path)
            self.volumes.clear()
            library["retire_clean_snapshots"](spec)
        self.assertFalse(self.source.exists())
        self.assertFalse(list((self.source.parent / "preview-attempts").iterdir()))

    def test_overlay_charges_database_dump_and_reserve_without_another_filestore(self) -> None:
        budget = self.snapshot.storage_budget
        dump = (self.source / "database.dump").stat().st_size
        required = budget.database_bytes + dump + budget.reserve_bytes
        with patch("os.statvfs", return_value=SimpleNamespace(f_bavail=required - 1, f_frsize=1)):
            with self.assertRaises(RuntimeError):
                self.run_script()
        self.assertNotIn(["docker", "stop", "web"], self.calls)
        self.calls.clear()
        with patch("os.statvfs", return_value=SimpleNamespace(f_bavail=required, f_frsize=1)):
            self.run_script()
        self.assertIn(["docker", "start", "web"], self.calls)
        self.assertFalse(any(call[0] == "cp" for call in self.calls))
        self.assertFalse(any("-R" in call for call in self.calls if "chown" in call))

    def test_existing_preview_data_and_staff_edits_are_never_overwritten(self) -> None:
        self.present = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertFalse(any(call[0] == "cp" for call in self.calls))

    def test_failed_sanitizer_or_snapshot_integrity_never_starts_web(self) -> None:
        self.sanitized = False
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "start", "web"], self.calls)
        self.present = False
        self.sanitized = True
        (self.source / "database.dump").write_bytes(b"corrupted snapshot")
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "start", "web"], self.calls)

    def test_destroyed_preview_can_start_again_on_new_storage(self) -> None:
        self.killed_during_restore = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.killed_during_restore = False
        self.remount_filestore()
        self.present = False
        self.volume_created_at = datetime.now(timezone.utc).isoformat()
        self.calls.clear()
        self.run_script()
        self.assertIn(["docker", "start", "web"], self.calls)

    def test_interrupted_restore_preserves_upper_and_legacy_writes(self) -> None:
        self.runner_failure = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        legacy = self.mounted_filestore / "failed-module-attachment"
        upper = self.private / "upper" / "attempt-attachment"
        for attachment in (legacy, upper):
            attachment.write_bytes(b"preserve interrupted writes")
        self.runner_failure = False
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.run_script()
        for attachment in (legacy, upper):
            self.assertEqual(attachment.read_bytes(), b"preserve interrupted writes")
        self.assertFalse(any(call[1] in {"stop", "start", "cp"} for call in self.calls))

    def test_qualified_copy_preserves_edits_after_api_completion_failure(self) -> None:
        self.run_script()
        attachment = self.mounted_filestore / "attachment"
        attachment.write_bytes(b"preview edit")
        self.calls.clear()
        self.sanitized = True
        self.run_script()
        self.assertEqual(attachment.read_bytes(), b"preview edit")
        self.assertFalse(
            any(call[0] == "cp" or call[:2] == ["docker", "cp"] for call in self.calls)
        )

    def test_shared_guard_refuses_privilege_and_snapshot_mount_exposure(self) -> None:
        self.containers["script-runner"]["HostConfig"] = {"Privileged": True}
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.containers["script-runner"].pop("HostConfig")
        self.containers["script-runner"]["Mounts"].append(
            {
                "Type": "bind",
                "Source": str(self.source.parent),
                "Destination": "/snapshot",
                "RW": False,
            }
        )
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.assertNotIn(["docker", "stop", "web"], self.calls)

    def test_full_copy_headroom_is_checked_before_stopping_preview(self) -> None:
        with patch("os.statvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=1)):
            with self.assertRaises(RuntimeError):
                self.run_script()
        self.assertNotIn(["docker", "stop", "web"], self.calls)

    def test_obsolete_snapshot_retirement_preserves_incomplete_reader(self) -> None:
        self.runner_failure = True
        with self.assertRaises(RuntimeError):
            self.run_script()
        self.volumes.pop("preview-data-filestore")
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        attempts = self.source.parent / "preview-attempts"
        attempt = next(attempts.glob("*.json"))
        payload = json.loads(attempt.read_text())
        spec = {
            "copy_binding_sha256": "b" * 64,
            "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
        }
        with patch("pathlib.Path.home", return_value=self.root):
            library["retire_clean_snapshots"](spec)
            self.assertTrue(self.source.is_dir())
            payload["completed"] = True
            payload.pop("overlay_filestore", None)
            attempt.write_text(json.dumps(payload))
            library["retire_clean_snapshots"](spec)
        self.assertFalse(self.source.exists())

    def test_completed_overlay_pins_source_even_after_its_data_backing_is_removed(self) -> None:
        self.run_script()
        self.volumes.pop("preview-data")
        shutil.rmtree(self.data.parent)
        library: dict[str, Any] = {"docker": lambda *args: self.command(["docker", *args]).stdout}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
        spec = {
            "copy_binding_sha256": "b" * 64,
            "clean_snapshot_retirements": [self.snapshot.model_dump(mode="json")],
        }
        with patch("pathlib.Path.home", return_value=self.root):
            library["retire_clean_snapshots"](spec)
            self.assertTrue(self.source.exists())
            self.volumes.pop("preview-data-filestore")
            library["retire_clean_snapshots"](spec)
            self.assertTrue(self.source.exists())
            for path in self.volumes.values():
                shutil.rmtree(path)
            self.volumes.clear()
            library["retire_clean_snapshots"](spec)
        self.assertFalse(self.source.exists())

    def test_production_reader_and_cross_host_fallback_are_refused(self) -> None:
        for updates in (
            {"workflow_environment": {"ODOO_UPSTREAM_HOST": "production.invalid"}},
            {"workflow_environment": {"DATA_WORKFLOW_SSH_DIR": "/existing-reader"}},
            {"server_id": "another-server"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.run_script(**updates)
        self.assertFalse(self.calls)
