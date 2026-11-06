import hashlib
import unittest
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from control_plane.contracts.testing_candidate import TestingCandidateAllocation

from pydantic import ValidationError

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
    def test_fresh_receiver_inspects_no_artifacts_before_the_first_stream(self) -> None:
        import io
        import json
        from contextlib import redirect_stdout
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        capture["source_observation"] = {"kind": "migrated"}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            fixture.consume(capture, mode="inspect")
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue())["artifacts"], {})

    def test_extraction_retry_removes_only_its_owned_interrupted_temporary(self) -> None:
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": len(fixture.attachment),
            "filestore_files": 1,
        }
        staging = fixture.root / "volumes/data" / (".lp-native-" + fixture.identity)
        staging.mkdir(parents=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                staging / (part + ".artifact")
            )
        filestore = fixture.root / "volumes/data/filestore" / fixture.database
        filestore.mkdir(parents=True)
        owned = filestore / (".lp-native-" + fixture.identity + "-interrupted")
        other_identity = f"{int(fixture.identity, 16) ^ 1:032x}"
        other = filestore / (".lp-native-" + other_identity + "-other")
        owned.write_bytes(b"partial")
        other.write_bytes(b"retained other owner")
        with self.assertRaises(SystemExit):
            fixture.consume(capture)
        self.assertFalse(owned.exists())
        self.assertEqual(other.read_bytes(), b"retained other owner")
        self.assertEqual(
            fixture.mapped_path(fixture.dump_path).read_bytes(), b"custom database fixture"
        )

    def test_checklist_subdirectories_are_preserved_in_the_full_filestore(self) -> None:
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage(name="checklist/" + fixture.name)
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": len(fixture.attachment),
            "filestore_files": 1,
        }
        staging = fixture.root / "volumes/data" / (".lp-native-" + fixture.identity)
        staging.mkdir(parents=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                staging / (part + ".artifact")
            )
        with self.assertRaises(SystemExit) as exited:
            fixture.consume(capture)
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(
            (
                fixture.root
                / "volumes/data/filestore"
                / fixture.database
                / "checklist"
                / fixture.name
            ).read_bytes(),
            fixture.attachment,
        )

    def test_complete_filestore_is_streamed_and_checked_before_database_publication(self) -> None:
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": len(fixture.attachment),
            "filestore_files": 1,
        }
        staging = fixture.root / "volumes/data" / (".lp-native-" + fixture.identity)
        staging.mkdir(parents=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                staging / (part + ".artifact")
            )
        with self.assertRaises(SystemExit) as exited:
            fixture.consume(capture)
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(
            fixture.mapped_path(fixture.dump_path).read_bytes(), b"custom database fixture"
        )
        self.assertEqual(
            (
                fixture.root / "volumes/data/filestore" / fixture.database / fixture.name
            ).read_bytes(),
            fixture.attachment,
        )

    def test_retry_does_not_allocate_another_copy_of_matching_attachments(self) -> None:
        from unittest.mock import patch
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": len(fixture.attachment),
            "filestore_files": 1,
        }
        staging = fixture.root / "volumes/data" / (".lp-native-" + fixture.identity)
        staging.mkdir(parents=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                staging / (part + ".artifact")
            )
        with self.assertRaises(SystemExit):
            fixture.consume(capture)
        fixture.mapped_path(fixture.dump_path).unlink()
        with (
            patch(
                "tests.test_odoo_testing_native_source.NamedTemporaryFile",
                side_effect=OSError("No room for a duplicate attachment"),
            ),
            self.assertRaises(SystemExit) as exited,
        ):
            fixture.consume(capture)
        self.assertEqual(exited.exception.code, 0)

    def test_incomplete_declared_inventory_does_not_publish_database(self) -> None:
        from tests.test_odoo_testing_native_source import NativeArtifactConsumptionTests

        fixture = NativeArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        capture["source_observation"] = {
            "kind": "migrated",
            "filestore_expanded_bytes": len(fixture.attachment),
            "filestore_files": 2,
        }
        staging = fixture.root / "volumes/data" / (".lp-native-" + fixture.identity)
        staging.mkdir(parents=True)
        for part in ("database", "filestore"):
            (fixture.root / "tmp" / f"lp-native-{fixture.identity}-{part}-0").rename(
                staging / (part + ".artifact")
            )
        with self.assertRaisesRegex(RuntimeError, "inventory is incomplete"):
            fixture.consume(capture)
        self.assertFalse(fixture.mapped_path(fixture.dump_path).exists())


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
        self.operation = SimpleNamespace(
            product="fixture",
            context="fixture",
            request=MigratedTestingRequestTests().request(),
            idempotency_scope=idempotency_scope(identity),
        )
        self.store.read_odoo_stable_target_replacement_operation_record.return_value = (
            self.operation
        )
        self.enterContext(
            patch.object(routes, "migrated_candidate", return_value=self.fixture.allocation)
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
