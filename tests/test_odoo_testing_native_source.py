"""Behavior proof for private native source custody and bounded transport."""

import base64
import click
from collections import deque
from collections.abc import Mapping, Callable
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
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.contracts.odoo_testing_source import (
    NativeOdooTestingSource,
    NativeTestingCopyArtifact,
    NativeTestingCopyCapture,
    NATIVE_TESTING_SOURCE_BOUND_CHECKPOINT,
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
    TestingReplacementBootstrapPlan,
    bootstrap_plan_payload,
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
    def test_dispatch_guard_can_refuse_after_handshake_without_sending_the_program(self) -> None:
        connection = Connection()
        before_refusal: list[str] = []

        def refuse(dispatch: Any) -> None:
            before_refusal.extend(connection.sent)
            raise transport.NativeSourceAuthorizationDenied("fixture revoked authority")

        with self.assertRaises(transport.NativeSourceAuthorizationDenied):
            transport._read_program(
                connection,
                program="fixture program",
                payload={},
                timeout_seconds=5,
                receive=lambda frame: None,
                dispatch_guard=refuse,
            )
        self.assertTrue(connection.sent)
        self.assertEqual(connection.sent, before_refusal)

    def test_guarded_send_deadline_aborts_the_connection(self) -> None:
        connection = Connection()
        abort = Mock()
        scheduled: list[Any] = []

        class Deadline:
            daemon = False

            def __init__(self, seconds: float, callback: Any):
                scheduled.append(callback)

            def start(self) -> None:
                pass

            def cancel(self) -> None:
                pass

            def join(self) -> None:
                pass

        original = connection.send

        def send(message: str) -> None:
            if scheduled:
                scheduled[0]()
            original(message)

        with (
            patch.object(transport, "Timer", Deadline),
            patch.object(connection, "send", side_effect=send),
        ):
            with self.assertRaises(transport.NativeSourceUnavailable):
                transport._read_program(
                    connection,
                    program="fixture",
                    payload={},
                    timeout_seconds=5,
                    receive=lambda frame: None,
                    dispatch_guard=lambda send: send(),
                    abort_send=abort,
                )
        abort.assert_called_once()

    def test_private_cache_requires_a_mount_outside_the_shared_backing(self) -> None:
        from control_plane.odoo_testing_native_source import require_private_copy_mount

        private = Path("/fixture/private-testing-source")
        shared = "1 0 0:1 /shared /fixture rw - ext4 fixture rw\n"
        for private_root in (None, "/shared", "/shared/private-testing-source", "/dedicated-copy"):
            mounts = shared + (
                ("2 1 0:1 " + private_root + " " + str(private) + " rw - ext4 fixture rw")
                if private_root
                else ""
            )
            with (
                self.subTest(private_root=private_root),
                patch("pathlib.Path.read_text", return_value=mounts),
            ):
                if private_root == "/dedicated-copy":
                    require_private_copy_mount(private)
                else:
                    with self.assertRaises(transport.NativeSourceUnavailable):
                        require_private_copy_mount(private)

    def test_source_runtime_must_belong_to_the_registered_product_and_lane(self) -> None:
        source = descriptor().model_dump(mode="json")
        for changed in (
            {"product": "other-product"},
            {"context": "other-context"},
            {"instance": "testing"},
            {},
        ):
            identity = {
                "product": source["product"],
                "context": source["context"],
                "instance": "prod",
                "environment_kind": "stable",
                **changed,
            }
            rows = []
            for role in ("database", "web"):
                rows.append(
                    {
                        "State": {"Running": True},
                        "Image": source[role + "_image"],
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": source["compose_project"],
                                "com.docker.compose.service": role,
                            },
                            "Env": [
                                "ODOO_DB_NAME=" + str(source["database_name"]),
                                "ODOO_DB_HOST=database",
                                "PLATFORM_INSTANCE=prod",
                                RUNTIME_IDENTITY_ENV_KEY + "=" + json.dumps(identity),
                            ],
                        },
                    }
                )
            spec = base64.b64encode(
                json.dumps({"source": source, "mode": "probe"}).encode()
            ).decode()
            output = io.StringIO()
            with (
                self.subTest(changed=changed),
                patch.object(sys, "argv", ["source", spec, "fixture:"]),
                patch(
                    "subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess(
                            [], 0, stdout=json.dumps([row]).encode(), stderr=b""
                        )
                        for row in rows
                    ]
                    + [subprocess.CompletedProcess([], 0, stdout=b"1", stderr=b"")] * 2,
                ) as commands,
                patch("subprocess.Popen") as export,
                redirect_stdout(output),
            ):
                if changed:
                    with self.assertRaises(SystemExit):
                        exec(
                            compile(
                                transport.NATIVE_ODOO_SOURCE_READ_PROGRAM, "<source-read>", "exec"
                            ),
                            {},
                        )
                    self.assertEqual(commands.call_count, len(rows))
                else:
                    exec(
                        compile(transport.NATIVE_ODOO_SOURCE_READ_PROGRAM, "<source-read>", "exec"),
                        {},
                    )
                    self.assertGreater(commands.call_count, len(rows))
                export.assert_not_called()
            self.assertEqual(
                json.loads(output.getvalue().removeprefix("fixture:"))["type"],
                "failed" if changed else "complete",
            )

    def test_chunk_delivery_uses_only_stdin_and_preserves_different_receiver_bytes(self) -> None:
        content = b"PRIVATE_ARTIFACT_CONTENT"
        filename = "lp-native-" + "f" * 32 + "-database-0"
        with TemporaryDirectory() as directory:
            receiver = Path(directory)
            arguments_seen = []

            def docker_exec(arguments: list[str], **kwargs: Any) -> Any:
                arguments_seen.append(arguments)
                self.assertNotIn(content.decode(), " ".join(arguments))
                self.assertNotIn(base64.b64encode(content).decode(), " ".join(arguments))
                result = io.StringIO()
                with (
                    patch.object(sys, "argv", ["writer", arguments[-1]]),
                    patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(kwargs["input"]))),
                    patch("pathlib.Path", return_value=receiver),
                    redirect_stdout(result),
                ):
                    exec(compile(arguments[-2], "<receiver-writer>", "exec"), {})
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=result.getvalue().encode(), stderr=b""
                )

            for data in (content, b"different receiver attempt"):
                spec = {
                    "container": "e" * 64,
                    "filename": filename,
                    "content": base64.b64encode(data).decode(),
                }
                output = io.StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "fixture",
                            base64.b64encode(json.dumps(spec).encode()).decode(),
                            "fixture:",
                        ],
                    ),
                    patch("subprocess.run", side_effect=docker_exec),
                    redirect_stdout(output),
                ):
                    exec(
                        compile(
                            transport.NATIVE_TESTING_CHUNK_WRITE_PROGRAM, "<native-upload>", "exec"
                        ),
                        {},
                    )
                frame = json.loads(output.getvalue().removeprefix("fixture:"))
                self.assertEqual(frame["type"], "complete" if data == content else "upload_failed")
                self.assertEqual((receiver / filename).read_bytes(), content)
            self.assertTrue(arguments_seen)
            self.assertEqual((receiver / filename).stat().st_mode & 0o777, 0o600)

    def test_source_program_refuses_wrong_compose_before_export_without_private_output(
        self,
    ) -> None:
        source = descriptor().model_dump(mode="json")
        row = {
            "State": {"Running": True},
            "Image": source["database_image"],
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "another-project",
                    "com.docker.compose.service": "database",
                },
                "Env": ["PRIVATE=fixture-only"],
            },
        }
        output = io.StringIO()
        spec = base64.b64encode(json.dumps({"source": source, "mode": "capture"}).encode()).decode()
        with (
            patch.object(sys, "argv", ["source", spec, "fixture:"]),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps([row]).encode(), stderr=b""
                ),
            ),
            patch("subprocess.Popen") as export,
            redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit):
                exec(
                    compile(transport.NATIVE_ODOO_SOURCE_READ_PROGRAM, "<source-read>", "exec"), {}
                )
        export.assert_not_called()
        self.assertEqual(json.loads(output.getvalue().removeprefix("fixture:")), {"type": "failed"})

    def test_source_file_permission_error_is_capability_failure_without_identity_fallback(
        self,
    ) -> None:
        spec = base64.b64encode(
            json.dumps({"source": descriptor().model_dump(mode="json"), "mode": "probe"}).encode()
        ).decode()
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["source", spec, "fixture:"]),
            patch("subprocess.run", side_effect=PermissionError("private fixture path")),
            redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit):
                exec(
                    compile(transport.NATIVE_ODOO_SOURCE_READ_PROGRAM, "<source-read>", "exec"), {}
                )
        self.assertEqual(json.loads(output.getvalue().removeprefix("fixture:")), {"type": "failed"})

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
        sent: list[str] = []

        class Terminal:
            def recv(self, timeout: float | None = None) -> bytes:
                readable, _, _ = select.select([master], [], [], timeout)
                if not readable:
                    raise TimeoutError()
                data = os.read(master, 65536)
                raw.extend(data)
                return data

            def send(self, message: str) -> None:
                sent.append(message)
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
            private = json.loads(next(message for message in sent if message.startswith("{")))[
                "payload"
            ]
            self.assertNotIn(private[:512].encode(), raw)
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

        with (
            api.observe_dokploy_mutations(observe),
            patch(
                "control_plane.dokploy.native_copy_stream.write_native_testing_chunk",
                side_effect=transport.NativeSourceAuthorizationDenied("fixture refusal"),
            ),
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
        self.assertNotIn("secret-fixture", str(caught.exception))
        self.assertNotIn("exit\n", connection.sent)

    def test_login_banner_is_not_an_authorization_refusal(self) -> None:
        connection = Connection()
        connection.pending = deque(
            ["Connecting...\n", "\x1bcUnauthorized access is prohibited.\nfixture# "]
        )
        frames: list[dict[str, object]] = []
        transport._read_program(
            connection,
            program=transport.NATIVE_ODOO_SOURCE_READ_PROGRAM,
            payload={},
            timeout_seconds=5,
            receive=frames.append,
        )
        self.assertTrue(frames[-1]["probe"])

    def test_provider_authentication_failure_before_clear_sends_no_program(self) -> None:
        connection = Connection()
        connection.pending = deque(
            [
                "Connecting...\n",
                "Authentication failed: Unauthorized private SSH key or username.\n",
            ]
        )
        with self.assertRaises(transport.NativeSourceAuthorizationDenied):
            transport._read_program(
                connection,
                program=transport.NATIVE_ODOO_SOURCE_READ_PROGRAM,
                payload={},
                timeout_seconds=5,
                receive=lambda _: None,
            )
        self.assertFalse(connection.sent)

    def test_truncation_never_supplies_completion(self) -> None:
        connection = Connection({"type": "data", "part": "database", "data": "Zml4dHVyZQ=="})
        with self.assertRaises(TimeoutError):
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
        self.enterContext(
            patch("control_plane.odoo_testing_native_source.require_private_copy_mount")
        )
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

    def test_lost_receipt_commit_reply_retains_published_bytes_for_resume(self) -> None:
        def committed(receipt: NativeTestingCopyCapture) -> None:
            self.store.read_native_testing_copy_capture.return_value = receipt
            raise OSError("fixture lost commit reply")

        self.store.write_native_testing_copy_capture.side_effect = committed
        with patch(
            "control_plane.odoo_testing_native_source.read_native_source", side_effect=self.stream
        ):
            with self.assertRaises(OSError):
                self.capture()
        receipt = self.store.read_native_testing_copy_capture.return_value
        self.assertTrue(all(path.is_file() for path in verify_native_capture_files(receipt)))
        with patch("control_plane.odoo_testing_native_source.read_native_source") as read:
            self.assertEqual(self.capture(), receipt)
            read.assert_not_called()

    def test_source_capacity_refusal_is_checkpointed_before_upload(self) -> None:
        checkpoint = Mock()

        def inspect(**kwargs: Any) -> None:
            kwargs["receive"]({"type": "complete", "runner": "e" * 64, "resume": False})

        with (
            patch(
                "control_plane.odoo_testing_native_source.inspect_native_testing_receiver",
                side_effect=inspect,
            ),
            patch(
                "control_plane.odoo_testing_native_source.capture_native_testing_source",
                side_effect=transport.NativeSourceExceedsBound("fixture source capacity"),
            ),
            patch(
                "control_plane.odoo_testing_native_source.api.upload_native_testing_chunk"
            ) as upload,
        ):
            with self.assertRaises(transport.NativeSourceExceedsBound):
                prepare_native_testing_delivery(
                    cast(PostgresRecordStore, self.store),
                    root=self.root,
                    generation=self.generation,
                    descriptor=self.source,
                    host="https://provider.invalid",
                    token="fixture",
                    server_id="receiver",
                    spec={},
                    before_mutation=checkpoint,
                )
        checkpoint.assert_called_once_with(NATIVE_TESTING_SOURCE_BOUND_CHECKPOINT)
        upload.assert_not_called()

    def test_complete_receiver_artifacts_do_not_require_private_cache_or_recapture(self) -> None:
        with patch(
            "control_plane.odoo_testing_native_source.read_native_source", side_effect=self.stream
        ):
            receipt = self.capture()
        self.store.read_native_testing_copy_capture.return_value = receipt
        for path in verify_native_capture_files(receipt):
            path.unlink()

        def inspect(**kwargs: Any) -> None:
            kwargs["receive"](
                {
                    "type": "complete",
                    "runner": "e" * 64,
                    "resume": False,
                    "artifacts": {part: getattr(receipt, part).model_dump() for part in self.data},
                }
            )

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
            read.assert_not_called()
            upload.assert_not_called()

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
            with self.assertRaises(ValueError):
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
                        with self.assertRaises(ValueError):
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
    def test_ssh_storage_still_accepts_the_previous_strict_generation_shape(self) -> None:
        from pydantic import create_model, ConfigDict

        record = generation()
        legacy = create_model(
            "LegacyTestingGeneration",
            __config__=ConfigDict(extra="forbid"),
            **cast(
                dict[str, Any],
                {
                    name: (field.annotation, field)
                    for name, field in type(record).model_fields.items()
                    if name not in {"source_mode", "source_binding_sha256"}
                },
            ),
        )
        accepted = legacy.model_validate(PostgresRecordStore._payload_dict(record))
        self.assertEqual(accepted.model_dump()["target_id"], record.target_id)

    def test_ssh_response_still_validates_with_the_previous_strict_client_shape(self) -> None:
        from pydantic import create_model, ConfigDict

        legacy = create_model(
            "LegacyBootstrapPlan",
            __config__=ConfigDict(extra="forbid"),
            **cast(
                dict[str, Any],
                {
                    name: (field.annotation, field)
                    for name, field in TestingReplacementBootstrapPlan.model_fields.items()
                    if name not in {"source_mode", "source_binding_sha256"}
                },
            ),
        )
        plan = TestingReplacementBootstrapPlan(
            product="example",
            context="example",
            operation_id="fixture",
            domain_host="testing.example.invalid",
            plan_sha256="a" * 64,
            resources_binding_sha256="b" * 64,
        )
        accepted = legacy.model_validate(bootstrap_plan_payload(plan))
        self.assertEqual(accepted.model_dump()["plan_sha256"], plan.plan_sha256)
        native = plan.model_copy(
            update={
                "source_mode": "container-export",
                "source_binding_sha256": descriptor().binding_sha256,
            }
        )
        self.assertEqual(
            TestingReplacementBootstrapPlan.model_validate(bootstrap_plan_payload(native)), native
        )

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
        old_wire = request.model_dump(mode="json", exclude={"source_mode", "initial_generation"})
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
        if path.is_relative_to(self.root):
            return path
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

    def consume(
        self,
        capture: dict[str, Any],
        mode: str = "prepare",
        program: str = NATIVE_TESTING_FILES_PROGRAM,
    ) -> None:
        spec = {
            "mode": mode,
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
            exec(compile(program, "<native-files>", "exec"), {})

    def test_capacity_increase_accepts_the_exact_bound_for_inspection_and_restore(self) -> None:
        from control_plane.odoo_testing_restore import render_native_testing_files_program

        capture = self.stage()
        capacity = max(capture[part]["size"] for part in ("database", "filestore"))
        with patch(
            "control_plane.odoo_testing_restore.NATIVE_TESTING_COPY_ARTIFACT_BYTES", capacity - 1
        ):
            with self.assertRaises(RuntimeError):
                self.consume(capture, mode="inspect", program=render_native_testing_files_program())
        with patch(
            "control_plane.odoo_testing_restore.NATIVE_TESTING_COPY_ARTIFACT_BYTES", capacity
        ):
            program = render_native_testing_files_program()
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                self.consume(capture, mode="inspect", program=program)
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(
                json.loads(output.getvalue())["artifacts"],
                {part: capture[part] for part in ("database", "filestore")},
            )
            self.consume(capture, program=program)
        self.assertEqual(self.mapped_path(self.dump_path).read_bytes(), b"custom database fixture")
        self.assertEqual(
            (self.root / "volumes/data/filestore" / self.database / self.name).read_bytes(),
            self.attachment,
        )

    def test_inspection_hashes_actual_complete_artifacts_and_excludes_missing_chunks(self) -> None:
        capture = self.stage()
        for missing in (False, True):
            if missing:
                (self.root / "tmp" / f"lp-native-{self.identity}-database-0").unlink()
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                self.consume(capture, mode="inspect")
            self.assertEqual(stopped.exception.code, 0)
            observed = json.loads(output.getvalue())
            expected = (
                {part: capture[part] for part in ("filestore",) if missing}
                if missing
                else {part: capture[part] for part in ("database", "filestore")}
            )
            self.assertEqual(observed["artifacts"], expected)
            self.assertFalse(self.mapped_path(self.dump_path).exists())

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

    def test_unsafe_archive_paths_and_links_are_refused(self) -> None:
        for name, link in [("../outside", False), (self.name, True)]:
            with self.subTest(name=name, link=link):
                capture = self.stage(name, link)
                with self.assertRaises(RuntimeError):
                    self.consume(capture)
                self.assertFalse(self.mapped_path(self.dump_path).exists())

    def test_source_attachment_is_copied_faithfully_with_stale_filename_metadata(self) -> None:
        name = "aa/" + "0" * 40
        capture = self.stage(name)
        self.consume(capture)
        self.assertEqual(
            (self.root / "volumes/data/filestore" / self.database / name).read_bytes(),
            self.attachment,
        )

    def test_existing_different_attachment_is_preserved_on_retry(self) -> None:
        capture = self.stage()
        path = self.root / "volumes/data/filestore" / self.database / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original testing evidence")
        with self.assertRaises(RuntimeError):
            self.consume(capture)
        self.assertEqual(path.read_bytes(), b"original testing evidence")


class NativeRecipientPreflightTests(unittest.TestCase):
    def native_generation(self) -> Any:
        return generation().model_copy(
            update={
                "source_mode": "container-export",
                "source_binding_sha256": descriptor().binding_sha256,
            }
        )

    def test_missing_service_adapter_refuses_before_provider_reads_or_writes(self) -> None:
        from control_plane.dokploy.post_deploy import run_compose_post_deploy_update
        from control_plane.dokploy.source import DokployTargetDefinition

        record = self.native_generation()
        target = DokployTargetDefinition(
            context=record.context, instance="testing", target_id=record.target_id
        )
        with (
            patch.object(api, "fetch_dokploy_target_payload") as read,
            patch.object(api, "dokploy_request") as write,
        ):
            with self.assertRaises(click.ClickException):
                run_compose_post_deploy_update(
                    host="https://provider.invalid",
                    token="fixture",
                    target_definition=target,
                    env_file=None,
                    run_destructive_restore=True,
                    fresh_testing_generation=record,
                )
            read.assert_not_called()
            write.assert_not_called()

    def test_local_receiver_refuses_before_provider_mutation(self) -> None:
        from control_plane.dokploy.post_deploy import run_compose_post_deploy_update
        from control_plane.dokploy.source import DokployTargetDefinition

        record = self.native_generation()
        target = DokployTargetDefinition(
            context=record.context, instance="testing", target_id=record.target_id
        )
        for server in (None, "local", ""):
            with (
                self.subTest(server=server),
                patch.object(
                    api, "fetch_dokploy_target_payload", return_value={"serverId": server}
                ),
                patch.object(api, "dokploy_request") as write,
            ):
                with self.assertRaises(click.ClickException):
                    run_compose_post_deploy_update(
                        host="https://provider.invalid",
                        token="fixture",
                        target_definition=target,
                        env_file=None,
                        run_destructive_restore=True,
                        fresh_testing_generation=record,
                        prepare_native_source=lambda *_: {},
                    )
                write.assert_not_called()

    def test_missing_descriptor_refuses_before_workflow_setup(self) -> None:
        from control_plane.workflows.odoo_post_deploy import (
            execute_odoo_post_deploy,
            OdooPostDeployRequest,
        )

        with patch(
            "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config"
        ) as config:
            with self.assertRaises(click.ClickException):
                execute_odoo_post_deploy(
                    control_plane_root=Path("/fixture"),
                    record_store=Mock(),
                    request=OdooPostDeployRequest(
                        context="example", instance="testing", phase="restore"
                    ),
                    run_destructive_restore=True,
                    fresh_testing_generation=self.native_generation(),
                )
            config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
