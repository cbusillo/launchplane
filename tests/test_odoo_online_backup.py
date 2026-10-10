import base64
import errno
import hashlib
import io
import json
import os
import subprocess
import tarfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from control_plane.contracts.odoo_online_backup import OdooProdBackupCaptureEvidence
from control_plane.dokploy.online_backup import ONLINE_ODOO_BACKUP_PROGRAM
from control_plane.dokploy.post_deploy import _build_dokploy_odoo_backup_verification_script


class OnlineBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.filestore = self.root / "filestore" / "example"
        self.filestore.mkdir(parents=True)
        self.destination = self.root / "backup"
        self.destination.mkdir()
        self.content = b"committed attachment before the cut"
        self.checksum = hashlib.sha1(self.content).hexdigest()
        self.fname = self.checksum[:2] + "/" + self.checksum
        self.source = self.filestore / self.fname
        self.source.parent.mkdir()
        self.source.write_bytes(self.content)
        self.fence = MagicMock()
        self.snapshot = MagicMock()
        self.fence.__enter__.return_value = self.fence
        self.snapshot.__enter__.return_value = self.snapshot
        self.reader = self.snapshot.cursor.return_value.__enter__.return_value
        from datetime import datetime, timezone

        self.reader.fetchone.return_value = ("00000001-00000001-1", datetime.now(timezone.utc))
        self.reader.__iter__.side_effect = lambda: iter(
            [(self.fname, self.checksum, len(self.content))]
        )
        self.psycopg = SimpleNamespace(connect=MagicMock(side_effect=[self.fence, self.snapshot]))
        self.environment = {
            "DATABASE_NAME": "example",
            "FILESTORE_ROOT": str(self.filestore.parent),
            "BACKUP_DIR": str(self.destination),
            "BACKUP_RECORD_ID": "backup-example",
            "BACKUP_NONCE": "c" * 64,
            "IMAGE_ID": "sha256:" + "d" * 64,
            "RESULT_MARKER": "RESULT",
        }
        self.output = io.StringIO()

    def dump(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "pg_dump":
            self.fence.commit.assert_called_once()
            self.assertEqual(command[command.index("--snapshot") + 1], self.reader.fetchone()[0])
            Path(command[command.index("--file") + 1]).write_bytes(b"snapshot database dump")
        return subprocess.CompletedProcess(command, 0, stdout="1; TABLE ir_attachment\n")

    def capture(self, dump: object = None) -> dict[str, object]:
        with (
            patch.dict("sys.modules", {"psycopg2": self.psycopg}),
            patch.dict(os.environ, self.environment),
            patch("subprocess.run", side_effect=dump or self.dump),
            redirect_stdout(self.output),
        ):
            exec(ONLINE_ODOO_BACKUP_PROGRAM, {})
        payload = json.loads(base64.b64decode(self.output.getvalue().split("=", 1)[1]))
        OdooProdBackupCaptureEvidence.model_validate(payload)
        return cast(dict[str, object], payload)

    def verify(self, expected: dict[str, str] | None = None) -> dict[str, object]:
        script = _build_dokploy_odoo_backup_verification_script(
            compose_app_name="example",
            verification_nonce="e" * 64,
            backup_record_id="backup-example",
            database_name="example",
            filestore_path=str(self.filestore.parent),
            backup_dir=str(self.destination),
            database_dump_path=str(self.destination / "example.dump"),
            filestore_archive_path=str(self.destination / "example-filestore.tar.gz"),
            manifest_path=str(self.destination / "manifest.json"),
        )
        program = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "VERIFICATION_NONCE": "e" * 64,
                    "BACKUP_RECORD_ID": "backup-example",
                    "DATABASE_NAME": "example",
                    "FILESTORE_ROOT": str(self.filestore.parent),
                    "BACKUP_DIR": str(self.destination),
                    "DATABASE_DUMP_PATH": str(self.destination / "example.dump"),
                    "FILESTORE_ARCHIVE_PATH": str(self.destination / "example-filestore.tar.gz"),
                    "MANIFEST_PATH": str(self.destination / "manifest.json"),
                    "RESULT_MARKER": "VERIFY",
                    "CAPTURE_EVIDENCE": json.dumps(expected or {}),
                },
            ),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="1; TABLE\n"),
            ),
            redirect_stdout(output),
        ):
            exec(program, {})
        return cast(
            dict[str, object], json.loads(base64.b64decode(output.getvalue().split("=", 1)[1]))
        )

    def test_gc_after_cut_cannot_remove_pinned_attachment(self) -> None:
        # GC runs as soon as the attachment fence commits; the exported snapshot
        # and pinned files still contain the pre-delete reference.
        self.fence.commit.side_effect = self.source.unlink
        evidence = self.capture()
        self.assertFalse(self.source.exists())
        with tarfile.open(self.destination / "example-filestore.tar.gz") as archive:
            handle = archive.extractfile("example/" + self.fname)
            assert handle is not None
            self.assertEqual(handle.read(), self.content)
        self.assertEqual(evidence["attachment_file_count"], 1)
        self.assertFalse(list(self.destination.glob(".filestore-snapshot-*")))
        self.assertEqual(self.verify()["verification_status"], "pass")

    def test_failed_dump_never_publishes_success(self) -> None:
        def fail(command: list[str], **_kwargs: object) -> None:
            self.dump(command)
            raise subprocess.CalledProcessError(23, command)

        with self.assertRaises(subprocess.CalledProcessError):
            self.capture(fail)
        self.assertFalse((self.destination / "manifest.json").exists())
        self.assertEqual(self.output.getvalue(), "")
        self.assertFalse(list(self.destination.glob(".filestore-snapshot-*")))

    def test_missing_or_corrupt_committed_attachment_blocks_capture(self) -> None:
        self.source.write_bytes(b"wrong bytes")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            self.capture()
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_missing_attachment_blocks_before_dump(self) -> None:
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.capture()
        self.fence.commit.assert_not_called()
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_cross_filesystem_pinning_fails_without_incoherent_copy_fallback(self) -> None:
        with patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device link")):
            with self.assertRaises(OSError):
                self.capture()
        self.fence.commit.assert_not_called()
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_incomplete_archive_never_publishes_success(self) -> None:
        with patch("tarfile.open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertFalse((self.destination / "manifest.json").exists())
        self.assertEqual(self.output.getvalue(), "")

    def test_matching_artifact_hash_does_not_hide_missing_referenced_file(self) -> None:
        self.capture()
        archive_path = self.destination / "example-filestore.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(self.filestore, arcname="example", recursive=False)
        manifest_path = self.destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["filestore_archive_sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        manifest["filestore_archive_size"] = archive_path.stat().st_size
        manifest_path.write_text(json.dumps(manifest))
        result = self.verify()
        self.assertEqual(result["verification_status"], "fail")
        self.assertEqual(result["tar_status"], "fail")

    def test_capture_binding_rejects_manifest_downgrade(self) -> None:
        evidence = OdooProdBackupCaptureEvidence.model_validate(self.capture())
        manifest_path = self.destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = 1
        manifest_path.write_text(json.dumps(manifest))
        result = self.verify({"schema_version": str(evidence.schema_version)})
        self.assertEqual(result["verification_status"], "fail")
        self.assertEqual(result["manifest_status"], "fail")

    def test_capture_contract_rejects_missing_snapshot_or_image(self) -> None:
        evidence = self.capture()
        for field in ("postgres_snapshot_id", "image_id", "recovery_point_at"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                OdooProdBackupCaptureEvidence.model_validate(
                    {k: v for k, v in evidence.items() if k != field}
                )
