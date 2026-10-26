import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from control_plane._preview_preservation_program import PREVIEW_PRESERVATION_PROGRAM
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.contracts.idempotency_record import complete_launchplane_mutation_reservation
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope, ODOO_PREVIEW_APPLY_ROUTE
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
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            self.preserve()
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
        with self.assertRaisesRegex(RuntimeError, "pending source changed"):
            self.preserve()
        self.assertEqual((temporary / "unknown").read_bytes(), b"only copy")
        self.assertTrue((self.filestore / "ab/attachment").exists())

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
        with self.assertRaisesRegex(RuntimeError, "memory headroom"):
            self.preserve()
        self.assertEqual(self.calls, [])

    def test_tampering_or_reopened_source_blocks_deletion_proof(self) -> None:
        receipt = self.preserve()
        self.containers["web"]["State"]["Running"] = True
        with self.assertRaisesRegex(RuntimeError, "reopened"):
            self.namespace["verify_preservation_source"](receipt, self.source)
        self.containers["web"]["State"]["Running"] = False
        (self.filestore / "ab/attachment").write_bytes(b"new original data")
        with self.assertRaisesRegex(RuntimeError, "original filestore changed"):
            self.namespace["verify_preservation_source"](receipt, self.source)
        (self.path / "database.dump").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "artifact changed"):
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
                with self.assertRaisesRegex(RuntimeError, "execution budget"):
                    namespace["capture_snapshot_file"](
                        [sys.executable, "-c", "import time;time.sleep(60)"],
                        path,
                        1024,
                        timeout_seconds=0,
                    )
            self.assertTrue(processes)
            self.assertTrue(all(process.poll() is not None for process in processes))


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
                with self.assertRaisesRegex(RuntimeError, "foreign custody"):
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

    def test_restore_stream_uses_only_isolated_owned_verifier_and_cleans_up_on_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            dump = Path(temporary) / "database.dump"
            dump.write_bytes(b"captured database")
            cleanup = Mock()
            calls: list[tuple[str, ...]] = []

            def docker(*arguments: str) -> bytes:
                calls.append(arguments)
                return b"owned-verifier" if arguments[0] == "run" else b""

            def restore(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(kwargs["stdin"].read(), dump.read_bytes())
                self.assertEqual(arguments[:4], ["docker", "exec", "-i", "owned-verifier"])
                return subprocess.CompletedProcess(arguments, 1, b"", b"invalid dump")

            self.namespace.update(
                docker=docker,
                verification_resources=cleanup,
                wait_database=Mock(),
                deadline=time.monotonic() + 60,
            )
            with patch.object(subprocess, "run", side_effect=restore):
                with self.assertRaisesRegex(RuntimeError, "restore verification failed"):
                    self.namespace["restore_verification"](dump, self.image)
            run = next(call for call in calls if call[0] == "run")
            self.assertEqual(run[run.index("--network") + 1], "none")
            mount = run[run.index("--mount") + 1]
            self.assertIn("src=lp-preview-preserve-" + self.identifier[:32], mount)
            self.assertNotIn("--privileged", run)
            self.assertNotIn("-p", run)
            self.assertNotIn("--volume", run)
            self.assertEqual(run[-1], self.image)
            self.assertEqual(cleanup.call_count, 2)


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

    def test_active_original_blocks_destruction_without_blocking_custody_reads(self) -> None:
        self.assertEqual(self.authority().original_reservation_id, self.original.record_id)
        with self.assertRaisesRegex(ValueError, "recovery grace"):
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
        with self.assertRaisesRegex(ValueError, "does not match"):
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
                with self.assertRaisesRegex(ValueError, "protected use or resource"):
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
            self.assertEqual(inspect.call_args.kwargs["mode"], "inspect")
            inspect.side_effect = ValueError("source execution unknown")
            with self.assertRaisesRegex(ValueError, "source execution unknown"):
                adapter.observe(
                    "fixture-key", PREVIEW_PRESERVATION_CAPTURE_PHASE, "fixture-reconciliation"
                )

    def test_adoption_cannot_write_destroyed_lifecycle_without_retained_copy_verification(
        self,
    ) -> None:
        adapter = self.adapter()
        with (
            patch(
                "control_plane.preview_preservation.run_preview_preservation",
                side_effect=ValueError("copy changed"),
            ),
            patch(
                "control_plane.odoo_preview_apply_execution.apply_odoo_preview_lifecycle_evidence"
            ) as lifecycle,
        ):
            with self.assertRaisesRegex(ValueError, "copy changed"):
                adapter._finalize_successful_result({"status": "pass"})
            lifecycle.assert_not_called()
