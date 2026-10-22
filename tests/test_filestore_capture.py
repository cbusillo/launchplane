import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane._odoo_upgrade_rehearsal_program import ODOO_UPGRADE_REHEARSAL_PROGRAM
from control_plane.dokploy.native_copy_stream import NATIVE_ODOO_SOURCE_READ_PROGRAM


def capture_namespace(program: str) -> dict[str, Any]:
    tree = ast.parse(program)
    tree.body.pop()
    namespace: dict[str, Any] = {}
    payload = base64.b64encode(json.dumps({"source": {}}).encode()).decode()
    with patch.object(sys, "argv", ["fixture", payload, "fixture:"]):
        exec(compile(tree, "<capture-fixture>", "exec"), namespace)
    return namespace


class FilestoreCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.rehearsal = capture_namespace(ODOO_UPGRADE_REHEARSAL_PROGRAM)
        self.native = capture_namespace(NATIVE_ODOO_SOURCE_READ_PROGRAM)
        self.frames: list[dict[str, Any]] = []
        self.native["emit"] = self.frames.append

    def capture(self, transport: str, arguments: list[str], *, filestore: bool = True) -> bytes:
        if transport == "rehearsal":
            destination = self.root / "archive"
            destination.unlink(missing_ok=True)
            self.rehearsal["bounded_capture"](
                arguments, destination, 2 * 1024**2, filestore=filestore
            )
            return destination.read_bytes()
        self.frames.clear()
        receipt = self.native["stream"]("filestore" if filestore else "database", arguments)
        data = b"".join(base64.b64decode(frame["data"]) for frame in self.frames)
        self.assertEqual(receipt, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return data

    def test_only_complete_changed_file_warnings_allow_filestore_status_one(self) -> None:
        warning = b"tar: .: file changed as we read it\n"
        for transport in ("rehearsal", "native"):
            for status, errors, filestore, accepted in (
                (0, b"", False, True),
                (1, warning, True, True),
                (1, warning + b"tar: ./ab: file changed as we read it\n", True, True),
                (1, warning, False, False),
                (1, b"", True, False),
                (1, warning.rstrip(b"\n"), True, False),
                (1, warning + b"\n", True, False),
                (
                    1,
                    b"tar: ./missing: Cannot stat: No such file or directory\n" + warning,
                    True,
                    False,
                ),
                (1, warning + b"tar: ./private: Permission denied\n", True, False),
                (1, b"x" * 70000 + warning, True, False),
                (1, warning * 2000, True, False),
                (1, warning + b"x" * 70000, True, False),
                (2, warning, True, False),
                (124, warning, True, False),
                (-15, warning, True, False),
            ):
                with self.subTest(
                    transport=transport, status=status, errors=len(errors), filestore=filestore
                ):
                    program = (
                        'import base64,os,signal,sys; sys.stdout.buffer.write(b"archive"); '
                        f"sys.stdout.flush(); sys.stderr.buffer.write(base64.b64decode({base64.b64encode(errors)!r})); "
                        f"sys.stderr.flush(); os.kill(os.getpid(),signal.SIGTERM) if {status}<0 else sys.exit({status})"
                    )
                    arguments = [sys.executable, "-c", program]
                    if accepted:
                        self.assertEqual(
                            self.capture(transport, arguments, filestore=filestore), b"archive"
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            self.capture(transport, arguments, filestore=filestore)

    @unittest.skipUnless(
        os.environ.get("LAUNCHPLANE_TEST_FILESTORE_IMAGE"),
        "Select an already-local immutable GNU tar/Python image",
    )
    def test_real_directory_mutation_archive_keeps_attachment_validation(self) -> None:
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_VALIDATION_PROGRAM,
        )

        program = """import os,subprocess
from pathlib import Path
root=Path('/fixture/source'); root.mkdir()
content=b'attachment fixture'
import hashlib
checksum=hashlib.sha1(content).hexdigest()
path=root/checksum[:2]/checksum; path.parent.mkdir(); path.write_bytes(content)
(root/'padding').write_bytes(b'x'*1024**2)
os.chdir(root)
result=subprocess.run(['env','LC_ALL=C','tar','-cf','-','--checkpoint=1',
    '--checkpoint-action=exec=test -e /fixture/changed || touch /fixture/changed /fixture/source/late','.'])
raise SystemExit(result.returncode)
"""
        validator: dict[str, Any] = {}
        exec(compile(ATTACHMENT_VALIDATION_PROGRAM, "<attachment-validator>", "exec"), validator)
        content = b"attachment fixture"
        checksum = hashlib.sha1(content).hexdigest()
        name = checksum[:2] + "/" + checksum
        for transport in ("rehearsal", "native"):
            with self.subTest(transport=transport):
                arguments = [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--tmpfs",
                    "/fixture:rw",
                    "--entrypoint",
                    "python3",
                    os.environ["LAUNCHPLANE_TEST_FILESTORE_IMAGE"],
                    "-c",
                    program,
                ]
                processes = []
                popen = subprocess.Popen

                def observe(*args: Any, **kwargs: Any) -> Any:
                    process = popen(*args, **kwargs)
                    processes.append(process)
                    return process

                with patch.object(subprocess, "Popen", side_effect=observe):
                    data = self.capture(transport, arguments)
                self.assertEqual([process.returncode for process in processes], [1])
                archive_path = self.root / "captured.tar"
                archive_path.write_bytes(data)
                extracted = self.root / transport
                with tarfile.open(archive_path) as archive:
                    archive.extractall(extracted, filter="data")
                row = (
                    name,
                    checksum,
                    len(content),
                    "fixture",
                    "fixture.model",
                    1,
                    "application/octet-stream",
                )
                validate = validator["validate_attachment_files"]
                self.assertEqual(validate([row], extracted, None), "verified")
                path = extracted / name
                for replacement, reason in (
                    (b"short", "attachment_size_mismatch"),
                    (b"x" * len(content), "attachment_hash_mismatch"),
                    (None, "attachment_missing"),
                ):
                    if replacement is None:
                        path.unlink()
                    else:
                        path.write_bytes(replacement)
                    self.assertEqual(validate([row], extracted, None), reason)
