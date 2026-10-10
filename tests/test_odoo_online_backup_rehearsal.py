"""Opt-in proof on newly created local Odoo/PostgreSQL fixtures, never live targets."""

import json
import os
import subprocess
import threading
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from control_plane.dokploy.post_deploy import (
    ODOO_BACKUP_GATE_RESULT_MARKER,
    _build_dokploy_odoo_backup_gate_script,
    _build_dokploy_odoo_backup_verification_script,
    extract_odoo_backup_gate_result,
    extract_odoo_backup_verification_result,
)


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_ODOO_BACKUP_IMAGE"), "isolated Odoo image not selected"
)
class OdooOnlineBackupRehearsalTests(unittest.TestCase):
    project: ClassVar[str]
    image: ClassVar[str]
    temporary: ClassVar[TemporaryDirectory[str]]
    data: ClassVar[Path]

    @classmethod
    def docker(cls, *arguments: str, program: str | None = None) -> str:
        return subprocess.run(
            ["docker", *arguments],
            input=program,
            text=True,
            capture_output=True,
            check=True,
            timeout=90,
        ).stdout.strip()

    @classmethod
    def setUpClass(cls) -> None:
        cls.project = "launchplane-online-backup-fixture-" + uuid.uuid4().hex[:10]
        cls.image = os.environ["LAUNCHPLANE_TEST_ODOO_BACKUP_IMAGE"]
        image_environment = json.loads(
            cls.docker("image", "inspect", "--format", "{{json .Config.Env}}", cls.image)
        )
        image_path = next(
            value.removeprefix("PATH=") for value in image_environment if value.startswith("PATH=")
        )
        state = Path("state").resolve()
        state.mkdir(exist_ok=True)
        cls.temporary = TemporaryDirectory(prefix=cls.project, dir=state)
        cls.data = Path(cls.temporary.name)
        cls.data.chmod(0o777)
        cls.addClassCleanup(cls.cleanup_fixture)
        cls.docker("network", "create", "--internal", cls.project)
        cls.docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            cls.project + "-database",
            "--network",
            cls.project,
            "--network-alias",
            "database",
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            "POSTGRES_USER=odoo",
            "-e",
            "POSTGRES_DB=fixture",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "postgres:17",
        )
        for service in ("script-runner", "web"):
            arguments = [
                "run",
                "-d",
                "--pull",
                "never",
                "--name",
                cls.project + "-" + service,
                "--network",
                cls.project,
                "--network-alias",
                service,
                "--label",
                "com.docker.compose.project=" + cls.project,
                "--label",
                "com.docker.compose.service=" + service,
                "-v",
                str(cls.data) + ":/volumes/data",
            ]
            if service == "script-runner":
                arguments += [
                    "-e",
                    "ODOO_DB_HOST=database",
                    "-e",
                    "ODOO_DB_USER=odoo",
                    "-e",
                    "PATH=/tmp/online-bin:" + image_path,
                    "--entrypoint",
                    "/bin/bash",
                    cls.image,
                    "-lc",
                    "tail -f /dev/null",
                ]
            else:
                arguments += [
                    "--entrypoint",
                    "/odoo/odoo-bin",
                    cls.image,
                    *cls.odoo_arguments(),
                    "-i",
                    "base,web",
                ]
            cls.docker(*arguments)
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            try:
                if cls.probe() == 200:
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.25)
        raise AssertionError(
            "Odoo fixture failed to start: "
            + cls.docker("logs", "--tail", "20", cls.project + "-web")
        )

    @classmethod
    def odoo_arguments(cls) -> list[str]:
        return [
            "--config",
            "/dev/null",
            "--database",
            "fixture",
            "--db_host",
            "database",
            "--db_user",
            "odoo",
            "--addons-path",
            "/odoo/addons,/odoo/odoo/addons",
            "--data-dir",
            "/volumes/data",
            "--http-interface",
            "0.0.0.0",
            "--max-cron-threads",
            "0",
            "--without-demo",
            "all",
        ]

    @classmethod
    def cleanup_fixture(cls) -> None:
        for service in ("web", "script-runner", "database"):
            subprocess.run(
                ["docker", "rm", "-f", cls.project + "-" + service], capture_output=True, timeout=30
            )
        subprocess.run(["docker", "network", "rm", cls.project], capture_output=True, timeout=30)
        cls.temporary.cleanup()

    @classmethod
    def probe(cls) -> int:
        return int(
            cls.docker(
                "exec",
                cls.project + "-script-runner",
                "python3",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://web:8069/web/login?db=fixture', timeout=5).status)",
            )
        )

    def shell(self, program: str) -> str:
        return self.docker(
            "exec",
            "-i",
            self.project + "-script-runner",
            "/odoo/odoo-bin",
            "shell",
            *self.odoo_arguments(),
            "--no-http",
            program=program,
        )

    def assert_capture_fails(self, record: str) -> str:
        script = _build_dokploy_odoo_backup_gate_script(
            compose_app_name=self.project,
            backup_nonce="a" * 64,
            database_name="fixture",
            filestore_path="/volumes/data/filestore",
            backup_root="/volumes/data/backups",
            backup_record_id=record,
        )
        completed = subprocess.run(
            ["bash", "-s"], input=script, text=True, capture_output=True, timeout=30
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((self.data / "backups" / "fixture" / record / "manifest.json").exists())
        self.assertNotIn(ODOO_BACKUP_GATE_RESULT_MARKER + "=", completed.stdout)
        return completed.stderr

    def test_restore_cut_with_concurrent_attachment_writes_and_gc(self) -> None:
        old = json.loads(
            self.shell("""
import json
p = env['res.partner'].create({'name': 'before-cut'})
a = env['ir.attachment'].create({'name': 'before-cut', 'raw': b'before-cut bytes', 'res_model': 'res.partner', 'res_id': p.id})
env.cr.commit()
print(json.dumps({'id': a.id, 'fname': a.store_fname}))
""").splitlines()[-1]
        )
        # Hold the REAL pg_dump after the production program releases its fence.
        self.docker(
            "exec",
            "-i",
            self.project + "-script-runner",
            "bash",
            "-s",
            program="""set -eu
mkdir /tmp/online-bin
cat > /tmp/online-bin/pg_dump <<'DUMP'
#!/usr/bin/env bash
set -eu
touch /volumes/data/cut-ready
for ((i=0; i<400; i++)); do
    if [ -f /volumes/data/cut-release ]; then exec /usr/bin/pg_dump "$@"; fi
    sleep 0.05
done
exit 99
DUMP
chmod +x /tmp/online-bin/pg_dump
""",
        )
        samples: list[int | str] = []
        stopped = threading.Event()

        def probe_loop() -> None:
            while not stopped.is_set():
                try:
                    samples.append(self.probe())
                except (subprocess.SubprocessError, ValueError) as error:
                    samples.append(type(error).__name__)
                stopped.wait(0.1)

        thread = threading.Thread(target=probe_loop)
        thread.start()
        script = _build_dokploy_odoo_backup_gate_script(
            compose_app_name=self.project,
            backup_nonce="a" * 64,
            database_name="fixture",
            filestore_path="/volumes/data/filestore",
            backup_root="/volumes/data/backups",
            backup_record_id="concurrent-cut",
        )
        process = subprocess.Popen(
            ["bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(script)
        process.stdin.close()
        try:
            deadline = time.monotonic() + 15
            while not (self.data / "cut-ready").exists():
                self.assertLess(time.monotonic(), deadline, "capture never reached the dump")
                if process.poll() is not None:
                    assert process.stderr is not None
                    self.fail("capture failed before the dump: " + process.stderr.read())
                time.sleep(0.05)
            self.shell(
                """
p = env['res.partner'].create({'name': 'after-cut'})
env['ir.attachment'].create({'name': 'after-cut', 'raw': b'after-cut bytes', 'res_model': 'res.partner', 'res_id': p.id})
env['ir.attachment'].browse(%d).unlink()
env.cr.commit()
env['ir.attachment']._gc_file_store()
"""
                % old["id"]
            )
            self.assertFalse((self.data / "filestore" / "fixture" / old["fname"]).exists())
            (self.data / "cut-release").touch()
            process.wait(timeout=30)
            assert process.stdout is not None and process.stderr is not None
            output, errors = process.stdout.read(), process.stderr.read()
            self.assertEqual(process.returncode, 0, errors)
            evidence = extract_odoo_backup_gate_result(output)
            backup = "/volumes/data/backups/fixture/concurrent-cut"
            verification = _build_dokploy_odoo_backup_verification_script(
                compose_app_name=self.project,
                verification_nonce="b" * 64,
                backup_record_id="concurrent-cut",
                database_name="fixture",
                filestore_path="/volumes/data/filestore",
                backup_dir=backup,
                database_dump_path=backup + "/fixture.dump",
                filestore_archive_path=backup + "/fixture-filestore.tar.gz",
                manifest_path=backup + "/manifest.json",
                capture_evidence={k: str(v) for k, v in evidence.items() if k != "backup_nonce"},
            )
            verified = subprocess.run(
                ["bash", "-s"],
                input=verification,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.assertEqual(
                extract_odoo_backup_verification_result(verified.stdout)["verification_status"],
                "pass",
            )
            self.docker(
                "exec",
                self.project + "-script-runner",
                "bash",
                "-c",
                "createdb -h database -U odoo restored && pg_restore -h database -U odoo -d restored --no-owner --no-acl "
                + backup
                + "/fixture.dump",
            )
            self.docker(
                "exec",
                self.project + "-script-runner",
                "bash",
                "-c",
                "mkdir /volumes/data/restored && tar -xzf "
                + backup
                + "/fixture-filestore.tar.gz -C /volumes/data/restored",
            )
            restored = json.loads(
                self.docker(
                    "exec",
                    "-i",
                    self.project + "-script-runner",
                    "python3",
                    "-",
                    program="""
import hashlib, json
from pathlib import Path
import psycopg2
with psycopg2.connect(host='database', user='odoo', dbname='restored') as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM res_partner WHERE name IN ('before-cut', 'after-cut') ORDER BY name")
        names = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT store_fname, checksum, file_size FROM ir_attachment WHERE store_fname IS NOT NULL")
        rows = cursor.fetchall()
        for fname, checksum, size in rows:
            data = (Path('/volumes/data/restored/fixture') / fname).read_bytes()
            assert len(data) == size and hashlib.sha1(data).hexdigest() == checksum
print(json.dumps({'names': names, 'references': len(rows)}))
""",
                )
            )
            self.assertEqual(restored["names"], ["before-cut"])
            self.assertEqual(restored["references"], evidence["attachment_count"])
            self.assertEqual(
                (self.data / "restored" / "fixture" / old["fname"]).read_bytes(),
                b"before-cut bytes",
            )
            # A persisted reference without its file must refuse before dumping.
            current = json.loads(
                self.shell("""
import json
a = env['ir.attachment'].search([('name', '=', 'after-cut')], limit=1)
print(json.dumps({'fname': a.store_fname}))
""").splitlines()[-1]
            )
            current_file = self.data / "filestore" / "fixture" / current["fname"]
            content = current_file.read_bytes()
            current_file.unlink()
            self.assertIn("FileNotFoundError", self.assert_capture_fails("missing-file"))
            current_file.write_bytes(content)
            current_file.chmod(0o666)

            # Terminate the actual snapshot keeper and let real pg_dump refuse.
            self.docker(
                "exec",
                "-i",
                self.project + "-script-runner",
                "bash",
                "-s",
                program="""set -eu
cat > /tmp/online-bin/pg_dump <<'DUMP'
#!/usr/bin/env bash
set -eu
psql -h database -U odoo -d fixture -tA -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'launchplane-online-backup' AND state = 'idle in transaction'" > /volumes/data/terminated-snapshot
exec /usr/bin/pg_dump "$@"
DUMP
chmod +x /tmp/online-bin/pg_dump
""",
            )
            self.assertIn("pg_dump", self.assert_capture_fails("lost-snapshot"))
            self.assertEqual((self.data / "terminated-snapshot").read_text().strip(), "t")
            self.docker(
                "exec",
                "-i",
                self.project + "-script-runner",
                "bash",
                "-s",
                program="""set -eu
cat > /tmp/online-bin/pg_dump <<'DUMP'
#!/usr/bin/env bash
exit 23
DUMP
chmod +x /tmp/online-bin/pg_dump
""",
            )
            self.assertIn("23", self.assert_capture_fails("failed-dump"))
            archive = (
                self.data / "backups" / "fixture" / "concurrent-cut" / "fixture-filestore.tar.gz"
            )
            archive.write_bytes(b"incomplete snapshot archive")
            failed_verification = subprocess.run(
                ["bash", "-s"],
                input=verification,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.assertEqual(
                extract_odoo_backup_verification_result(failed_verification.stdout)[
                    "verification_status"
                ],
                "fail",
            )
        finally:
            (self.data / "cut-release").touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for handle in (process.stdout, process.stderr):
                if handle is not None:
                    handle.close()
            stopped.set()
            thread.join(timeout=10)
        self.assertGreater(len(samples), 3)
        self.assertTrue(all(status == 200 for status in samples), samples)
        print(
            json.dumps(
                {
                    "web_samples": len(samples),
                    "zero_5xx": True,
                    "restored_references": restored["references"],
                }
            )
        )
