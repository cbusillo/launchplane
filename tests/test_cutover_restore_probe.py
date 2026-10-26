from contextlib import redirect_stdout
import io
import json
import subprocess
from typing import cast
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_restore_probe import RESTORE_PROBE_PROGRAM


class CutoverRestoreProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = dict(
            container="isolated-restore",
            record="original-cutover",
            image="postgres@sha256:" + "a" * 64,
            runner="runner",
            dump="/backup/example.dump",
            database="example",
            database_uuid="original-database",
            database_dump_sha256="b" * 64,
            filestore="/backup/example.tar.gz",
            filestore_sha256="c" * 64,
            timeout_seconds=1800,
        )
        self.commands: list[tuple[list[str], dict[str, object]]] = []
        self.checksum = "d" * 40

    def run_probe(self, *, corrupt_attachment: bool = False) -> dict[str, str]:
        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.commands.append((argv, kwargs))
            output = b""
            status = 0
            if argv[1:3] == ["image", "inspect"]:
                output = b"[{}]"
            elif "sha256sum" in argv:
                field = (
                    "database_dump_sha256"
                    if argv[-1] == self.request["dump"]
                    else "filestore_sha256"
                )
                output = (str(self.request[field]) + "  artifact").encode()
            elif argv[-1].startswith("SELECT store_fname"):
                output = (("dd/" + self.checksum + "|" + self.checksum + "\n") * 10000).encode()
            elif argv[-1].startswith("SELECT value"):
                output = str(self.request["database_uuid"]).encode()
            elif "sha1sum" in argv:
                status = 1 if corrupt_attachment else 0
            return subprocess.CompletedProcess(argv, status, stdout=output)

        source = Mock(stdout=io.BytesIO(b"archive"))
        source.wait.return_value = 0
        source.poll.return_value = 0
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.request))),
            patch("subprocess.run", side_effect=run),
            patch("subprocess.Popen", return_value=source),
            redirect_stdout(output),
        ):
            exec(RESTORE_PROBE_PROGRAM, {})
        return cast(dict[str, str], json.loads(output.getvalue()))

    def test_large_attachment_inventory_uses_one_batch_and_disk_backed_restore(self) -> None:
        result = self.run_probe()
        self.assertEqual(result["restored_database_uuid"], self.request["database_uuid"])
        checks = [(argv, kwargs) for argv, kwargs in self.commands if "sha1sum" in argv]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(cast(bytes, checks[0][1]["input"]).splitlines()), 10000)
        create = next(argv for argv, _ in self.commands if argv[1] == "create")
        mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
        self.assertEqual(
            mounts,
            ["type=volume,src=isolated-restore-data,dst=/var/lib/postgresql/data"],
        )
        restore = next(kwargs for argv, kwargs in self.commands if "pg_restore" in argv)
        self.assertEqual(restore["timeout"], self.request["timeout_seconds"])
        self.assertEqual(self.commands[-1][0], ["docker", "volume", "rm", "isolated-restore-data"])

    def test_corrupt_restored_attachment_refuses_and_cleans_owned_resources(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_probe(corrupt_attachment=True)
        self.assertEqual(self.commands[-1][0], ["docker", "volume", "rm", "isolated-restore-data"])
