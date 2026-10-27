from pathlib import Path
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from control_plane.filestore_permissions import (
    filestore_permission_shell,
    normalize_filestore_permissions,
)


class FilestorePermissionTests(unittest.TestCase):
    def test_generated_shell_normalizes_plain_filestore_through_outer_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            filestore = root / "filestore with 'quotes' and $variables"
            filestore.mkdir()
            attachment = filestore / "attachment"
            attachment.write_bytes(b"plain attachment")
            attachment.chmod(stat.S_IRUSR)
            mountinfo = root / "mountinfo with 'quotes'"
            mountinfo.write_text("1 0 0:1 / / rw - ext4 fixture rw\n")
            (root / "python3").symlink_to(sys.executable)
            owner = f"{os.getuid()}:{os.getgid()}"
            command = (
                '/bin/bash -c \'filestore_database_path="$1"; target_owner="$2"; '
                + filestore_permission_shell(mountinfo)
                + "' _ "
                + shlex.join([str(filestore), owner])
            )
            subprocess.run(
                ["/bin/bash", "-c", command],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = attachment.stat()
            self.assertEqual((metadata.st_uid, metadata.st_gid), (os.getuid(), os.getgid()))
            self.assertTrue(metadata.st_mode & stat.S_IWUSR)
            self.assertTrue(metadata.st_mode & stat.S_IWGRP)
            self.assertEqual(attachment.read_bytes(), b"plain attachment")

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
