"""Behavior proof for private native source custody and bounded transport."""

import base64
from collections import deque
from collections.abc import Mapping, Callable
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import select
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory, NamedTemporaryFile
from typing import Any, cast
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_testing_source import (
    NativeOdooTestingSource,
    NativeTestingCopyArtifact,
    NativeTestingCopyCapture,
)
from control_plane.dokploy import native_copy_stream as transport
from control_plane.dokploy import api
from control_plane.odoo_testing_native_source import (
    capture_native_testing_source,
    prepare_native_testing_delivery,
    verify_native_capture_files,
)
from control_plane.odoo_testing_restore import generation_binding, NATIVE_TESTING_FILES_PROGRAM
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_bootstrap import (
    TestingReplacementBootstrapRequest,
    bootstrap_request_sha256,
    bootstrap_wire_payload,
)
from tests.test_odoo_testing_restore import generation


def descriptor() -> NativeOdooTestingSource:
    return NativeOdooTestingSource(
        product="example-odoo",
        context="example",
        target_id="source",
        database_name="production",
        filestore_path="/volumes/data/filestore/production",
        settings_sha256="e" * 64,
        server_id="registered-source",
        compose_project="source-project",
        database_container="a" * 64,
        web_container="b" * 64,
        database_image="sha256:" + "c" * 64,
        web_image="sha256:" + "d" * 64,
    )


class Connection:
    """A fragmented framed provider, reacting to the actual emitted handshake."""

    def __init__(self, frame: Mapping[str, object] | None = None):
        self.pending: deque[str] = deque(["Connecting...\n", "\x1bcroot@fixture# "])
        self.sent: list[str] = []
        self.frame = frame or {"type": "complete", "probe": True}

    def recv(self, timeout: float | None = None) -> str:
        if not self.pending:
            raise TimeoutError()
        return self.pending.popleft()

    def send(self, message: str) -> None:
        self.sent.append(message)
        if message.startswith("HISTFILE="):
            ready = shlex.split(message)[-2]
            self.prefix = shlex.split(message)[-1]
            self.pending.extend(["\r\n" + ready[:8], ready[8:] + "\r\n"])
        elif message.startswith("{"):
            payload = self.prefix + json.dumps(self.frame) + "\r\n"
            self.pending.extend([payload[:7], payload[7:31], payload[31:]])


class NativeSourceTransportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "The registered terminal transport uses a POSIX tty")
    def test_long_private_payload_is_read_after_loader_disables_tty_echo_and_canonical_mode(
        self,
    ) -> None:
        import pty

        master, slave = pty.openpty()
        # A controlled fixture shell, using this test's interpreter, accepts only
        # the emitted exec-loader command and replaces itself with that loader.
        shell = "import os,shlex,sys; print('Connecting...\\n\\x1bcfixture# ',end='',flush=True); args=shlex.split(sys.stdin.readline()); args=args[args.index('exec')+1:]; os.execv(sys.executable,[sys.executable,*args[1:]])"
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", shell], stdin=slave, stdout=slave, stderr=slave
        )
        os.close(slave)
        raw = bytearray()

        class Terminal:
            def recv(self, timeout: float | None = None) -> bytes:
                readable, _, _ = select.select([master], [], [], timeout)
                if not readable:
                    raise TimeoutError()
                data = os.read(master, 65536)
                raw.extend(data)
                return data

            def send(self, message: str) -> None:
                data = message.encode()
                while data:
                    data = data[os.write(master, data) :]

        frames: list[dict[str, object]] = []
        payload: dict[str, object] = {"fixture": "private-fixture" * 5000}
        program = "import sys,json,base64; value=json.loads(base64.b64decode(sys.argv[1])); print(sys.argv[2]+json.dumps({'type':'complete','length':len(value['fixture'])}),flush=True)"
        try:
            transport._read_program(
                Terminal(),
                program=program,
                payload=payload,
                timeout_seconds=10,
                receive=frames.append,
            )
            child.wait(timeout=5)
            self.assertEqual(frames, [{"type": "complete", "length": len(str(payload["fixture"]))}])
            self.assertLess(len(raw), 4096)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            os.close(master)

    def test_upload_journal_contains_only_metadata_and_provider_error_body_stays_private(
        self,
    ) -> None:
        payloads: list[api.JsonObject] = []

        def observe(
            host: str, path: str, payload: api.JsonObject, send: Callable[[], api.JsonValue]
        ) -> api.JsonValue:
            payloads.append(payload)
            return send()

        error = HTTPError(
            "https://provider.invalid",
            403,
            "refused",
            Message(),
            io.BytesIO(b"PRIVATE_ARTIFACT_CONTENT"),
        )
        opener = Mock()
        opener.open.side_effect = error
        with (
            api.observe_dokploy_mutations(observe),
            patch("control_plane.dokploy.api.build_opener", return_value=opener),
        ):
            with self.assertRaises(transport.NativeSourceAuthorizationDenied) as caught:
                api.upload_native_testing_chunk(
                    host="https://provider.invalid",
                    token="fixture-token",
                    server_id="receiver",
                    container_id="e" * 64,
                    filename="lp-native-" + "f" * 32 + "-database-0",
                    content=b"PRIVATE_ARTIFACT_CONTENT",
                )
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("PRIVATE_ARTIFACT_CONTENT", json.dumps(payloads))
        self.assertNotIn("fixture-token", json.dumps(payloads))
        self.assertNotIn("PRIVATE_ARTIFACT_CONTENT", str(caught.exception))
        self.assertEqual(
            payloads[0]["sha256"], hashlib.sha256(b"PRIVATE_ARTIFACT_CONTENT").hexdigest()
        )

    def test_fragmented_terminal_requires_complete_witness_and_keeps_token_out_of_program(
        self,
    ) -> None:
        connection = Connection()
        connect = Mock()
        connect.return_value.__enter__ = Mock(return_value=connection)
        connect.return_value.__exit__ = Mock(return_value=False)
        frames: list[dict[str, object]] = []
        with patch.object(transport, "connect", connect):
            transport.read_native_source(
                host="https://provider.invalid",
                token="private-fixture",
                server_id="registered-source",
                source=descriptor().model_dump(mode="json"),
                mode="probe",
                receive=frames.append,
            )
        self.assertEqual(frames, [{"type": "complete", "probe": True}])
        self.assertEqual(len(connect.call_args_list), 1)
        self.assertNotIn("private-fixture", "".join(connection.sent))
        self.assertIsNone(connect.call_args.kwargs["proxy"])
        self.assertTrue(connect.call_args.kwargs["logger"].disabled)

    def test_explicit_refusal_stops_without_second_connection_or_error_contents(self) -> None:
        connection = Connection({"type": "denied", "private": "secret-fixture"})
        with self.assertRaises(transport.NativeSourceAuthorizationDenied) as caught:
            transport._read_program(
                connection,
                program=transport.NATIVE_ODOO_SOURCE_READ_PROGRAM,
                payload={},
                timeout_seconds=5,
                receive=lambda _: None,
            )
        self.assertEqual(caught.exception.code, "authorization_denied")
        self.assertNotIn("secret-fixture", str(caught.exception))
        self.assertNotIn("exit\n", connection.sent)

    def test_truncation_and_oversize_never_supply_completion(self) -> None:
        for value in (
            {"type": "data", "part": "database", "data": "Zml4dHVyZQ=="},
            {"type": "data", "private": "x" * (129 * 1024)},
        ):
            with self.subTest(kind=value["type"], oversize="private" in value):
                connection = Connection(value)
                with self.assertRaises((transport.NativeSourceUnavailable, TimeoutError)):
                    transport._read_program(
                        connection,
                        program=transport.NATIVE_ODOO_SOURCE_READ_PROGRAM,
                        payload={},
                        timeout_seconds=5,
                        receive=lambda _: None,
                    )

    def test_local_key_setup_and_untrusted_provider_urls_are_refused_before_connect(self) -> None:
        with patch.object(transport, "connect") as connect:
            for host, server in [
                ("https://provider.invalid", "local"),
                ("https://user:password@provider.invalid", "source"),
                ("https://provider.invalid/other", "source"),
            ]:
                with self.assertRaises(transport.NativeSourceUnavailable):
                    transport.read_native_source(
                        host=host,
                        token="fixture",
                        server_id=server,
                        source={},
                        mode="probe",
                        receive=lambda _: None,
                    )
            connect.assert_not_called()


class NativeCopyCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.enterContext(patch.dict(os.environ, {"LAUNCHPLANE_STATE_DIR": str(self.root)}))
        self.source = descriptor()
        self.generation = generation().model_copy(
            update={
                "source_mode": "container-export",
                "source_binding_sha256": self.source.binding_sha256,
            }
        )
        self.store = Mock(spec=PostgresRecordStore)
        self.store.read_native_testing_copy_capture.return_value = None
        self.data = {"database": b"custom database fixture", "filestore": b"filestore fixture"}
        self.enterContext(
            patch(
                "control_plane.odoo_testing_native_source.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-token"),
            )
        )

    def stream(self, **kwargs: Any) -> None:
        receive = kwargs["receive"]
        receipts = {}
        for part, data in self.data.items():
            receive({"type": "data", "part": part, "data": base64.b64encode(data).decode()})
            receipts[part] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        receive({"type": "complete", **receipts})

    def capture(self) -> NativeTestingCopyCapture:
        return capture_native_testing_source(
            cast(PostgresRecordStore, self.store),
            root=self.root,
            generation=self.generation,
            descriptor=self.source,
        )

    def test_receipt_is_published_after_bytes_and_original_capture_is_reused(self) -> None:
        def persist(receipt: NativeTestingCopyCapture) -> None:
            files = verify_native_capture_files(receipt)
            self.assertEqual([path.read_bytes() for path in files], list(self.data.values()))
            self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in files))
            self.store.read_native_testing_copy_capture.return_value = receipt

        self.store.write_native_testing_copy_capture.side_effect = persist
        with patch(
            "control_plane.odoo_testing_native_source.read_native_source", side_effect=self.stream
        ) as read:
            capture = self.capture()
            self.assertEqual(self.capture(), capture)
            self.assertEqual(read.call_count, 1)
            files = verify_native_capture_files(capture)
            files[0].write_bytes(b"changed")
            with self.assertRaises(ValueError):
                self.capture()
            self.assertEqual(read.call_count, 1)

    def test_incomplete_capture_never_records_receipt_or_leaves_reusable_bytes(self) -> None:
        def incomplete(**kwargs: Any) -> None:
            kwargs["receive"](
                {"type": "data", "part": "database", "data": base64.b64encode(b"private").decode()}
            )
            raise transport.NativeSourceUnavailable("Incomplete fixture")

        with patch(
            "control_plane.odoo_testing_native_source.read_native_source", side_effect=incomplete
        ):
            with self.assertRaises(transport.NativeSourceUnavailable):
                self.capture()
        self.store.write_native_testing_copy_capture.assert_not_called()
        self.assertFalse(any(path.is_file() for path in self.root.rglob("*")))

    def test_database_witness_resumes_without_source_read_or_private_cache(self) -> None:
        artifacts = {
            part: NativeTestingCopyArtifact(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            for part, data in self.data.items()
        }
        receipt = NativeTestingCopyCapture(
            product=self.generation.product,
            context=self.generation.context,
            generation_binding_sha256=generation_binding(self.generation),
            source_binding_sha256=self.source.binding_sha256,
            capture_id="f" * 32,
            database=artifacts["database"],
            filestore=artifacts["filestore"],
            source_observation=self.source,
            captured_at="fixture",
        )
        self.store.read_native_testing_copy_capture.return_value = receipt

        def inspect(**kwargs: Any) -> None:
            kwargs["receive"]({"type": "complete", "runner": "e" * 64, "resume": True})

        with (
            patch(
                "control_plane.odoo_testing_native_source.inspect_native_testing_receiver",
                side_effect=inspect,
            ),
            patch("control_plane.odoo_testing_native_source.read_native_source") as read,
            patch(
                "control_plane.odoo_testing_native_source.api.upload_native_testing_chunk"
            ) as upload,
        ):
            actual = prepare_native_testing_delivery(
                cast(PostgresRecordStore, self.store),
                root=self.root,
                generation=self.generation,
                descriptor=self.source,
                host="https://provider.invalid",
                token="fixture",
                server_id="receiver",
                spec={},
                before_mutation=None,
            )
        self.assertEqual(actual, receipt)
        read.assert_not_called()
        upload.assert_not_called()

    def test_receiver_is_qualified_before_capture_and_false_upload_success_fails_readback(
        self,
    ) -> None:
        def inspect(**kwargs: Any) -> None:
            kwargs["receive"]({"type": "complete", "runner": "e" * 64, "resume": False})

        events: list[str] = []

        def stream(**kwargs: Any) -> None:
            events.append("capture")
            self.stream(**kwargs)

        def inspect_with_event(**kwargs: Any) -> None:
            events.append("inspection")
            inspect(**kwargs)

        with (
            patch(
                "control_plane.odoo_testing_native_source.inspect_native_testing_receiver",
                side_effect=inspect_with_event,
            ),
            patch(
                "control_plane.odoo_testing_native_source.read_native_source", side_effect=stream
            ),
            patch(
                "control_plane.odoo_testing_native_source.api.upload_native_testing_chunk"
            ) as upload,
        ):
            with self.assertRaisesRegex(ValueError, "actual receiver readback"):
                prepare_native_testing_delivery(
                    cast(PostgresRecordStore, self.store),
                    root=self.root,
                    generation=self.generation,
                    descriptor=self.source,
                    host="https://provider.invalid",
                    token="fixture",
                    server_id="receiver",
                    spec={},
                    before_mutation=None,
                )
        self.assertLess(events.index("inspection"), events.index("capture"))
        self.assertTrue(upload.called)

    def test_retry_repairs_only_proven_prefix_of_original_chunk(self) -> None:
        with patch(
            "control_plane.odoo_testing_native_source.read_native_source", side_effect=self.stream
        ):
            receipt = self.capture()
        self.store.read_native_testing_copy_capture.return_value = receipt
        for prefix_matches in (True, False):
            with self.subTest(prefix_matches=prefix_matches):
                prefix = self.data["database"][:5] if prefix_matches else b"wrong"
                observed = {
                    ("database", 0): {
                        "type": "chunk",
                        "part": "database",
                        "index": 0,
                        "size": len(prefix),
                        "sha256": hashlib.sha256(prefix).hexdigest(),
                    }
                }

                def inspect(**kwargs: Any) -> None:
                    for value in observed.values():
                        kwargs["receive"](value)
                    kwargs["receive"]({"type": "complete", "runner": "e" * 64, "resume": False})

                def upload(**kwargs: Any) -> None:
                    part = str(kwargs["filename"]).rsplit("-", 2)[1]
                    data = kwargs["content"]
                    observed[(part, 0)] = {
                        "type": "chunk",
                        "part": part,
                        "index": 0,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }

                with (
                    patch(
                        "control_plane.odoo_testing_native_source.inspect_native_testing_receiver",
                        side_effect=inspect,
                    ),
                    patch(
                        "control_plane.odoo_testing_native_source.api.upload_native_testing_chunk",
                        side_effect=upload,
                    ) as uploads,
                    patch("control_plane.odoo_testing_native_source.read_native_source") as read,
                ):
                    if prefix_matches:
                        self.assertEqual(
                            prepare_native_testing_delivery(
                                cast(PostgresRecordStore, self.store),
                                root=self.root,
                                generation=self.generation,
                                descriptor=self.source,
                                host="https://provider.invalid",
                                token="fixture",
                                server_id="receiver",
                                spec={},
                                before_mutation=None,
                            ),
                            receipt,
                        )
                        self.assertEqual(uploads.call_count, len(self.data))
                    else:
                        with self.assertRaisesRegex(ValueError, "existing source chunk differs"):
                            prepare_native_testing_delivery(
                                cast(PostgresRecordStore, self.store),
                                root=self.root,
                                generation=self.generation,
                                descriptor=self.source,
                                host="https://provider.invalid",
                                token="fixture",
                                server_id="receiver",
                                spec={},
                                before_mutation=None,
                            )
                        uploads.assert_not_called()
                    read.assert_not_called()


class NativeIdentityCompatibilityTests(unittest.TestCase):
    def test_existing_generation_hash_is_unchanged_by_defaulted_new_fields(self) -> None:
        record = generation()
        old_payload = record.model_dump(
            mode="json",
            exclude={
                "completed_at",
                "reconcile_requested_at",
                "source_mode",
                "source_binding_sha256",
            },
        )
        self.assertEqual(generation_binding(record), canonical_json_sha256(old_payload))
        native = record.model_copy(
            update={
                "source_mode": "container-export",
                "source_binding_sha256": descriptor().binding_sha256,
            }
        )
        self.assertNotEqual(generation_binding(native), generation_binding(record))

    def test_existing_wire_and_request_identity_preserve_old_recovery_keys(self) -> None:
        request = TestingReplacementBootstrapRequest(
            context="example",
            operation_id="fixture-operation",
            domain_host="testing.example.invalid",
            reason="fixture",
        )
        old_wire = request.model_dump(mode="json", exclude={"source_mode"})
        self.assertEqual(bootstrap_wire_payload(request), old_wire)
        old_identity = {
            key: value
            for key, value in old_wire.items()
            if key not in {"mode", "reviewed_plan_sha256"}
        }
        self.assertEqual(bootstrap_request_sha256(request), canonical_json_sha256(old_identity))
        native = request.model_copy(update={"source_mode": "container-export"})
        self.assertNotEqual(bootstrap_request_sha256(native), bootstrap_request_sha256(request))


class NativeArtifactConsumptionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity = "f" * 32
        self.namespace = generation().namespace
        self.database = "testing_copy"
        self.dump_path = "/tmp/" + self.namespace + "-" + "a" * 32 + "-database.dump"
        (self.root / "tmp").mkdir()
        self.attachment = b"fixture attachment"
        digest = hashlib.sha1(self.attachment).hexdigest()
        self.name = digest[:2] + "/" + digest

    def mapped_path(self, value: str) -> Path:
        path = Path(value)
        if str(path).startswith("/tmp/") or str(path) == "/tmp":
            return self.root / "tmp" / path.relative_to("/tmp")
        if str(path).startswith("/volumes/"):
            return self.root / path.relative_to("/")
        return path

    def stage(self, name: str | None = None, link: bool = False) -> dict[str, Any]:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as handle:
            member = tarfile.TarInfo(name or self.name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = "/elsewhere"
            else:
                member.size = len(self.attachment)
            handle.addfile(member, io.BytesIO(self.attachment) if not link else None)
        data = {"database": b"custom database fixture", "filestore": archive.getvalue()}
        for part, value in data.items():
            (self.root / "tmp" / f"lp-native-{self.identity}-{part}-0").write_bytes(value)
        return {
            "capture_id": self.identity,
            **{
                part: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
                for part, value in data.items()
            },
        }

    def consume(self, capture: dict[str, Any]) -> None:
        spec = {
            "mode": "prepare",
            "capture": capture,
            "database_name": self.database,
            "dump_path": self.dump_path,
        }

        def temporary_file(**kwargs: Any) -> Any:
            kwargs["dir"] = self.mapped_path(str(kwargs["dir"]))
            return NamedTemporaryFile(**kwargs)

        with (
            patch.object(
                sys, "argv", ["consume", base64.b64encode(json.dumps(spec).encode()).decode()]
            ),
            patch("pathlib.Path", side_effect=self.mapped_path),
            patch("tempfile.NamedTemporaryFile", side_effect=temporary_file),
        ):
            exec(compile(NATIVE_TESTING_FILES_PROGRAM, "<native-files>", "exec"), {})

    def test_verified_chunks_become_only_local_dump_and_content_addressed_filestore(self) -> None:
        capture = self.stage()
        self.consume(capture)
        self.assertEqual(self.mapped_path(self.dump_path).read_bytes(), b"custom database fixture")
        self.assertEqual(
            (self.root / "volumes/data/filestore" / self.database / self.name).read_bytes(),
            self.attachment,
        )
        self.assertFalse(list((self.root / "tmp").glob("*.artifact")))

    def test_tampered_chunk_refuses_before_any_attachment_or_database_is_restored(self) -> None:
        capture = self.stage()
        (self.root / "tmp" / f"lp-native-{self.identity}-database-0").write_bytes(b"changed")
        with self.assertRaises(RuntimeError):
            self.consume(capture)
        self.assertFalse(self.mapped_path(self.dump_path).exists())
        self.assertFalse((self.root / "volumes/data/filestore").exists())

    def test_unsafe_archive_and_wrong_attachment_names_are_refused(self) -> None:
        for name, link in [("../outside", False), (self.name, True), ("aa/" + "0" * 40, False)]:
            with self.subTest(name=name, link=link):
                capture = self.stage(name, link)
                with self.assertRaises(RuntimeError):
                    self.consume(capture)
                self.assertFalse(self.mapped_path(self.dump_path).exists())

    def test_existing_different_attachment_is_preserved_on_retry(self) -> None:
        capture = self.stage()
        path = self.root / "volumes/data/filestore" / self.database / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original testing evidence")
        with self.assertRaises(RuntimeError):
            self.consume(capture)
        self.assertEqual(path.read_bytes(), b"original testing evidence")


if __name__ == "__main__":
    unittest.main()
