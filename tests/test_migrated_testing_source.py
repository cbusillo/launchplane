import hashlib
import unittest
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from control_plane.contracts.testing_candidate import TestingCandidateAllocation

from pydantic import ValidationError

from control_plane.copy_readback_diagnostics import (
    COPY_FAILURE_KIND_MARKER,
    COPY_FAILURE_EXIT_MARKER,
    COPY_COMMAND_FAILURE_KIND,
)

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)


class MigratedTestingRequestTests(unittest.TestCase):
    def request(self, **changes: object) -> OdooStableTargetReplacementApplyRequest:
        data = {
            "product": "fixture",
            "instance": "testing",
            "strategy": "replace-and-cutover",
            "data_source_mode": "migrated_restore",
            "artifact_id": "artifact-fixture",
            "source_git_ref": "a" * 40,
            "migrated_source": {
                "product": "fixture",
                "context": "fixture",
                "source_backup": "fixture-snapshot",
                "producer_run": "fixture-run",
                "target_image": "sha256:" + "b" * 64,
                "database": {
                    "sha256": hashlib.sha256(b"database").hexdigest(),
                    "size": len(b"database"),
                },
                "filestore": {
                    "sha256": hashlib.sha256(b"store").hexdigest(),
                    "size": len(b"store"),
                },
                "filestore_expanded_bytes": 100,
                "filestore_files": 1,
            },
            **changes,
        }
        return OdooStableTargetReplacementApplyRequest.model_validate(data)

    def test_completed_migrated_pair_is_a_testing_restore_input(self) -> None:
        request = self.request()
        assert request.migrated_source is not None
        self.assertEqual(request.migrated_source.product, request.product)

    def test_migrated_input_never_selects_production_or_an_in_place_restore(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"strategy": "recreate-in-place"},
            {"migrated_source": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.request(**changes)

    def test_migrated_input_cannot_be_relabelled_as_an_ordinary_production_copy(self) -> None:
        with self.assertRaises(ValidationError):
            self.request(data_source_mode="production_restore")


class MigratedUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from control_plane.storage.postgres import PostgresRecordStore
        from tests.test_odoo_testing_restore import generation
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
        from control_plane.migrated_testing_source import upload_directory

        self.original_upload_directory = upload_directory
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        source = MigratedTestingRequestTests().request().migrated_source
        assert source is not None
        self.source = source
        self.generation = generation().model_copy(
            update={
                "product": source.product,
                "context": source.context,
                "source_mode": "container-export",
                "migrated_source": source,
                "drill_storage_budget": OdooTestingDrillStorageBudget(
                    database_bytes=1024,
                    filestore_bytes=1024,
                    staging_bytes=1024,
                    reserve_bytes=1024,
                ),
                "source_binding_sha256": source.binding_sha256,
            }
        )
        self.allocation = cast(
            "TestingCandidateAllocation",
            SimpleNamespace(
                namespace=self.generation.namespace,
                product=source.product,
                operation_id=self.generation.operation_id,
                application=SimpleNamespace(
                    plan=SimpleNamespace(generation=self.generation, native_source=source)
                ),
            ),
        )
        self.store = Mock(spec=PostgresRecordStore)
        self.store.read_native_testing_copy_capture.return_value = None
        self.enterContext(
            patch(
                "control_plane.migrated_testing_source.upload_directory",
                return_value=self.directory,
            )
        )

    def test_storage_preserves_existing_reserve_for_the_entire_remaining_pair(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from control_plane.migrated_testing_source import receive_migrated_part

        budget = self.generation.drill_storage_budget
        assert budget is not None
        free = self.source.database.size + self.source.filestore.size + budget.reserve_bytes - 1
        with (
            patch(
                "control_plane.migrated_testing_source.shutil.disk_usage",
                return_value=SimpleNamespace(free=free),
            ),
            self.assertRaisesRegex(ValueError, "reserve"),
        ):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ):
                self.fail("The upload consumed reserved shared storage")
        self.assertFalse((self.directory / "database").exists())

    def test_other_writers_cannot_consume_the_reserve_during_an_upload(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from control_plane.migrated_testing_source import receive_migrated_part

        with self.assertRaisesRegex(ValueError, "reserve"):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ) as upload:
                with patch(
                    "control_plane.migrated_testing_source.os.fstatvfs",
                    return_value=SimpleNamespace(f_bavail=1, f_frsize=1),
                ):
                    upload.write(b"database")
        self.assertEqual((self.directory / "database.partial").stat().st_size, 0)

    def test_same_pair_reuses_content_across_candidates_and_get_creates_no_directory(self) -> None:
        from unittest.mock import patch
        from control_plane.migrated_testing_source import upload_state

        root = self.directory / "not-created"
        with patch("control_plane.migrated_testing_source._cache_directory", return_value=root):
            first = self.original_upload_directory(self.allocation, create=False)
            assert self.allocation.application is not None
            self.allocation.application.plan.generation = self.generation.model_copy(
                update={"operation_id": "another-operation", "prepared_at": "another-time"}
            )
            second = self.original_upload_directory(self.allocation, create=False)
        self.assertEqual(first, second)
        self.assertFalse(root.exists())
        with (
            patch(
                "control_plane.migrated_testing_source.upload_directory",
                side_effect=lambda allocation, *, create: self.original_upload_directory(
                    allocation, create=create
                ),
            ),
            patch("control_plane.migrated_testing_source._cache_directory", return_value=root),
        ):
            self.assertEqual(
                upload_state(self.allocation),
                {part: {"complete": False, "size": 0} for part in ("database", "filestore")},
            )
        self.assertFalse(root.exists())

    def test_explicit_restart_replaces_wrong_partial_without_resetting_completed_original(
        self,
    ) -> None:
        from control_plane.migrated_testing_source import receive_migrated_part

        (self.directory / "database.partial").write_bytes(b"wrong bytes")
        with receive_migrated_part(
            self.allocation, "database", 0, hashlib.sha256().hexdigest()
        ) as upload:
            upload.write(b"database")
        with self.assertRaises(ValueError):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ):
                self.fail("A completed original was reset")
        self.assertEqual((self.directory / "database").read_bytes(), b"database")

    def test_interrupted_upload_resumes_only_its_exact_prefix(self) -> None:
        from control_plane.migrated_testing_source import receive_migrated_part

        with self.assertRaisesRegex(RuntimeError, "disconnect"):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ) as upload:
                upload.write(b"data")
                raise RuntimeError("disconnect")
        self.assertFalse((self.directory / "database").exists())
        with self.assertRaises(ValueError):
            with receive_migrated_part(
                self.allocation, "database", 4, hashlib.sha256(b"else").hexdigest()
            ):
                self.fail("A different prefix was admitted")
        with receive_migrated_part(
            self.allocation, "database", 4, hashlib.sha256(b"data").hexdigest()
        ) as upload:
            upload.write(b"base")
        self.assertEqual((self.directory / "database").read_bytes(), b"database")

    def test_wrong_digest_and_special_paths_never_publish_a_complete_artifact(self) -> None:
        import os
        from control_plane.migrated_testing_source import receive_migrated_part

        with self.assertRaises(ValueError):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ) as upload:
                upload.write(b"badbytes")
        self.assertFalse((self.directory / "database").exists())
        (self.directory / "database.partial").unlink()
        os.mkfifo(self.directory / "database.partial")
        with self.assertRaises(ValueError):
            with receive_migrated_part(
                self.allocation, "database", 0, hashlib.sha256().hexdigest()
            ):
                self.fail("FIFO admitted")

    def test_cleanup_keeps_migrated_pair_for_another_generations_pending_candidate(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from control_plane import odoo_testing_native_source as native
        from control_plane.migrated_testing_source import finish_migrated_upload

        (self.directory / "database").write_bytes(b"database")
        (self.directory / "filestore").write_bytes(b"store")
        capture = finish_migrated_upload(self.store, self.allocation)
        assert capture is not None
        other_generation = self.generation.model_copy(update={"operation_id": "another-original"})
        other = SimpleNamespace(
            product=self.source.product,
            context=self.source.context,
            operation_id=other_generation.operation_id,
            namespace="another-candidate",
            compose_id="another-target",
            application=SimpleNamespace(plan=SimpleNamespace(generation=other_generation)),
        )
        self.store.list_testing_candidates.return_value = [other]
        self.store.read_idempotency_record.return_value = None
        with patch.object(native, "_cache_directory", return_value=self.directory):
            native.remove_completed_native_capture_cache(capture, store=self.store)
            self.assertEqual((self.directory / "database").read_bytes(), b"database")
            self.store.list_testing_candidates.return_value = []
            native.remove_completed_native_capture_cache(capture, store=self.store)
            self.assertFalse(self.directory.exists())

    def test_lost_receipt_commit_reply_reuses_the_original_pair_and_timestamp(self) -> None:
        from control_plane.migrated_testing_source import finish_migrated_upload

        (self.directory / "database").write_bytes(b"database")
        self.assertIsNone(finish_migrated_upload(self.store, self.allocation))
        self.store.write_native_testing_copy_capture.assert_not_called()
        (self.directory / "filestore").write_bytes(b"store")
        self.store.write_native_testing_copy_capture.side_effect = RuntimeError("reply lost")
        with self.assertRaises(RuntimeError):
            finish_migrated_upload(self.store, self.allocation)
        original = self.store.write_native_testing_copy_capture.call_args.args[0]
        self.store.write_native_testing_copy_capture.side_effect = None
        retried = finish_migrated_upload(self.store, self.allocation)
        self.assertEqual(retried, original)
        self.assertEqual((self.directory / "filestore").read_bytes(), b"store")


class MigratedArtifactConsumptionTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        self.fixture = NativeArtifactConsumptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.staging = self.fixture.root / "volumes/data" / (".lp-native-" + self.fixture.identity)
        self.filestore = self.fixture.root / "volumes/data/filestore" / self.fixture.database

    def stage(
        self, files: list[tuple[str, bytes]] | None = None, member_type: bytes | None = None
    ) -> dict[str, Any]:
        import io
        import tarfile

        fixture = self.fixture
        files = files if files is not None else [(fixture.name, fixture.attachment)]
        capture = fixture.stage()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as handle:
            for name, data in files:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                if member_type is not None:
                    member.type = member_type
                    member.linkname = fixture.name
                handle.addfile(member, io.BytesIO(data))
        content = archive.getvalue()
        (fixture.root / "tmp" / f"lp-native-{fixture.identity}-filestore-0").write_bytes(content)
        capture["filestore"] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": sum(len(data) for _, data in files),
            "filestore_files": len(files),
        }
        self.staging.mkdir(parents=True, exist_ok=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                self.staging / (part + ".artifact")
            )
        return capture

    def test_fresh_receiver_inspects_no_artifacts_before_the_first_stream(self) -> None:
        import io
        import json
        from contextlib import redirect_stdout

        fixture = self.fixture
        capture = fixture.stage()
        capture["source_observation"] = {"kind": "migrated"}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            fixture.consume(capture, mode="inspect")
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue())["artifacts"], {})

    def test_extraction_retry_removes_only_its_owned_interrupted_temporary(self) -> None:
        fixture = self.fixture
        capture = self.stage()
        owned = self.staging / (".lp-native-" + fixture.identity + "-interrupted")
        owned.write_bytes(b"partial")
        with self.assertRaises(SystemExit):
            fixture.consume(capture)
        self.assertFalse(owned.exists())
        self.assertEqual(
            fixture.mapped_path(fixture.dump_path).read_bytes(), b"custom database fixture"
        )

    def test_all_metadata_and_attachment_bytes_survive_extraction_and_retry(self) -> None:
        from contextlib import nullcontext
        from unittest.mock import patch

        fixture = self.fixture
        files = [
            (".DS_Store", b"synthetic metadata"),
            ("nested/notes with spaces.txt", b"nested metadata"),
            (fixture.name, fixture.attachment),
            ("checklist/" + fixture.name, b"checklist attachment"),
            (".lp-native-" + fixture.identity + "-member", b"legitimate former scratch name"),
        ]
        capture = self.stage(files)
        for retry in (False, True):
            with self.subTest(retry=retry):
                if retry:
                    fixture.mapped_path(fixture.dump_path).unlink()
                scratch = (
                    patch(
                        "tests.test_odoo_testing_native_source.NamedTemporaryFile",
                        side_effect=OSError("No room for duplicate files"),
                    )
                    if retry
                    else nullcontext()
                )
                with (
                    scratch,
                    self.assertRaises(SystemExit) as exited,
                ):
                    fixture.consume(capture)
                self.assertEqual(exited.exception.code, 0)
                self.assertEqual(
                    {
                        str(path.relative_to(self.filestore)): path.read_bytes()
                        for path in self.filestore.rglob("*")
                        if path.is_file()
                    },
                    dict(files),
                )
                self.assertEqual(
                    fixture.mapped_path(fixture.dump_path).read_bytes(), b"custom database fixture"
                )

    def test_unsafe_and_aliased_member_names_never_publish_the_database(self) -> None:
        for name in (
            "/outside",
            "../outside",
            "nested/../outside",
            ".",
            "./metadata",
            "nested//metadata",
        ):
            with self.subTest(name=name):
                capture = self.stage([(name, b"metadata")])
                with self.assertRaises(RuntimeError):
                    self.fixture.consume(capture)
                self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_links_and_special_members_never_publish_the_database(self) -> None:
        import tarfile

        for member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(member_type=member_type):
                capture = self.stage([("metadata", b"")], member_type)
                with self.assertRaises(RuntimeError):
                    self.fixture.consume(capture)
                self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_duplicate_metadata_is_refused_before_database_publication(self) -> None:
        capture = self.stage([(".DS_Store", b"first"), (".DS_Store", b"other")])
        with self.assertRaises(RuntimeError):
            self.fixture.consume(capture)
        self.assertEqual((self.filestore / ".DS_Store").read_bytes(), b"first")
        self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_metadata_inventory_limits_remain_exact(self) -> None:
        for field in ("filestore_files", "filestore_expanded_bytes"):
            for adjustment in (-1, 1):
                with self.subTest(field=field, adjustment=adjustment):
                    capture = self.stage([("metadata", b"metadata")])
                    capture["source_observation"][field] += adjustment
                    with self.assertRaises(RuntimeError):
                        self.fixture.consume(capture)
                    self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_metadata_never_overwrites_different_existing_bytes(self) -> None:
        capture = self.stage([("metadata", b"new bytes")])
        self.filestore.mkdir(parents=True)
        destination = self.filestore / "metadata"
        destination.write_bytes(b"old bytes")
        with self.assertRaises(RuntimeError):
            self.fixture.consume(capture)
        self.assertEqual(destination.read_bytes(), b"old bytes")
        self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_metadata_never_follows_a_destination_alias(self) -> None:
        capture = self.stage([("nested/metadata", b"new bytes")])
        self.filestore.mkdir(parents=True)
        outside = self.fixture.root / "outside"
        outside.mkdir()
        (self.filestore / "nested").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.fixture.consume(capture)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_tampered_complete_archive_is_refused_by_normal_inspection(self) -> None:
        capture = self.stage([("metadata", b"original")])
        artifact = self.staging / "filestore.artifact"
        original = artifact.read_bytes()
        artifact.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaises(RuntimeError):
            self.fixture.consume(capture, mode="inspect")
        self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())

    def test_normal_cleanup_removes_interrupted_staging_scratch(self) -> None:
        capture = self.stage([("metadata", b"metadata")])
        scratch = self.staging / (".lp-native-" + self.fixture.identity + "-interrupted")
        scratch.write_bytes(b"partial metadata")
        with self.assertRaises(SystemExit):
            self.fixture.consume(capture, mode="cleanup")
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertFalse(self.filestore.exists())

    def test_metadata_extraction_preserves_space_before_and_during_writes(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        for probe, space in (
            ("shutil.disk_usage", SimpleNamespace(free=0)),
            ("os.fstatvfs", SimpleNamespace(f_bavail=0, f_frsize=1)),
        ):
            with self.subTest(probe=probe):
                capture = self.stage([("metadata", b"metadata")])
                with (
                    patch(probe, return_value=space),
                    self.assertRaises(RuntimeError),
                ):
                    self.fixture.consume(capture)
                self.assertFalse((self.filestore / "metadata").exists())
                self.assertFalse(list(self.staging.glob(".lp-native-*")))
                self.assertFalse(self.fixture.mapped_path(self.fixture.dump_path).exists())


class MigratedStreamTests(unittest.TestCase):
    def test_one_stream_sends_windows_and_resumes_only_a_matching_prefix(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from control_plane.dokploy.native_testing_stream import write_native_testing_stream
        from control_plane.contracts.odoo_testing_source import (
            NativeTestingCopyArtifact,
            NATIVE_TESTING_COPY_CHUNK_BYTES,
        )
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        data = b"a" * (NATIVE_TESTING_COPY_CHUNK_BYTES * 3 + 31)
        artifact = NativeTestingCopyArtifact(
            sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "original"
            path.write_bytes(data)

            def receive(**kwargs: Any) -> None:
                offset = NATIVE_TESTING_COPY_CHUNK_BYTES
                prefix = hashlib.sha256(data[:offset]).hexdigest()
                blocks = kwargs["input_frames"](
                    {"offset": offset, "chunks": 2, "prefix_sha256": prefix}
                )
                self.assertEqual(
                    b"".join(blocks), data[offset : offset + 2 * NATIVE_TESTING_COPY_CHUNK_BYTES]
                )
                blocks = kwargs["input_frames"](
                    {"offset": offset + 2 * NATIVE_TESTING_COPY_CHUNK_BYTES, "chunks": 1}
                )
                self.assertEqual(b"".join(blocks), data[-31:])
                kwargs["receive"]({"type": "complete", **artifact.model_dump()})

            with patch(
                "control_plane.dokploy.native_copy_stream.run_managed_server_program",
                side_effect=receive,
            ):
                write_native_testing_stream(
                    host="fixture",
                    token="private",
                    server_id="fixture",
                    container_id="a" * 64,
                    capture_id="b" * 32,
                    part="filestore",
                    path=path,
                    artifact=artifact,
                    reserve_bytes=1024,
                )

            def wrong_prefix(**kwargs: Any) -> None:
                kwargs["input_frames"](
                    {"offset": 1, "chunks": 1, "prefix_sha256": hashlib.sha256(b"z").hexdigest()}
                )

            with (
                patch(
                    "control_plane.dokploy.native_copy_stream.run_managed_server_program",
                    side_effect=wrong_prefix,
                ),
                self.assertRaises(NativeSourceUnavailable),
            ):
                write_native_testing_stream(
                    host="fixture",
                    token="private",
                    server_id="fixture",
                    container_id="a" * 64,
                    capture_id="b" * 32,
                    part="filestore",
                    path=path,
                    artifact=artifact,
                    reserve_bytes=1024,
                )

    def test_receiver_process_keeps_partial_bytes_and_resumes_the_original_artifact(self) -> None:
        import base64
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        from tempfile import TemporaryDirectory
        from control_plane.dokploy.native_testing_stream import NATIVE_TESTING_STREAM_WRITE_PROGRAM

        data = b"original bytes" * 20000
        artifact = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            volume = root / "data"
            volume.mkdir()
            docker = root / "docker"
            docker.write_text(
                "#!"
                + sys.executable
                + "\nimport os,sys\nprogram=sys.argv[-2].replace(\"Path('/volumes/data')\", \"Path(\"+repr(os.environ['FIXTURE_VOLUME'])+\")\")\nos.execv(sys.executable,[sys.executable,'-u','-c',program,sys.argv[-1]])\n"
            )
            docker.chmod(0o700)
            spec = {
                "container": "a" * 64,
                "capture_id": "b" * 32,
                "part": "filestore",
                "artifact": artifact,
                "reserve_bytes": 1024,
            }
            command = [
                sys.executable,
                "-u",
                "-c",
                NATIVE_TESTING_STREAM_WRITE_PROGRAM,
                base64.b64encode(json.dumps(spec).encode()).decode(),
                "FRAME:",
            ]
            env = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "FIXTURE_VOLUME": str(volume),
            }
            first = base64.b64encode(data[:65536]) + b"\n"
            interrupted = subprocess.run(
                command, input=first, capture_output=True, env=env, timeout=15
            )
            partial = volume / (".lp-native-" + "b" * 32) / "filestore.artifact.partial"
            self.assertTrue(partial.exists(), (interrupted.stdout, interrupted.stderr))
            self.assertEqual(partial.read_bytes(), data[:65536])
            self.assertIn(b'"type": "failed"', interrupted.stdout)
            failed = json.loads(interrupted.stdout.splitlines()[-1].removeprefix(b"FRAME:"))
            self.assertNotIn(COPY_FAILURE_KIND_MARKER, failed)
            from control_plane.dokploy import native_copy_stream as transport
            from tests.test_odoo_testing_native_source import Connection

            with self.assertRaises(transport.NativeProgramFailed) as raised:
                transport._read_program(
                    connection=Connection(failed),
                    program=NATIVE_TESTING_STREAM_WRITE_PROGRAM,
                    payload=spec,
                    timeout_seconds=5,
                    receive=lambda _frame: None,
                )
            self.assertFalse(raised.exception.has_diagnostic)
            rest = b"".join(
                base64.b64encode(data[index : index + 65536]) + b"\n"
                for index in range(65536, len(data), 65536)
            )
            resumed = subprocess.run(
                command, input=rest, capture_output=True, env=env, timeout=15, check=True
            )
            frames = [
                json.loads(line.removeprefix(b"FRAME:")) for line in resumed.stdout.splitlines()
            ]
            self.assertEqual(frames[0]["offset"], 65536)
            self.assertEqual(frames[-1], {"type": "complete", **artifact})
            self.assertEqual(partial.with_suffix("").read_bytes(), data)
            docker.write_text(
                "#!"
                + sys.executable
                + "\nimport hashlib,json,sys\nprint(json.dumps({'offset':0,'prefix_sha256':hashlib.sha256(b'').hexdigest()}),flush=True)\nsys.exit(7)\n"
            )
            content = b"".join(
                base64.b64encode(data[index : index + 65536]) + b"\n"
                for index in range(0, len(data), 65536)
            )
            dead_receiver = subprocess.run(
                command, input=content, capture_output=True, env=env, timeout=15
            )
            terminal = json.loads(dead_receiver.stdout.splitlines()[-1].removeprefix(b"FRAME:"))
            self.assertEqual(terminal[COPY_FAILURE_KIND_MARKER], COPY_COMMAND_FAILURE_KIND)
            self.assertEqual(terminal[COPY_FAILURE_EXIT_MARKER], "7")


class MigratedUploadRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        from control_plane.http_routes.support import ReadRouteDependencies
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.http_routes import migrated_testing_source as routes
        from control_plane.http_routes.migrated_testing_source import UploadRoutes
        from control_plane.service_auth import (
            BearerIdentityConfig,
            configured_local_operator_identity,
        )

        self.fixture = MigratedUploadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        identity = configured_local_operator_identity(
            BearerIdentityConfig(
                local_operator_token="fixture",
                local_operator_subject="director",
                local_operator_token_label="fixture",
            )
        )
        assert identity is not None
        from datetime import UTC, datetime
        from control_plane.contracts.artifact_identity import (
            ArtifactIdentityManifest,
            ArtifactImageReference,
        )
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.contracts.testing_candidate import (
            TestingCandidateAllocation,
            testing_candidate_namespace,
        )
        from control_plane.contracts.testing_candidate_application import (
            TestingCandidateApplication,
            TestingCandidateApplicationPlan,
        )
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            OdooStableTargetReplacementOperationRecord,
        )

        source = self.fixture.source
        operation_id = "original"
        namespace = testing_candidate_namespace(source.product, source.context, operation_id)
        generation = self.fixture.generation.model_copy(
            update={"operation_id": operation_id, "namespace": namespace}
        )
        artifact = ArtifactIdentityManifest(
            artifact_id="fixture-build",
            source_commit="a" * 40,
            enterprise_base_digest=source.target_image,
            image=ArtifactImageReference(
                repository="example.invalid/fixture", digest=source.target_image
            ),
        )
        plan = TestingCandidateApplicationPlan(
            product=source.product,
            context=source.context,
            operation_id=operation_id,
            namespace=namespace,
            target_id=generation.target_id,
            base_url="https://" + generation.domain_host,
            artifact=artifact,
            runtime_identity=RuntimeIdentity(
                product=source.product,
                context=source.context,
                instance="testing",
                deployment_record_id="fixture-deployment",
                artifact_id=artifact.artifact_id,
                source_git_ref=artifact.source_commit,
                image_digest=artifact.image.digest,
                image_reference=f"{artifact.image.repository}@{artifact.image.digest}",
            ),
            source="migrated",
            generation=generation,
            native_source=source,
            authority_sha256="a" * 64,
            credential_bindings_sha256="b" * 64,
        )
        self.allocation = TestingCandidateAllocation(
            namespace=namespace,
            product=source.product,
            context=source.context,
            operation_id=operation_id,
            intent_sha256="c" * 64,
            serving_target_id="serving",
            server_id="server",
            project_id="project",
            environment_id="environment",
            compose_id=generation.target_id,
            resources_binding_sha256=generation.resources_binding_sha256,
            created_at=datetime.now(UTC),
            application=TestingCandidateApplication(plan=plan),
        )
        self.fixture.allocation = self.allocation
        self.operation = OdooStableTargetReplacementOperationRecord(
            operation_id=operation_id,
            product=source.product,
            context=source.context,
            instance="testing",
            request=MigratedTestingRequestTests().request(),
            idempotency_scope=idempotency_scope(identity),
            idempotency_key="fixture-original",
            request_fingerprint="fixture-fingerprint",
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.store.read_testing_candidate.side_effect = lambda key: (
            self.allocation if key == self.allocation.namespace else None
        )
        self.store.read_odoo_stable_target_replacement_operation_record.return_value = (
            self.operation
        )
        self.policy = self.enterContext(
            patch.object(routes, "read_active_authz_policy_record", return_value=SimpleNamespace())
        )
        self.allowed = self.enterContext(
            patch.object(routes, "director_testing_rebuild_allowed", return_value=True)
        )
        self.app = FastAPI()

        def error(**kwargs: Any) -> HTTPException:
            return HTTPException(status_code=kwargs["status_code"], detail=kwargs["code"])

        dependencies = ReadRouteDependencies(
            read_identity=lambda: identity,
            get_record_store=lambda: self.store,
            next_trace_id=lambda: "fixture",
            authorization_allows=Mock(),
            http_error=error,
            error_response_model=BaseModel,
        )
        routes.register_migrated_testing_source_routes(
            cast(UploadRoutes, self.app),
            dependencies=dependencies,
            bearer_identity=lambda header: identity if header == "Bearer fixture" else None,
        )
        self.url = "/v1/products/fixture/testing-rebuild/original/migrated-source"

    async def websocket(
        self, url: str, frames: list[dict[str, Any]], *, token: str = "fixture"
    ) -> list[dict[str, Any]]:
        import asyncio
        from starlette.types import Message

        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        for frame in frames:
            incoming.put_nowait(frame)
        incoming.put_nowait({"type": "websocket.disconnect", "code": 1006})
        sent = []

        async def send(message: Message) -> None:
            sent.append(dict(message))

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": "wss",
            "path": url,
            "raw_path": url.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", ("Bearer " + token).encode())],
            "client": ("127.0.0.1", 123),
            "server": ("fixture", 443),
            "subprotocols": [],
        }
        await self.app(scope, incoming.get, send)
        return sent

    def frames(self, data: bytes, *, offset: int = 0, finish: bool = True) -> list[dict[str, Any]]:
        import json

        frames: list[dict[str, Any]] = [
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "offset": offset,
                        "prefix_sha256": hashlib.sha256(data[:offset]).hexdigest(),
                    }
                ),
            }
        ]
        if offset < len(data):
            frames.append({"type": "websocket.receive", "bytes": data[offset:]})
        if finish:
            frames.append({"type": "websocket.receive", "text": '{"complete": true}'})
        return frames

    async def test_original_caller_uploads_the_pair_and_get_never_commits_a_receipt(self) -> None:
        import json
        from tests.support.http import request

        read = await request(self.app, "GET", self.url)
        self.assertEqual(read.status_code, 200, read.text)
        self.store.write_native_testing_copy_capture.assert_not_called()
        for part, data in (("database", b"database"), ("filestore", b"store")):
            sent = await self.websocket(self.url + "/" + part, self.frames(data))
            self.assertEqual(sent[0]["type"], "websocket.accept", sent)
            self.assertEqual(json.loads(sent[1]["text"]), {"ready": True, "offset": 0})
            self.assertEqual(
                sent[-1], {"type": "websocket.close", "code": 1000, "reason": ""}, sent
            )
            result = json.loads(sent[-2]["text"])
            self.assertTrue(result["parts"][part]["complete"])
        self.store.write_native_testing_copy_capture.assert_called_once()

    async def test_foreign_product_caller_scope_and_authority_are_rejected_before_body_storage(
        self,
    ) -> None:
        for denied in ("product", "scope", "authority", "token", "policy"):
            self.policy.side_effect = LookupError("unavailable") if denied == "policy" else None
            original_scope = self.operation.idempotency_scope
            self.operation.idempotency_scope = (
                "another-caller" if denied == "scope" else original_scope
            )
            self.allowed.return_value = denied != "authority"
            url = self.url.replace("/fixture/", "/foreign/") if denied == "product" else self.url
            sent = await self.websocket(
                url + "/database",
                self.frames(b"database"),
                token="invalid" if denied == "token" else "fixture",
            )
            self.assertEqual(sent, [{"type": "websocket.close", "code": 1008, "reason": ""}])
            self.operation.idempotency_scope = original_scope
        self.assertFalse((self.fixture.directory / "database.partial").exists())
        self.store.write_native_testing_copy_capture.assert_not_called()

    async def test_disconnect_retains_prefix_and_resume_requires_its_original_hash(self) -> None:
        data = b"database"
        frames = self.frames(data, finish=False)
        frames[1]["bytes"] = data[:4]
        await self.websocket(self.url + "/database", frames)
        self.assertEqual((self.fixture.directory / "database.partial").read_bytes(), data[:4])
        sent = await self.websocket(self.url + "/database", self.frames(data, offset=4))
        self.assertEqual(sent[-1]["code"], 1000, sent)
        self.assertEqual((self.fixture.directory / "database").read_bytes(), data)

    async def test_opt_in_reads_prerequisites_without_changing_default_upload_response(
        self,
    ) -> None:
        from tests.support.http import request

        default = await request(self.app, "GET", self.url)
        projected = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(projected.status_code, 200, projected.text)
        fields = projected.json()
        prerequisites = fields.pop("prerequisites")
        self.assertEqual(fields, default.json())
        assert self.allocation.application is not None
        self.assertEqual(prerequisites["candidate_phase"], self.allocation.application.phase)
        self.assertIsNone(prerequisites["restore_workflow_started"])
        self.assertFalse(prerequisites["holds"])
        self.assertEqual(fields["operation_id"], self.operation.operation_id)
        self.assertEqual(fields["source_binding_sha256"], self.fixture.source.binding_sha256)

    async def test_foreign_authorized_read_reports_original_caller_hold(self) -> None:
        from tests.support.http import request

        self.operation.idempotency_scope = "foreign-caller"
        read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["prerequisites"]["holds"], ["original_caller_required"])
        self.assertFalse(list(self.fixture.directory.iterdir()))

    async def test_terminal_and_started_candidates_remain_readable_but_refuse_upload(self) -> None:
        from datetime import UTC, datetime
        from control_plane.contracts.testing_candidate_application import (
            TestingCandidateApplication,
            TestingCandidatePrepared,
        )
        from tests.support.http import request

        initial = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["prerequisites"]["holds"])
        original = self.allocation
        assert original.application is not None
        plan = original.application.plan
        prepared = TestingCandidatePrepared(
            plan_sha256=plan.digest,
            runtime_identity=plan.runtime_identity,
            private_endpoint="http://candidate.invalid",
            deployment_id="deployment",
            source_witness="fixture-source",
            integration_isolation_verified=True,
            credentials_verified=True,
            observed_at=datetime.now(UTC),
        )
        for changes in (
            {"phase": "restoring", "deployment_id": "deployment", "restore_workflow_started": None},
            {
                "phase": "restoring",
                "deployment_id": "deployment",
                "restore_workflow_started": False,
            },
            {"phase": "restoring", "deployment_id": "deployment", "restore_workflow_started": True},
            {"phase": "prepared", "deployment_id": "deployment", "prepared": prepared},
            {"phase": "abandoned", "abandonment_evidence": "fixture-custody"},
            {
                "phase": "abandoned",
                "abandonment_evidence": "fixture-custody",
                "restore_workflow_started": True,
            },
        ):
            application = TestingCandidateApplication.model_validate({"plan": plan, **changes})
            self.allocation = original.model_copy(update={"application": application})
            with self.subTest(
                phase=application.phase, started=application.restore_workflow_started
            ):
                read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
                self.assertEqual(read.status_code, 200, read.text)
                evidence = read.json()["prerequisites"]
                self.assertEqual(evidence["candidate_phase"], application.phase)
                self.assertIs(
                    evidence["restore_workflow_started"], application.restore_workflow_started
                )
                if application.phase == "abandoned":
                    self.assertIn("candidate_abandoned", evidence["holds"])
                if application.phase == "prepared":
                    self.assertIn("candidate_prepared", evidence["holds"])
                if application.restore_workflow_started:
                    self.assertIn("restore_started", evidence["holds"])
                if (
                    application.phase in {"prepared", "abandoned"}
                    or application.restore_workflow_started
                ):
                    sent = await self.websocket(self.url + "/database", self.frames(b"database"))
                    self.assertEqual(sent[0]["type"], "websocket.close")
                    self.assertEqual(sent[0]["code"], 1008)
        self.assertFalse(list(self.fixture.directory.iterdir()))
        self.store.write_native_testing_copy_capture.assert_not_called()

    async def test_missing_unbound_and_refused_reads_supply_no_prerequisite_claim(self) -> None:
        from tests.support.http import request

        for url, expected_status in (
            (self.url.replace("/fixture/", "/foreign/"), 403),
            (self.url.replace("/original/", "/foreign/"), 409),
        ):
            read = await request(self.app, "GET", url + "?include_prerequisites=true")
            self.assertEqual(read.status_code, expected_status, read.text)
        self.allowed.return_value = False
        read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(read.status_code, 403)
        self.allowed.return_value = True
        reader = self.store.read_testing_candidate
        original_read = reader.side_effect
        reader.side_effect = None
        for candidate in (None, self.allocation.model_copy(update={"application": None})):
            reader.return_value = candidate
            read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
            self.assertEqual(read.status_code, 409, read.text)
        from control_plane.contracts.testing_candidate import TestingCandidateAllocation

        incomplete = self.allocation.model_dump(mode="json")
        incomplete["application"]["phase"] = "unknown-phase"
        reader.side_effect = lambda _key: TestingCandidateAllocation.model_validate(incomplete)
        read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(read.status_code, 409, read.text)
        reader.side_effect = original_read
        self.policy.side_effect = LookupError("unavailable")
        read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(read.status_code, 503)
        self.policy.side_effect = None
        self.operation.request = MigratedTestingRequestTests().request(
            migrated_source=self.fixture.source.model_copy(
                update={"producer_run": "different-source"}
            )
        )
        read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
        self.assertEqual(read.status_code, 409, read.text)
        self.store.write_native_testing_copy_capture.assert_not_called()

    async def test_parts_and_receipt_reads_have_no_upload_reconciliation_or_record_writes(
        self,
    ) -> None:
        from unittest.mock import patch
        from control_plane.http_routes import migrated_testing_source as routes
        from control_plane.contracts.odoo_testing_source import NativeTestingCopyCapture
        from control_plane.odoo_testing_restore import generation_binding
        from tests.support.http import request

        assert self.allocation.application is not None
        generation = self.allocation.application.plan.generation
        assert generation is not None
        source = self.fixture.source
        with (
            patch.object(
                routes, "receive_migrated_part", side_effect=AssertionError("read invoked receiver")
            ),
            patch.object(
                routes,
                "finish_migrated_upload",
                side_effect=AssertionError("read reconciled receipt"),
            ),
        ):
            for database, filestore, receipt in (
                (b"data", None, False),
                (b"database", None, False),
                (b"database", b"store", False),
                (b"database", b"store", True),
            ):
                for part, data in (("database", database), ("filestore", filestore)):
                    if data is not None:
                        name = (
                            part if len(data) == getattr(source, part).size else part + ".partial"
                        )
                        (self.fixture.directory / name).write_bytes(data)
                if receipt:
                    self.store.read_native_testing_copy_capture.return_value = (
                        NativeTestingCopyCapture(
                            product=source.product,
                            context=source.context,
                            generation_binding_sha256=generation_binding(generation),
                            source_binding_sha256=source.binding_sha256,
                            capture_id="c" * 32,
                            database=source.database,
                            filestore=source.filestore,
                            captured_at="2026-11-01T00:00:00Z",
                            source_observation=source,
                        )
                    )
                before = {path.name: path.read_bytes() for path in self.fixture.directory.iterdir()}
                self.store.reset_mock()
                read = await request(self.app, "GET", self.url + "?include_prerequisites=true")
                self.assertEqual(read.status_code, 200, read.text)
                payload = read.json()
                self.assertIs(payload["receipt_recorded"], receipt)
                for part, data in (("database", database), ("filestore", filestore)):
                    self.assertEqual(
                        payload["parts"][part],
                        {
                            "complete": data is not None
                            and len(data) == getattr(source, part).size,
                            "size": len(data) if data is not None else 0,
                        },
                    )
                self.assertEqual(
                    before,
                    {path.name: path.read_bytes() for path in self.fixture.directory.iterdir()},
                )
                self.store.write_native_testing_copy_capture.assert_not_called()
                self.store.write_odoo_stable_target_replacement_operation_record.assert_not_called()
                self.store.compare_and_write_testing_candidate.assert_not_called()
