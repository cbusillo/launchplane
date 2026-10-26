import argparse
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

import yaml

from control_plane._preview_preservation_program import PREVIEW_PRESERVATION_PROGRAM
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.contracts.idempotency_record import complete_launchplane_mutation_reservation
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope, ODOO_PREVIEW_APPLY_ROUTE
from control_plane.dokploy.compose import render_odoo_raw_compose_file
from control_plane.preview_preservation import (
    read_preview_preservation_authority,
    require_preview_preservation_admission,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_filestore_capture import capture_namespace
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload


class PreviewPreservationProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.namespace = capture_namespace(PREVIEW_PRESERVATION_PROGRAM)
        self.source = {
            "compose_project": "fixture-preview",
            "database_name": "fixture_db",
            "preview_volumes": {
                "data": "fixture-data",
                "database": "fixture-db",
                "logs": "fixture-logs",
            },
            "provider_binding_sha256": "fixture-binding",
        }
        self.authority = {"original_reservation_id": "fixture-original"}
        self.identifier = hashlib.sha256(json.dumps(self.authority).encode()).hexdigest()
        self.spec = {
            "authority": self.authority,
            "preservation_id": self.identifier,
            "source": self.source,
            "mode": "preserve",
            "storage_budget": {
                "database_bytes": 1024,
                "filestore_bytes": 1024,
                "reserve_bytes": 1024,
            },
            "memory_floor": 1024,
        }
        self.paths = {role: self.root / role for role in self.source["preview_volumes"]}
        for path in self.paths.values():
            path.mkdir()
        self.filestore = self.paths["data"] / "filestore"
        (self.filestore / "ab").mkdir(parents=True)
        (self.filestore / "ab/attachment").write_bytes(b"attachment bytes")
        (self.paths["data"] / "addons/19.0").mkdir(parents=True)
        self.binding = {
            "volume_backings": {role: {"path": str(path)} for role, path in self.paths.items()}
        }
        self.containers: dict[str, dict[str, Any]] = {
            role: {
                "Id": role,
                "Image": "sha256:fixture-pg",
                "State": {"Running": True},
                "ExecIDs": None,
            }
            for role in ("database", "web", "script-runner")
        }
        self.calls: list[tuple[str, ...]] = []
        self.path = self.root / "retained"
        self.restored = Mock(return_value=3)
        self.namespace.update(
            spec=self.spec,
            deadline=time.monotonic() + 60,
            docker=self.docker,
            preview_containers=lambda source: self.containers,
            preview_attempt_binding=lambda *args: self.binding,
            observe_memory_headroom=lambda *args: 2048,
            require_unserved_web=Mock(),
            database_query=self.query,
            capture_snapshot_file=self.capture,
            restore_verification=self.restored,
        )

    def docker(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        self.calls.append(arguments)
        if arguments[0] == "stop":
            self.containers[arguments[-1]]["State"]["Running"] = False
        return b""

    def query(self, container: str, database: str, query: bytes) -> str:
        del container, database
        if b"pg_stat_activity" in query:
            return "0"
        if b"pg_database" in query:
            return json.dumps(sorted([self.source["database_name"], "postgres"]))
        return "3"

    def capture(
        self, arguments: list[str], path: Path, bound: int, **kwargs: object
    ) -> tuple[int, bytes, bool]:
        del arguments, bound, kwargs
        path.write_bytes(b"database content")
        return 0, b"", False

    def preserve(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.namespace["preserve"](self.path, self.source))

    def test_copy_is_published_only_after_database_restore_and_attachment_verification(
        self,
    ) -> None:
        receipt = self.preserve()
        self.restored.assert_called_once()
        self.assertTrue(receipt["database_restore_verified"])
        self.assertEqual(receipt["empty_addon_directories"], ["19.0"])
        self.assertEqual((self.path / "database.dump").read_bytes(), b"database content")
        with tarfile.open(self.path / "filestore.tar.gz") as archive:
            stream = archive.extractfile("ab/attachment")
            assert stream is not None
            self.assertEqual(stream.read(), (self.filestore / "ab/attachment").read_bytes())
        self.namespace["verify_preservation_source"](receipt, self.source)
        self.assertEqual(
            [call[-1] for call in self.calls if call[0] == "stop"], ["web", "script-runner"]
        )
        self.assertFalse(self.path.with_name(".pending-" + self.path.name).exists())

    def test_restore_failure_retains_source_and_owned_pending_copy_without_publish(self) -> None:
        self.restored.side_effect = RuntimeError("restore failed")
        with self.assertRaises(RuntimeError) as raised:
            self.preserve()
        self.assertIs(raised.exception, self.restored.side_effect)
        self.assertFalse(self.path.exists())
        self.assertEqual((self.filestore / "ab/attachment").read_bytes(), b"attachment bytes")
        temporary = self.path.with_name(".pending-" + self.path.name)
        self.assertTrue((temporary / "database.dump").is_file())
        self.assertTrue((temporary / "pending.json").is_file())
        self.assertFalse(
            any(call[0] == "rm" or call[:2] == ("volume", "rm") for call in self.calls)
        )

    def test_owned_interrupted_capture_can_resume_without_touching_original(self) -> None:
        self.restored.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.preserve()
        self.restored.side_effect = None
        self.namespace["verification_resources"] = Mock()
        self.namespace["inspect_pending_preservation"](self.path, self.source)
        receipt = self.preserve()
        self.assertTrue(receipt["database_restore_verified"])
        self.assertEqual((self.filestore / "ab/attachment").read_bytes(), b"attachment bytes")

    def test_foreign_pending_copy_is_never_removed(self) -> None:
        temporary = self.path.with_name(".pending-" + self.path.name)
        temporary.mkdir()
        (temporary / "unknown").write_bytes(b"only copy")
        with self.assertRaises(RuntimeError):
            self.preserve()
        self.assertEqual((temporary / "unknown").read_bytes(), b"only copy")
        self.assertTrue((self.filestore / "ab/attachment").exists())
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_owned_receipt_temp_file_does_not_block_capture_recovery(self) -> None:
        self.restored.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.preserve()
        temporary = self.path.with_name(".pending-" + self.path.name)
        (temporary / ".pending-receipt").write_bytes(b"partial receipt")
        self.restored.side_effect = None
        self.namespace["verification_resources"] = Mock()
        self.assertTrue(self.preserve()["database_restore_verified"])

    def test_interrupted_owner_publication_and_cleanup_leave_recoverable_custody(self) -> None:
        writer = self.namespace["write_snapshot_receipt"]
        receipt_failure = RuntimeError("receipt interrupted")

        def interrupt_receipt(path: Path, payload: object) -> None:
            del payload
            (path.parent / ".pending-owner").write_bytes(b"partial metadata")
            raise receipt_failure

        self.namespace["write_snapshot_receipt"] = interrupt_receipt
        with self.assertRaises(RuntimeError) as raised:
            self.preserve()
        self.assertIs(raised.exception, receipt_failure)
        temporary = self.path.with_name(".pending-" + self.path.name)
        self.assertFalse(temporary.exists())
        self.namespace["write_snapshot_receipt"] = writer
        self.restored.side_effect = RuntimeError("restore interrupted")
        with self.assertRaises(RuntimeError) as raised:
            self.preserve()
        self.assertIs(raised.exception, self.restored.side_effect)
        self.namespace["verification_resources"] = Mock()
        unlink = Path.unlink
        cleanup_failure = RuntimeError("cleanup interrupted")

        def interrupt_cleanup(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == temporary / "pending.json":
                raise cleanup_failure
            unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=interrupt_cleanup):
            with self.assertRaises(RuntimeError) as raised:
                self.preserve()
            self.assertIs(raised.exception, cleanup_failure)
        self.assertEqual([path.name for path in temporary.iterdir()], ["pending.json"])
        self.restored.side_effect = None
        self.assertTrue(self.preserve()["database_restore_verified"])
        self.assertEqual((self.filestore / "ab/attachment").read_bytes(), b"attachment bytes")

    def test_additional_data_and_low_capacity_fail_before_stopping_original(self) -> None:
        for additional in ("sessions", "addons/private-key"):
            with self.subTest(additional=additional):
                path = self.paths["data"] / additional
                path.write_bytes(b"unknown data")
                with self.assertRaises(RuntimeError):
                    self.preserve()
                path.unlink()
                self.assertEqual(self.calls, [])
        self.namespace["observe_memory_headroom"] = lambda *args: 0
        with self.assertRaises(RuntimeError):
            self.preserve()
        self.assertEqual(self.calls, [])

    def test_tampering_or_reopened_source_blocks_deletion_proof(self) -> None:
        receipt = self.preserve()
        self.containers["web"]["State"]["Running"] = True
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation_source"](receipt, self.source)
        self.containers["web"]["State"]["Running"] = False
        (self.filestore / "ab/attachment").write_bytes(b"new original data")
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation_source"](receipt, self.source)
        (self.path / "database.dump").write_bytes(b"changed")
        with self.assertRaises(RuntimeError):
            self.namespace["verify_preservation"](self.path)

    def test_complete_copy_remains_verifiable_after_provider_disappears(self) -> None:
        receipt = self.preserve()
        self.namespace["preview_containers"] = Mock(
            side_effect=AssertionError("source disappeared")
        )
        self.assertEqual(self.namespace["verify_preservation"](self.path), receipt)

    def test_archive_aliases_and_changed_attachment_bytes_are_rejected(self) -> None:
        manifest = self.namespace["tree_manifest"](self.filestore)
        archive_path = self.root / "invalid.tar.gz"
        for alias in (False, True):
            with self.subTest(alias=alias):
                with tarfile.open(archive_path, "w:gz") as archive:
                    info = tarfile.TarInfo("ab/attachment")
                    if alias:
                        info.type = tarfile.SYMTYPE
                        info.linkname = "/unknown"
                        archive.addfile(info)
                    else:
                        info.size = len(b"changed")
                        archive.addfile(info, io.BytesIO(b"changed"))
                with self.assertRaises(RuntimeError):
                    self.namespace["verify_filestore"](archive_path, manifest)


class PreviewPreservationContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace = capture_namespace(PREVIEW_PRESERVATION_PROGRAM)
        self.source: dict[str, Any] = {
            "compose_project": "fixture-preview",
            "database_name": "fixture_db",
            "preview_volumes": {
                "database": "fixture-db",
                "data": "fixture-data",
                "logs": "fixture-logs",
            },
        }
        compose = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference="registry.example.test/odoo:fixture",
                hold_web_until_integration_readback=True,
                publish_host_ports=True,
                mount_workflow_ssh=False,
            )
        )
        self.containers: dict[str, dict[str, Any]] = {}
        for role, mounts in {
            "database": {"/var/lib/postgresql/data": "database"},
            "web": {"/volumes/data": "data", "/volumes/logs": "logs"},
            "script-runner": {"/volumes/data": "data", "/volumes/logs": "logs"},
        }.items():
            self.containers[role] = {
                "Id": role,
                "ExecIDs": None,
                "Config": {
                    "Labels": {"com.docker.compose.service": role},
                    "Env": [
                        "PLATFORM_INSTANCE=preview",
                        "ODOO_DB_NAME=" + self.source["database_name"],
                    ],
                },
                "HostConfig": {"ExtraHosts": compose["services"][role].get("extra_hosts")},
                "Mounts": [
                    {
                        "Destination": destination,
                        "Type": "volume",
                        "Name": self.source["preview_volumes"][volume],
                    }
                    for destination, volume in mounts.items()
                ],
            }
        self.namespace.update(docker=self.docker)

    def docker(self, *arguments: str) -> bytes:
        if arguments[0] == "inspect":
            return json.dumps([self.containers[arguments[1]]]).encode()
        if arguments[0] == "ps":
            selector = arguments[-1]
            roles = list(self.containers)
            if selector.startswith("volume="):
                roles = [
                    role
                    for role, container in self.containers.items()
                    if any(
                        mount["Name"] == selector.removeprefix("volume=")
                        for mount in container["Mounts"]
                    )
                ]
            return "\n".join(roles).encode()
        raise AssertionError(arguments)

    def test_supported_legacy_compose_is_eligible_for_preservation(self) -> None:
        self.assertEqual(self.namespace["preview_containers"](self.source), self.containers)

    def test_foreign_host_mappings_are_rejected_on_every_service(self) -> None:
        legacy = self.containers["web"]["HostConfig"]["ExtraHosts"]
        for role, container in self.containers.items():
            original = container["HostConfig"]["ExtraHosts"]
            mappings = [
                ["foreign.example.test:192.0.2.9"],
                [*legacy, "foreign.example.test:192.0.2.9"],
            ]
            if role != "web":
                mappings.append(legacy)
            for hosts in mappings:
                with self.subTest(role=role, hosts=hosts):
                    container["HostConfig"]["ExtraHosts"] = hosts
                    with self.assertRaises(RuntimeError):
                        self.namespace["preview_containers"](self.source)
            container["HostConfig"]["ExtraHosts"] = original


