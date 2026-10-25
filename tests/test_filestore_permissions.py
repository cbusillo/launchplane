from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from control_plane.filestore_permissions import normalize_filestore_permissions


class FilestorePermissionTests(unittest.TestCase):
    def test_overlay_maintenance_does_not_change_shared_lower_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            filestore = root / "filestore"
            filestore.mkdir()
            attachment = filestore / "attachment"
            attachment.write_bytes(b"shared lower attachment")
            mountinfo = root / "mountinfo"
            mountinfo.write_text(f"1 0 0:1 / {filestore} rw - overlay overlay rw\n")
            before = attachment.stat()
            owner = f"{filestore.stat().st_uid}:{filestore.stat().st_gid}"
            with patch("subprocess.run") as command:
                normalize_filestore_permissions(filestore, owner, mountinfo)
            command.assert_not_called()
            self.assertEqual(attachment.stat(), before)
            with self.assertRaises(RuntimeError):
                normalize_filestore_permissions(filestore, owner + "0", mountinfo)

    def test_plain_filestore_retains_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(f"1 0 0:1 / {root} rw - ext4 fixture rw\n")
            attachment = root / "attachment"
            attachment.write_bytes(b"plain attachment")
            attachment.chmod(stat.S_IRUSR)
            owner = f"{root.stat().st_uid}:{root.stat().st_gid}"
            normalize_filestore_permissions(root, owner, mountinfo)
            self.assertTrue(attachment.stat().st_mode & stat.S_IWUSR)
            self.assertTrue(attachment.stat().st_mode & stat.S_IWGRP)
