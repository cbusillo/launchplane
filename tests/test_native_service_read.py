import base64
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.dokploy.native_service_read import read_stored_docker_service


class StoredDockerServiceReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.configuration = self.root / "reply.json"
        self.pid = self.root / "pid"
        script = self.root / "docker"
        script.write_text(
            "#!" + sys.executable + "\n"
            "import base64,json,os,select,sys,time\n"
            "from pathlib import Path\n"
            "if sys.argv[1:]!=['system','dial-stdio']: sys.exit(2)\n"
            "configuration=json.loads(Path(" + repr(str(self.configuration)) + ").read_text())\n"
            "Path(" + repr(str(self.pid)) + ").write_text(str(os.getpid()))\n"
            "while sys.stdin.buffer.readline() not in (b'\\r\\n',b''): pass\n"
            "if select.select([sys.stdin],[],[],0)[0] and not os.read(0,1): sys.exit(3)\n"
            "time.sleep(configuration.get('stall',0))\n"
            "sys.stderr.write('fixture-private-value')\n"
            "sys.stdout.buffer.write(base64.b64decode(configuration['reply']))\n"
            "sys.stdout.buffer.flush()\n"
            "sys.exit(configuration.get('exit',0))\n"
        )
        script.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.root)}))
        self.payload = {
            "ID": "retained-service",
            "Spec": {"Env": ["PRIVATE=fixture-private-value"]},
        }
        self.body = json.dumps(self.payload).encode()

    def response(self, body: bytes, *headers: bytes, status: int = 200) -> bytes:
        return (
            b"HTTP/1.1 "
            + str(status).encode()
            + b" Fixture\r\nContent-Type: application/json\r\n"
            + b"\r\n".join(headers)
            + b"\r\n\r\n"
            + body
        )

    def read(self, reply: bytes, *, stall: float = 0, exit_code: int = 0) -> dict[str, object]:
        self.configuration.write_text(
            json.dumps(
                {
                    "reply": base64.b64encode(reply).decode(),
                    "stall": stall,
                    "exit": exit_code,
                }
            )
        )
        return read_stored_docker_service(
            "retained-service", "1.47", max_bytes=1024, timeout_seconds=1
        )

    def test_complete_fixed_read_keeps_input_open_and_accepts_chunked_json(self) -> None:
        length = b"Content-Length: " + str(len(self.body)).encode()
        self.assertEqual(self.read(self.response(self.body, length)), self.payload)
        chunked = hex(len(self.body))[2:].encode() + b"\r\n" + self.body + b"\r\n0\r\n\r\n"
        self.assertEqual(
            self.read(self.response(chunked, b"Transfer-Encoding: chunked")), self.payload
        )

    def test_incomplete_ambiguous_and_oversized_replies_never_return_private_inputs(self) -> None:
        length = b"Content-Length: " + str(len(self.body)).encode()
        self.assertEqual(self.read(self.response(self.body, length)), self.payload)
        partial = b'{"private":"fixture-private-value"'
        replies = [
            self.response(self.body, b"Content-Length: " + str(len(self.body) + 1).encode()),
            self.response(self.body + b"extra", length),
            self.response(self.body, length, length),
            self.response(self.body, length, b"Transfer-Encoding: chunked"),
            self.response(self.body, length, b"Content-Encoding: gzip"),
            self.response(self.body),
            self.response(self.body, b"Content-Length: 1025"),
            self.response(
                b"401\r\n" + b"x" * 1025 + b"\r\n0\r\n\r\n", b"Transfer-Encoding: chunked"
            ),
            self.response(b"1\r\n{\r\n", b"Transfer-Encoding: chunked"),
            self.response(partial, b"Content-Length: " + str(len(partial)).encode()),
        ]
        for reply in replies:
            with self.subTest(reply_size=len(reply)), self.assertRaises(RuntimeError) as caught:
                self.read(reply)
            self.assertNotIn("fixture-private-value", str(caught.exception))

    def test_invalid_json_status_and_child_exit_are_unavailable(self) -> None:
        for body in (b'{"ID":1,"ID":2}', b'{"ID":NaN}', b"[]"):
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                self.read(self.response(body, b"Content-Length: " + str(len(body)).encode()))
        for status in (401, 403, 404, 500):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.read(
                    self.response(
                        self.body, b"Content-Length: " + str(len(self.body)).encode(), status=status
                    )
                )
        with self.assertRaises(RuntimeError):
            self.read(
                self.response(self.body, b"Content-Length: " + str(len(self.body)).encode()),
                exit_code=1,
            )
        denied = b'{"message":"authorization denied by plugin: fixture-private-value"}'
        with self.assertRaises(PermissionError) as caught:
            self.read(
                self.response(denied, b"Content-Length: " + str(len(denied)).encode(), status=403)
            )
        self.assertNotIn("fixture-private-value", str(caught.exception))

    def test_stalled_child_is_reaped_without_returning_a_body(self) -> None:
        with self.assertRaises(RuntimeError):
            self.read(
                self.response(self.body, b"Content-Length: " + str(len(self.body)).encode()),
                stall=60,
            )
        pid = int(self.pid.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_coordinates_never_start_the_child(self) -> None:
        for service, version in (
            ("../other", "1.47"),
            ("retained-service", "1.47\r\nInjected: yes"),
        ):
            with self.subTest(service=service, version=version), self.assertRaises(ValueError):
                read_stored_docker_service(service, version, max_bytes=1024)
        self.assertFalse(self.pid.exists())