class SnapshotCaptureDeadlineTests(unittest.TestCase):
    def test_capture_deadline_terminates_quiet_child_and_retains_original(self) -> None:
        namespace = capture_namespace(PREVIEW_PRESERVATION_PROGRAM)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "copy"
            processes = []
            start = subprocess.Popen

            def observe(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                process = start(*args, **kwargs)
                processes.append(process)
                return process

            with patch.object(subprocess, "Popen", side_effect=observe):
                with self.assertRaises(RuntimeError):
                    namespace["capture_snapshot_file"](
                        [sys.executable, "-c", "import time;time.sleep(60)"],
                        path,
                        1024,
                        timeout_seconds=0,
                    )
            self.assertTrue(processes)
            self.assertTrue(all(process.poll() is not None for process in processes))


class _PreservationVerifierEngine:
    def __init__(self, restore_succeeds: bool) -> None:
        self.restore_succeeds = restore_succeeds
        self.final_server = False
        self.environment: dict[str, str] = {}
        self.network = "bridge"
        self.restored_bytes = b""
        self.writable_volumes: set[str] = set()
        self.created_volumes: set[str] = set()
        self.host_mounts: list[str] = []
        self.published_ports: list[str] = []
        self.privileged = False
        self.image = ""

    def can_contact_external_service(self) -> bool:
        return self.network != "none"

    def docker(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        result = self.command(["docker", *arguments], input=stdin)
        if result.returncode:
            raise RuntimeError("Preview preservation Docker operation failed")
        return result.stdout

    def command(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output = b""
        status = 0
        if arguments[1:3] == ["volume", "create"]:
            self.created_volumes.add(arguments[-1])
        elif arguments[1] == "run":
            parser = argparse.ArgumentParser(add_help=False)
            for option in (
                "--pull",
                "--name",
                "--memory",
                "--memory-swap",
                "--cpus",
                "--security-opt",
            ):
                parser.add_argument(option)
            parser.add_argument("--label", action="append")
            parser.add_argument("--network", default="bridge")
            parser.add_argument("--env", "-e", action="append", default=[])
            parser.add_argument("--mount", action="append", default=[])
            parser.add_argument("--volume", "-v", action="append", default=[])
            parser.add_argument("--publish", "-p", action="append", default=[])
            parser.add_argument("--privileged", action="store_true")
            parser.add_argument("-d", "--detach", action="store_true")
            parser.add_argument("image")
            run = parser.parse_args(arguments[2:])
            self.environment = dict(item.split("=", 1) for item in run.env)
            self.network, self.image = run.network, run.image
            self.published_ports, self.privileged = run.publish, run.privileged
            self.host_mounts = run.volume
            for mount in run.mount:
                fields = dict(item.split("=", 1) for item in mount.split(","))
                source: str = fields.get("source") or fields.get("src") or ""
                if fields.get("type") == "volume":
                    self.writable_volumes.add(source)
                else:
                    self.host_mounts.append(source)
            output = b"owned-verifier"
        elif arguments[1] == "exec":
            command = arguments[4:] if arguments[2] == "-i" else arguments[3:]
            if command[0] == "pg_isready":
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("-h", "--host", default="")
                parser.add_argument("-U", "--username")
                probe = parser.parse_args(command[1:])
                status = 0 if self.final_server or not probe.host else 1
                if status:
                    self.final_server = True
            elif not self.final_server:
                status = 1
            elif command[0] == "pg_restore":
                self.restored_bytes = kwargs["stdin"].read()
                status = 0 if self.restore_succeeds else 1
            elif command[0] == "sh":
                user = self.environment.get("POSTGRES_USER") or "root"
                status = 0 if user == "postgres" else 1
                output = b"3" if status == 0 else b""
        return subprocess.CompletedProcess(arguments, status, output, b"")


class PreviewPreservationVerifierCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace = capture_namespace(PREVIEW_PRESERVATION_PROGRAM)
        self.identifier = hashlib.sha256(b"fixture verifier").hexdigest()
        self.name = "fixture-verifier"
        self.image = "sha256:fixture-database"
        self.container: dict[str, Any] = {
            "Id": "fixture-container",
            "Config": {"Labels": {"io.launchplane.preview-preservation": self.identifier}},
            "Image": self.image,
            "HostConfig": {"NetworkMode": "none"},
            "Mounts": [
                {"Type": "volume", "Name": self.name, "Destination": "/var/lib/postgresql/data"}
            ],
        }
        self.volume = {
            "Labels": {"io.launchplane.preview-preservation": self.identifier},
            "Options": {},
            "Driver": "local",
        }
        self.consumers = [self.container["Id"]]
        self.calls: list[tuple[str, ...]] = []
        self.namespace.update(spec={"preservation_id": self.identifier}, docker=self.docker)

    def docker(self, *arguments: str) -> bytes:
        self.calls.append(arguments)
        if arguments[0] == "ps":
            return "\n".join(
                self.consumers if arguments[-1].startswith("volume=") else [self.container["Id"]]
            ).encode()
        if arguments[0] == "inspect":
            return json.dumps([self.container]).encode()
        if arguments[:2] == ("volume", "ls"):
            return self.name.encode()
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([self.volume]).encode()
        return b""

    def cleanup(self) -> None:
        self.namespace["verification_resources"](self.name, self.image)

    def test_foreign_verifier_or_volume_consumer_prevents_every_removal(self) -> None:
        for failure in ("container", "consumer", "volume"):
            with self.subTest(failure=failure):
                self.calls.clear()
                if failure == "container":
                    self.container["HostConfig"]["NetworkMode"] = "bridge"
                elif failure == "consumer":
                    self.consumers.append("foreign-reader")
                else:
                    self.volume["Options"] = {"device": "/foreign"}
                with self.assertRaises(RuntimeError):
                    self.cleanup()
                self.assertFalse(
                    any(call[0] == "rm" or call[:2] == ("volume", "rm") for call in self.calls)
                )
                self.container["HostConfig"]["NetworkMode"] = "none"
                self.consumers = [self.container["Id"]]
                self.volume["Options"] = {}

    def test_owned_isolated_verifier_is_removed_before_its_own_volume(self) -> None:
        self.cleanup()
        removals = [call for call in self.calls if call[0] == "rm" or call[:2] == ("volume", "rm")]
        self.assertEqual(
            removals, [("rm", "-f", self.container["Id"]), ("volume", "rm", self.name)]
        )

    def test_verifier_restores_with_final_server_identity_and_isolated_storage(self) -> None:
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds), TemporaryDirectory() as temporary:
                dump = Path(temporary) / "database.dump"
                dump.write_bytes(b"captured database")
                cleanup = Mock()
                engine = _PreservationVerifierEngine(succeeds)
                self.namespace.update(
                    docker=engine.docker,
                    verification_resources=cleanup,
                    deadline=time.monotonic() + 60,
                )
                with (
                    patch.object(subprocess, "run", side_effect=engine.command),
                    patch.object(time, "sleep"),
                ):
                    if succeeds:
                        self.assertGreater(
                            self.namespace["restore_verification"](dump, self.image), 0
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            self.namespace["restore_verification"](dump, self.image)
                self.assertEqual(engine.restored_bytes, dump.read_bytes())
                self.assertFalse(engine.can_contact_external_service())
                self.assertTrue(engine.created_volumes)
                self.assertEqual(engine.writable_volumes, engine.created_volumes)
                self.assertFalse(engine.host_mounts)
                self.assertFalse(engine.published_ports)
                self.assertFalse(engine.privileged)
                self.assertEqual(engine.image, self.image)
                self.assertEqual(cleanup.call_count, 2)

    def test_transient_exec_must_finish_and_persistent_exec_remains_held(self) -> None:
        active = {"Id": "fixture-database", "ExecIDs": ["active-exec"]}
        idle = {**active, "ExecIDs": None}
        with patch.object(time, "sleep"):
            self.namespace["one_container"] = Mock(return_value=idle)
            self.assertEqual(self.namespace["require_idle_container"](active), idle)
            self.namespace["one_container"] = Mock(return_value=active)
            with self.assertRaises(RuntimeError):
                self.namespace["require_idle_container"](active)


class PreviewPreservationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "state.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        original_request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        original_request.apply.dry_run_plan.clean_snapshot = CleanTestingSnapshot(
            product=self.profile.product,
            context=self.profile.preview.context,
            snapshot_id="a" * 64,
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            server_id="fixture-server",
            captured_at=datetime.now(timezone.utc).isoformat(),
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
            ),
        )
        original_plan = _issued_plan(original_request)
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=original_request,
            issued_plan=original_plan,
            reservation_scope="fixture-original-scope",
            idempotency_key=original_plan.plan_provenance.plan_id
            if original_plan.plan_provenance
            else "",
            request_fingerprint="fixture-original-request",
            trace_id="fixture-original",
        )
        reservation = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=operation.idempotency_key,
            request_fingerprint="fixture-original-request",
            lease_owner="fixture-original-executor",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=operation.adapter.original_intent(),
        ).record
        assert reservation is not None
        self.original = reservation
        destroy = original_request.model_copy(deep=True)
        destroy.apply.dry_run_plan.operation = "destroy"
        destroy.apply.dry_run_plan.compose_ref = "fixture-provider-target"
        destroy.apply.dry_run_plan.clean_snapshot = None
        self.issued = _issued_plan(destroy)
        self.destroy = OdooPreviewApplyEnvelope.model_validate(
            {
                **destroy.model_dump(mode="json"),
                "preserve_original_reservation_id": self.original.record_id,
            }
        )

    def authority(self) -> Any:
        return read_preview_preservation_authority(
            store=self.store,
            profile=self.profile,
            issued_plan=self.issued,
            original_reservation_id=self.original.record_id,
        )

    def admit(self) -> Any:
        return require_preview_preservation_admission(
            store=self.store,
            profile=self.profile,
            issued_plan=self.issued,
            original_reservation_id=self.original.record_id,
            operation_record_id="fixture-new-operation",
        )

    def complete_original(self) -> None:
        completion = complete_launchplane_mutation_reservation(
            self.original,
            response_status_code=409,
            response_trace_id="fixture-superseded",
            completed_at=self.original.updated_at,
            response_payload={"retained": True},
        )
        result = self.store.complete_mutation_reservation(completion=completion)
        self.assertEqual(result.status, "completed")

    def no_snapshot_original(self, binding: dict[str, object] | None) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(
            self.store.read_original_mutation_intent(self.original)["apply_request"]
        )
        request.apply.dry_run_plan.clean_snapshot = None
        self.complete_original()
        issued = _issued_plan(request)
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=request,
            issued_plan=issued,
            reservation_scope="no-snapshot",
            idempotency_key=issued.plan_provenance.plan_id if issued.plan_provenance else "",
            request_fingerprint="no-snapshot",
            trace_id="no-snapshot",
        )
        operation.adapter.schedule_binding = binding
        reserved = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=operation.route_path,
            idempotency_key=operation.idempotency_key,
            request_fingerprint=operation.request_fingerprint,
            lease_owner="no-snapshot",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=operation.adapter.original_intent(),
        ).record
        assert reserved is not None
        self.original = reserved
        self.destroy.preserve_original_reservation_id = reserved.record_id

    def server_binding(self, server: str = "fixture-server") -> dict[str, object]:
        from control_plane.dokploy.post_deploy import _build_dokploy_data_workflow_schedule_app_name

        return {
            "schedule_type": "server",
            "lookup_id": server,
            "app_name": _build_dokploy_data_workflow_schedule_app_name(
                context_name=self.profile.product, instance_name=self.issued.preview_slug
            ),
        }

    def test_no_snapshot_original_uses_its_retained_server_without_changing_intent(self) -> None:
        binding = self.server_binding()
        self.no_snapshot_original(binding)
        retained = self.store.read_original_mutation_intent(self.original)
        self.assertEqual(self.authority().server_id, binding["lookup_id"])
        self.assertEqual(self.store.read_original_mutation_intent(self.original), retained)

    def test_no_snapshot_original_uses_captured_then_persisted_preserving_binding(self) -> None:
        self.no_snapshot_original(None)
        self.complete_original()
        binding = self.server_binding()
        execute = Mock(return_value={"status": "fail", "provider_effect_attempted": True})
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="new-preservation",
            idempotency_key=self.issued.plan_provenance.plan_id
            if self.issued.plan_provenance
            else "",
            request_fingerprint="new-preservation",
            trace_id="new-preservation",
            execute_apply=execute,
        )
        with patch.object(
            operation.adapter,
            "capture_schedule_binding",
            side_effect=lambda: setattr(operation.adapter, "schedule_binding", binding),
        ):
            result = operation.run(self.store)
        self.assertEqual(result.status, "reconcile_required")
        execute.assert_called_once()
        assert result.record is not None
        authority = read_preview_preservation_authority(
            store=self.store,
            profile=self.profile,
            issued_plan=self.issued,
            original_reservation_id=self.original.record_id,
            operation_record_id=result.record.record_id,
        )
        self.assertEqual(authority.server_id, binding["lookup_id"])
        self.assertEqual(
            self.store.read_original_mutation_intent(result.record)["schedule_binding"], binding
        )

    def test_missing_server_proof_cannot_create_a_preserving_operation(self) -> None:
        self.no_snapshot_original(None)
        self.complete_original()
        for binding in (
            None,
            {**self.server_binding(), "schedule_type": "dokploy-server"},
            {**self.server_binding(), "app_name": "another-preview"},
        ):
            with self.subTest(binding=binding):
                execute = Mock()
                operation = prepare_odoo_preview_apply_operation(
                    control_plane_root=self.root,
                    record_store=self.store,
                    profile=self.profile,
                    apply_request=self.destroy,
                    issued_plan=self.issued,
                    reservation_scope="missing-server",
                    idempotency_key="missing-server",
                    request_fingerprint="missing-server",
                    trace_id="missing-server",
                    execute_apply=execute,
                )
                with (
                    patch.object(
                        operation.adapter,
                        "capture_schedule_binding",
                        side_effect=lambda: setattr(operation.adapter, "schedule_binding", binding),
                    ),
                    patch("control_plane.preview_preservation.run_preview_preservation") as copy,
                    self.assertRaises(ValueError),
                ):
                    operation.run(self.store)
                execute.assert_not_called()
                copy.assert_not_called()
                self.assertIsNone(
                    self.store.read_idempotency_record_by_id(
                        operation.adapter._deployment_record_id
                    )
                )

    def test_conflicting_server_proof_refuses_preservation(self) -> None:
        self.no_snapshot_original(self.server_binding())
        execute = Mock()
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="conflicting-server",
            idempotency_key="conflicting-server",
            request_fingerprint="conflicting-server",
            trace_id="conflicting-server",
            execute_apply=execute,
        )
        with (
            patch.object(
                operation.adapter,
                "capture_schedule_binding",
                side_effect=lambda: setattr(
                    operation.adapter, "schedule_binding", self.server_binding("another-server")
                ),
            ),
            patch("control_plane.preview_preservation.run_preview_preservation") as copy,
            self.assertRaises(ValueError),
        ):
            operation.run(self.store)
        execute.assert_not_called()
        copy.assert_not_called()
        self.assertIsNone(
            self.store.read_idempotency_record_by_id(operation.adapter._deployment_record_id)
        )

    def test_uncertain_compose_destroy_adopts_only_same_key_verified_absence_and_archive(
        self,
    ) -> None:
        self.complete_original()

        def interrupted(**kwargs: Any) -> dict[str, object]:
            kwargs["provider_effect_checkpoint"]("compose_destroy")
            return {"status": "fail", "provider_effect_attempted": True}

        execute = Mock(side_effect=interrupted)
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="uncertain-delete",
            idempotency_key=self.issued.plan_provenance.plan_id
            if self.issued.plan_provenance
            else "",
            request_fingerprint="uncertain-delete",
            trace_id="uncertain-delete",
            execute_apply=execute,
        )
        with (
            patch.object(operation.adapter, "capture_schedule_binding"),
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime._compose_exists_by_id",
                return_value=True,
            ) as exists,
            patch(
                "control_plane.workflows.odoo_preview_runtime._destroyed_preview_absence_verified",
                return_value=False,
            ) as absent,
            patch("control_plane.preview_preservation.run_preview_preservation") as archive,
        ):
            first = operation.run(self.store)
            self.assertEqual(first.status, "reconcile_required")
            assert first.record is not None
            unknown = operation.run(self.store)
            self.assertEqual(unknown.status, "reconcile_required")
            self.assertEqual(unknown.record, first.record)
            archive.assert_not_called()
            execute.assert_called_once()
            exists.return_value = False
            still_unknown = operation.run(self.store)
            self.assertEqual(still_unknown.status, "reconcile_required")
            archive.assert_not_called()
            absent.return_value = True
            archive.side_effect = ValueError("archive changed")
            with self.assertRaises(ValueError) as raised:
                operation.run(self.store)
            self.assertIs(raised.exception, archive.side_effect)
            held = self.store.read_idempotency_record_by_id(first.record.record_id)
            assert held is not None
            self.assertEqual(
                held.state,
                "reconcile_required",
            )
            archive.side_effect = None
            recovered = operation.run(self.store)
            self.assertEqual(recovered.status, "adopted")
            self.assertEqual(archive.call_args.kwargs["mode"], "verify")
            assert recovered.record is not None
            self.assertEqual(recovered.record.record_id, first.record.record_id)
            execute.assert_called_once()

    def test_same_slug_target_disagreement_refuses_before_reservation_or_copy(self) -> None:
        self.complete_original()
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.issued.context,
                instance=self.issued.preview_slug,
                target_id="different-compose",
                updated_at=self.original.updated_at,
            )
        )
        execute = Mock()
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="target-mismatch",
            idempotency_key="target-mismatch",
            request_fingerprint="target-mismatch",
            trace_id="target-mismatch",
            execute_apply=execute,
        )
        with (
            patch("control_plane.preview_preservation.run_preview_preservation") as copy,
            self.assertRaises(ValueError),
        ):
            operation.run(self.store)
        execute.assert_not_called()
        copy.assert_not_called()
        self.assertIsNone(
            self.store.read_idempotency_record_by_id(operation.adapter._deployment_record_id)
        )

    def test_active_original_blocks_destruction_without_blocking_custody_reads(self) -> None:
        self.assertEqual(self.authority().original_reservation_id, self.original.record_id)
        with self.assertRaises(ValueError):
            self.admit()

    def test_custody_hash_survives_original_completion_and_mutable_capacity_policy(self) -> None:
        before = self.authority()
        self.complete_original()
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.admit(), before)
        self.profile.preview.override_env["FIXTURE_CAPACITY_NOTE"] = "changed"
        self.assertEqual(self.authority(), before)

    def test_changed_target_cannot_use_original_bytes(self) -> None:
        self.issued.dry_run_plan.compose_name += "-other"
        with self.assertRaises(ValueError):
            self.authority()

    def test_protected_custody_stays_held_even_with_completed_original(self) -> None:
        from tests.support.disposable_lanes import record_safety

        self.complete_original()
        for facts in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(facts=facts):
                record_safety(
                    self.store,
                    self.profile.product,
                    self.profile.preview.context,
                    self.issued.preview_slug,
                    **facts,
                )
                with self.assertRaises(ValueError):
                    self.admit()

    def adapter(self) -> Any:
        return prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="fixture-new-scope",
            idempotency_key="fixture-preserving-destroy",
            request_fingerprint="fixture-preserving-destroy",
            trace_id="fixture-preserving-destroy",
            observe_apply=Mock(return_value=("unknown", None, False)),
        ).adapter

    def test_retry_refusal_keeps_surviving_effects_and_releases_completed_rollback(self) -> None:
        self.complete_original()
        for phase, preserving in (
            ("domain_delete", True),
            ("domain_delete", False),
            ("compose_update", False),
            ("rollback_complete", False),
        ):
            with self.subTest(phase=phase, preserving=preserving):
                request = self.destroy.model_copy(deep=True)
                if not preserving:
                    request.preserve_original_reservation_id = ""
                if phase != "domain_delete":
                    request.apply.dry_run_plan.operation = "refresh"
                key = f"retry-{phase}-{preserving}"
                request.apply.dry_run_plan.compose_name += "-" + key.lower()
                guard = Mock()

                def interrupted(**kwargs: Any) -> dict[str, object]:
                    kwargs["provider_effect_checkpoint"](phase)
                    return {"status": "fail", "provider_effect_attempted": True}

                operation = prepare_odoo_preview_apply_operation(
                    control_plane_root=self.root,
                    record_store=self.store,
                    profile=self.profile,
                    apply_request=request,
                    issued_plan=_issued_plan(request),
                    reservation_scope=key,
                    idempotency_key=key,
                    request_fingerprint=key,
                    trace_id=key,
                    execute_apply=interrupted,
                    observe_apply=Mock(return_value=("absent", None, True)),
                    pre_mutation_guard=guard,
                )
                with patch(
                    "control_plane.preview_preservation.require_preview_preservation_admission"
                ):
                    first = operation.run(self.store)
                    self.assertEqual(first.status, "reconcile_required")
                    assert first.record is not None
                    guard.side_effect = ValueError("current admission refused")
                    if phase == "rollback_complete":
                        with self.assertRaises(ValueError) as raised:
                            operation.run(self.store)
                        self.assertIs(raised.exception, guard.side_effect)
                        self.assertIsNone(
                            self.store.read_idempotency_record_by_id(first.record.record_id)
                        )
                    else:
                        retried = operation.run(self.store)
                        self.assertEqual(retried.status, "reconcile_required")
                        assert retried.record is not None
                        self.assertEqual(
                            retried.record.provider_effect_started_at,
                            first.record.provider_effect_started_at,
                        )
                        self.assertEqual(retried.record.provider_effect_phase, phase)

    def test_snapshot_supersession_requires_positive_custody(self) -> None:
        original = self.store.read_original_mutation_intent(self.original)
        request = OdooPreviewApplyEnvelope.model_validate(original["apply_request"])
        self.profile.preview.clean_testing_snapshot = True
        future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        for owned in (False, True):
            with self.subTest(owned=owned):
                execute = Mock(
                    return_value={
                        "status": "pass",
                        "operation": "refresh",
                        "product": self.profile.product,
                        "repository": self.profile.repository,
                        "preview_slug": self.issued.preview_slug,
                        "preview_url": self.issued.preview_url,
                        "compose_id": "fixture-provider-target",
                        "domain_host": self.issued.preview_url.removeprefix("https://"),
                        "compose_name": self.issued.dry_run_plan.compose_name,
                    }
                )
                quiescence = Mock(return_value=owned)
                operation = prepare_odoo_preview_apply_operation(
                    control_plane_root=self.root,
                    record_store=self.store,
                    profile=self.profile,
                    apply_request=request,
                    issued_plan=_issued_plan(request),
                    reservation_scope="new-refresh",
                    idempotency_key="new-refresh",
                    request_fingerprint="new-refresh",
                    trace_id="new-refresh",
                    execute_apply=execute,
                    supersession_is_quiescent=quiescence,
                )
                with patch.object(self.store, "_database_mutation_timestamp", return_value=future):
                    result = operation.run(self.store)
                self.assertTrue(quiescence.call_args.kwargs["require_snapshot_custody"])
                current = self.store.read_idempotency_record_by_id(self.original.record_id)
                assert current is not None
                if owned:
                    self.assertEqual(result.status, "completed")
                    self.assertEqual(current.state, "completed")
                    execute.assert_called_once()
                else:
                    self.assertEqual(result.status, "target_busy")
                    self.assertNotEqual(current.state, "completed")
                    self.assertIsNone(
                        self.store.read_idempotency_record(
                            scope="new-refresh",
                            route_path=ODOO_PREVIEW_APPLY_ROUTE,
                            idempotency_key="new-refresh",
                        )
                    )
                    execute.assert_not_called()

    def test_preserving_destroy_can_supersede_an_unowned_snapshot_original(self) -> None:
        self.profile.preview.clean_testing_snapshot = True
        future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        execute = Mock(
            return_value={
                "status": "pass",
                "operation": "destroy",
                "product": self.profile.product,
                "repository": self.profile.repository,
                "preview_slug": self.issued.preview_slug,
                "preview_url": self.issued.preview_url,
                "domain_host": self.issued.preview_url.removeprefix("https://"),
                "compose_id": "fixture-provider-target",
                "compose_name": self.issued.dry_run_plan.compose_name,
            }
        )
        quiescence = Mock(return_value=True)
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.destroy,
            issued_plan=self.issued,
            reservation_scope="preserving-destroy",
            idempotency_key="preserving-destroy",
            request_fingerprint="preserving-destroy",
            trace_id="preserving-destroy",
            execute_apply=execute,
            supersession_is_quiescent=quiescence,
        )
        with (
            patch.object(self.store, "_database_mutation_timestamp", return_value=future),
            patch("control_plane.preview_preservation.run_preview_preservation") as preservation,
            patch(
                "control_plane.workflows.odoo_preview_runtime.resolve_original_preview_compose_id",
                return_value="fixture-provider-target",
            ),
        ):
            result = operation.run(self.store)
        self.assertEqual(result.status, "completed")
        self.assertFalse(quiescence.call_args.kwargs.get("require_snapshot_custody", False))
        self.assertEqual(preservation.call_args.kwargs["mode"], "verify")
        execute.assert_called_once()
        current = self.store.read_idempotency_record_by_id(self.original.record_id)
        assert current is not None
        self.assertEqual(current.state, "completed")

    def test_only_qualified_preservation_phase_can_resume_original_execution(self) -> None:
        from control_plane.preview_preservation import PREVIEW_PRESERVATION_CAPTURE_PHASE

        adapter = self.adapter()
        with patch("control_plane.preview_preservation.run_preview_preservation") as inspect:
            held = adapter.observe("fixture-key", "compose_delete", "fixture-reconciliation")
            self.assertEqual(held.outcome, "unknown")
            self.assertFalse(held.retry_safe)
            inspect.assert_not_called()
            resumed = adapter.observe(
                "fixture-key", PREVIEW_PRESERVATION_CAPTURE_PHASE, "fixture-reconciliation"
            )
            self.assertEqual(resumed.outcome, "absent")
            self.assertTrue(resumed.retry_safe)
            self.assertTrue(resumed.preserve_effect_checkpoint)
            self.assertEqual(inspect.call_args.kwargs["mode"], "inspect")
            inspect.side_effect = ValueError("source execution unknown")
            with self.assertRaises(ValueError) as raised:
                adapter.observe(
                    "fixture-key", PREVIEW_PRESERVATION_CAPTURE_PHASE, "fixture-reconciliation"
                )
            self.assertIs(raised.exception, inspect.side_effect)

    def test_adoption_cannot_write_destroyed_lifecycle_without_retained_copy_verification(
        self,
    ) -> None:
        adapter = self.adapter()
        with (
            patch(
                "control_plane.preview_preservation.run_preview_preservation",
                side_effect=ValueError("copy changed"),
            ) as preservation,
            patch(
                "control_plane.odoo_preview_apply_execution.apply_odoo_preview_lifecycle_evidence"
            ) as lifecycle,
        ):
            with self.assertRaises(ValueError) as raised:
                adapter._finalize_successful_result({"status": "pass"})
            self.assertIs(raised.exception, preservation.side_effect)
            lifecycle.assert_not_called()

    def test_remote_inspection_waits_outside_shared_metadata_guard(self) -> None:
        from control_plane.preview_preservation import run_preview_preservation

        self.complete_original()
        source = {"provider_binding_sha256": "fixture-provider-binding"}
        guarded = False

        @contextmanager
        def guard() -> Iterator[None]:
            nonlocal guarded
            guarded = True
            try:
                yield
            finally:
                guarded = False

        def inspect(**kwargs: Any) -> str:
            del kwargs
            self.assertFalse(guarded)
            return source["provider_binding_sha256"]

        def remote(**kwargs: Any) -> None:
            self.assertFalse(guarded)
            kwargs["dispatch_guard"](lambda: self.assertTrue(guarded))
            self.assertFalse(guarded)
            kwargs["receive"](
                {
                    "type": "complete",
                    "preservation_id": self.authority().preservation_id,
                    "source": source,
                    "volume_binding": {"fixture": True},
                }
            )

        with (
            patch(
                "control_plane.preview_preservation.read_preview_preservation_source",
                return_value=source,
            ),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://fixture.invalid", "fixture"),
            ),
            patch(
                "control_plane.clean_testing_snapshot.snapshot_storage_budget",
                return_value=OdooTestingDrillStorageBudget(
                    database_bytes=1024,
                    filestore_bytes=1024,
                    staging_bytes=1024,
                    reserve_bytes=1024,
                ),
            ),
            patch(
                "control_plane.odoo_preview_resources.require_existing_preview_resources",
                side_effect=inspect,
            ),
            patch(
                "control_plane.dokploy.memory_capacity.preview_host_memory_floor", return_value=1024
            ),
            patch("control_plane.dokploy.native_copy_stream._read_remote", side_effect=remote),
        ):
            run_preview_preservation(
                store=self.store,
                profile=self.profile,
                issued_plan=self.issued,
                original_reservation_id=self.original.record_id,
                operation_record_id="fixture-new-operation",
                control_plane_root=self.root,
                mode="preserve",
                timeout_seconds=60,
                check=lambda: None,
                effect_checkpoint=Mock(),
                mutation_guard=guard,
            )
