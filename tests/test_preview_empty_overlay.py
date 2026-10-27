import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane._preview_preservation_program import PREVIEW_PRESERVATION_PROGRAM
from tests.test_filestore_capture import capture_namespace


class EmptyOverlayPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.namespace = capture_namespace(PREVIEW_PRESERVATION_PROGRAM)
        self.source: dict[str, Any] = {
            "compose_project": "fixture-preview",
            "database_name": "fixture_db",
            "preview_volumes": {role: f"fixture-{role}" for role in ("data", "database", "logs")},
            "provider_binding_sha256": "fixture-provider",
        }
        self.snapshot = {
            "snapshot_id": hashlib.sha256(b"original snapshot").hexdigest(),
            "database_sha256": hashlib.sha256(b"retained database").hexdigest(),
            "filestore_sha256": hashlib.sha256(b"retained attachments").hexdigest(),
        }
        self.lower = (
            self.root
            / self.namespace["CLEAN_SNAPSHOT_STATE_DIRECTORY"]
            / self.snapshot["snapshot_id"]
            / "filestore"
        )
        self.lower.mkdir(parents=True)
        (self.lower / "retained-attachment").write_bytes(b"shared retained bytes")
        (self.lower.parent / "receipt.json").write_text(json.dumps(self.snapshot))
        self.authority = {
            "server_id": "fixture-server",
            "product": "fixture",
            "context": "fixture-testing",
        }
        self.identifier = hashlib.sha256(b"original authority").hexdigest()
        self.namespace.update(
            spec={
                "authority": self.authority,
                "preservation_id": self.identifier,
                "source": self.source,
                "mode": "preserve",
                "snapshot": self.snapshot,
            },
            deadline=time.monotonic() + 60,
        )
        self.volumes: dict[str, Any] = {}
        for role, name in self.source["preview_volumes"].items():
            backing = self.root / "docker-volumes" / name / "_data"
            backing.mkdir(parents=True)
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "CreatedAt": "fixture-created",
                "Mountpoint": str(backing),
                "Labels": {"com.docker.compose.project": self.source["compose_project"]},
            }
        data_name = self.source["preview_volumes"]["data"]
        self.private = (
            Path(self.volumes[data_name]["Mountpoint"]).parent / "launchplane-preview-filestore"
        )
        for child in ("upper", "work/work"):
            (self.private / child).mkdir(parents=True)
        self.backing = Path(self.volumes[data_name]["Mountpoint"])
        (self.backing / "filestore").mkdir()
        (self.backing / "addons/20.0").mkdir(parents=True)
        overlay_name = data_name + "-filestore"
        self.volumes[overlay_name] = {
            "Name": overlay_name,
            "Driver": "local",
            "CreatedAt": "fixture-overlay-created",
            "Mountpoint": str(self.root / "merged"),
            "Options": {
                "type": "overlay",
                "device": "overlay",
                "o": f"lowerdir={self.lower},upperdir={self.private}/upper,workdir={self.private}/work,userxattr",
            },
            "Labels": {
                "com.docker.compose.project": self.source["compose_project"],
                "io.launchplane.preview-snapshot": self.snapshot["snapshot_id"],
            },
        }
        destinations = {
            "database": {"/var/lib/postgresql/data": "database"},
            "web": {
                "/volumes/data": "data",
                "/volumes/logs": "logs",
                f"/volumes/data/filestore/{self.source['database_name']}": "overlay",
            },
            "script-runner": {
                "/volumes/data": "data",
                "/volumes/logs": "logs",
                f"/volumes/data/filestore/{self.source['database_name']}": "overlay",
            },
            "preview-router": {},
        }
        self.containers: dict[str, Any] = {
            service: {
                "Id": service,
                "Image": "sha256:fixture",
                "ExecIDs": None,
                "State": {"Running": service in {"database", "preview-router"}},
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": self.source["compose_project"],
                        "com.docker.compose.service": service,
                    },
                    "Env": ["PLATFORM_INSTANCE=preview", "ODOO_DB_NAME=fixture_db"],
                },
                "HostConfig": {},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": overlay_name
                        if role == "overlay"
                        else self.source["preview_volumes"][role],
                        "Source": self.volumes[
                            overlay_name
                            if role == "overlay"
                            else self.source["preview_volumes"][role]
                        ]["Mountpoint"],
                        "Destination": destination,
                        "RW": True,
                    }
                    for destination, role in mounts.items()
                ],
            }
            for service, mounts in destinations.items()
        }
        self.database_facts: dict[str, Any] = {
            "databases": ["postgres"],
            "connections": 0,
            "relations": 0,
            "prepared": 0,
        }
        self.calls: list[tuple[str, ...]] = []
        self.namespace["docker"] = self.docker
        self.path = self.root / "retained"

    def docker(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        self.calls.append(arguments)
        if arguments[:2] == ("volume", "ls"):
            return "\n".join(self.volumes).encode()
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([self.volumes[arguments[2]]]).encode()
        if arguments[0] == "inspect":
            return json.dumps([self.containers[arguments[1]]]).encode()
        if arguments[0] == "ps":
            if arguments[-1].startswith("volume="):
                name = arguments[-1].removeprefix("volume=")
                return "\n".join(
                    service
                    for service, container in self.containers.items()
                    if any(mount["Name"] == name for mount in container["Mounts"])
                ).encode()
            return "\n".join(self.containers).encode()
        if arguments[0] == "exec":
            self.assertIsNotNone(stdin)
            assert stdin is not None
            self.assertIn(b"BEGIN READ ONLY", stdin)
            return json.dumps(self.database_facts).encode()
        self.fail(f"unexpected provider effect: {arguments}")

    def preserve(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.namespace["preserve"](self.path, self.source))

    def test_empty_overlay_qualifies_without_rewriting_restore_or_copying_shared_lower(
        self,
    ) -> None:
        receipt = self.preserve()
        self.assertTrue(receipt["empty_overlay_verified"])
        self.assertNotIn("database_restore_verified", receipt)
        self.assertFalse((self.path / "database.dump").exists())
        self.assertFalse((self.path / "filestore.tar.gz").exists())
        self.namespace["verify_preservation_source"](receipt, self.source)
        self.assertEqual(
            (self.lower / "retained-attachment").read_bytes(), b"shared retained bytes"
        )
        self.namespace["docker"] = lambda *args, **kwargs: self.fail("provider is absent")
        self.assertEqual(self.namespace["verify_preservation"](self.path), receipt)

    def test_use_or_unique_private_data_never_publishes_empty_custody(self) -> None:
        for key, value in (
            ("databases", ["fixture_db", "postgres"]),
            ("databases", ["foreign", "postgres"]),
            ("connections", 1),
            ("relations", 1),
            ("prepared", 1),
        ):
            with self.subTest(key=key, value=value):
                previous = self.database_facts[key]
                self.database_facts[key] = value
                with self.assertRaises(RuntimeError):
                    self.preserve()
                self.database_facts[key] = previous
                self.assertFalse(self.path.exists())
        for path in (
            self.private / "upper/attachment",
            self.private / "work/private",
            self.backing / "filestore/attachment",
            self.backing / "addons/private",
            self.backing / "sessions",
            Path(self.volumes["fixture-logs"]["Mountpoint"]) / "log",
        ):
            with self.subTest(path=path):
                path.write_bytes(b"only copy")
                with self.assertRaises(RuntimeError):
                    self.preserve()
                self.assertEqual(path.read_bytes(), b"only copy")
                path.unlink()
                self.assertFalse(self.path.exists())

    def test_reopened_source_storage_drift_and_changed_pin_refuse_each_checkpoint(self) -> None:
        receipt = self.preserve()
        self.containers["web"]["State"]["Running"] = True
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation_source"](receipt, self.source)
        self.containers["web"]["State"]["Running"] = False
        self.volumes["fixture-data"]["CreatedAt"] = "replacement"
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation_source"](receipt, self.source)
        self.volumes["fixture-data"]["CreatedAt"] = "fixture-created"
        self.database_facts["databases"].append("fixture_db")
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation_source"](receipt, self.source)
        (self.lower.parent / "receipt.json").write_text("{}")
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation"](self.path)

    def test_same_operation_recovers_interrupted_receipt_without_changing_source(self) -> None:
        writer = self.namespace["write_snapshot_receipt"]

        def interrupt(path: Path, value: object) -> None:
            if path.name == "receipt.json":
                raise TimeoutError("receipt publication interrupted")
            writer(path, value)

        self.namespace["write_snapshot_receipt"] = interrupt
        with self.assertRaises(TimeoutError):
            self.preserve()
        self.assertFalse(self.path.exists())
        self.namespace["inspect_pending_preservation"](self.path, self.source)
        self.namespace["write_snapshot_receipt"] = writer
        receipt = self.preserve()
        self.namespace["verify_preservation_source"](receipt, self.source)
        self.assertFalse(self.path.with_name(".pending-" + self.path.name).exists())

    def test_foreign_consumers_mounts_active_exec_and_missing_pin_stay_held(self) -> None:
        for service in self.containers:
            with self.subTest(service=service):
                self.containers[service]["ExecIDs"] = ["foreign-exec"]
                with self.assertRaises(RuntimeError):
                    self.preserve()
                self.containers[service]["ExecIDs"] = None
        mount = self.containers["web"]["Mounts"][-1]
        previous = mount["Source"]
        mount["Source"] = str(self.root / "foreign-storage")
        with self.assertRaises(RuntimeError):
            self.preserve()
        mount["Source"] = previous
        self.containers["preview-router"]["Mounts"].append(dict(mount))
        with self.assertRaises(RuntimeError):
            self.preserve()
        self.containers["preview-router"]["Mounts"].clear()
        self.namespace["spec"]["snapshot"] = {
            **self.snapshot,
            "snapshot_id": hashlib.sha256(b"other snapshot").hexdigest(),
        }
        with self.assertRaises((RuntimeError, OSError)):
            self.preserve()
        self.assertFalse(self.path.exists())
