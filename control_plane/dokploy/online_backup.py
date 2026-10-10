"""Registry-free program shipped to the Odoo script runner for online backups."""

ONLINE_ODOO_BACKUP_PROGRAM = r"""
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

import psycopg2


def digest_file(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_file(path):
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def capture():
    database_name = os.environ["DATABASE_NAME"]
    backup_dir = Path(os.environ["BACKUP_DIR"])
    root = Path(os.environ["FILESTORE_ROOT"])
    if root.name != database_name:
        root = root / database_name
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise RuntimeError("Filestore must be an existing directory without symlinks")
    if backup_dir.resolve() != backup_dir.absolute() or any(backup_dir.iterdir()):
        raise RuntimeError("Backup destination must be a new empty directory without symlinks")
    image_id = os.environ["IMAGE_ID"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Backup requires an immutable running image identity")

    connection_values = dict(
        dbname=database_name,
        host=os.environ.get("ODOO_DB_HOST", "database"),
        port=os.environ.get("ODOO_DB_PORT", "5432"),
        user=os.environ.get("ODOO_DB_USER", "odoo"),
        password=os.environ.get("ODOO_DB_PASSWORD", ""),
        connect_timeout=10,
        application_name="launchplane-online-backup",
    )
    dump_path = backup_dir / (database_name + ".dump")
    archive_path = backup_dir / (database_name + "-filestore.tar.gz")
    inventory_path = backup_dir / "attachments.json"
    manifest_path = backup_dir / "manifest.json"
    pin_parent = Path(tempfile.mkdtemp(prefix=".filestore-snapshot-", dir=backup_dir))
    pin_root = pin_parent / database_name
    pin_root.mkdir()
    references = {}
    attachment_count = 0
    try:
        # A separate lock transaction lets attachment writers and Odoo's SHARE-mode
        # GC resume while the exported snapshot remains alive for pg_dump.
        with psycopg2.connect(**connection_values) as fence:
            with fence.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                cursor.execute("LOCK TABLE ir_attachment IN SHARE ROW EXCLUSIVE MODE")
                with psycopg2.connect(**connection_values) as snapshot:
                    snapshot.set_session(isolation_level="REPEATABLE READ", readonly=True)
                    with snapshot.cursor() as reader:
                        reader.execute("SELECT pg_export_snapshot(), clock_timestamp()")
                        snapshot_id, recovery_point = reader.fetchone()
                        reader.execute(
                            "SELECT store_fname, checksum, file_size FROM ir_attachment "
                            "WHERE store_fname IS NOT NULL ORDER BY store_fname"
                        )
                        for fname, checksum, file_size in reader:
                            attachment_count += 1
                            if (
                                not isinstance(checksum, str)
                                or not re.fullmatch(r"[0-9a-f]{40}", checksum)
                                or fname != checksum[:2] + "/" + checksum
                                or not isinstance(file_size, int)
                                or file_size < 0
                            ):
                                raise RuntimeError("Unsupported attachment storage reference")
                            reference = dict(checksum=checksum, file_size=file_size)
                            if fname in references:
                                if references[fname] != reference:
                                    raise RuntimeError("Conflicting attachment references")
                                continue
                            source = root / fname
                            if source.parent.is_symlink() or not stat.S_ISREG(source.lstat().st_mode):
                                raise RuntimeError("Attachment must be a regular local file")
                            destination = pin_root / fname
                            destination.parent.mkdir(exist_ok=True)
                            # Content-addressed Odoo files are immutable. Hard links retain
                            # them through delete/GC without copying bytes under the fence.
                            os.link(source, destination, follow_symlinks=False)
                            references[fname] = reference
                    fence.commit()
                    for fname, reference in references.items():
                        pinned = pin_root / fname
                        if (
                            pinned.stat().st_size != reference["file_size"]
                            or digest_file(pinned, "sha1") != reference["checksum"]
                        ):
                            raise RuntimeError("Attachment integrity failed")
                    dump_environment = dict(os.environ, PGPASSWORD=connection_values["password"])
                    subprocess.run(
                        [
                            "pg_dump", "--host", connection_values["host"],
                            "--port", connection_values["port"],
                            "--username", connection_values["user"],
                            "--format", "custom", "--snapshot", snapshot_id,
                            "--lock-wait-timeout=10s", "--file", str(dump_path), database_name,
                        ],
                        env=dump_environment, check=True, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        if dump_path.stat().st_size == 0:
            raise RuntimeError("Empty database dump")
        subprocess.run(
            ["pg_restore", "--list", str(dump_path)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["pg_restore", "--no-owner", "--no-acl", "--file=/dev/null", str(dump_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with tarfile.open(archive_path, "w:gz", dereference=True) as archive:
            archive.add(pin_root, arcname=database_name)
        # Read back archived bytes as well as the pins before publishing success.
        seen = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                fname = member.name.removeprefix(database_name + "/")
                reference = references[fname]
                handle = archive.extractfile(member)
                digest = hashlib.sha1()
                with handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if member.size != reference["file_size"] or digest.hexdigest() != reference["checksum"]:
                    raise RuntimeError("Archived attachment integrity failed")
                seen.add(fname)
        if seen != references.keys():
            raise RuntimeError("Incomplete filestore archive")
        inventory_path.write_text(json.dumps(references, sort_keys=True), encoding="utf-8")
        evidence = dict(
            schema_version=2,
            backup_record_id=os.environ["BACKUP_RECORD_ID"],
            database_name=database_name,
            database_dump_sha256=digest_file(dump_path),
            filestore_archive_sha256=digest_file(archive_path),
            database_dump_size=dump_path.stat().st_size,
            filestore_archive_size=archive_path.stat().st_size,
            consistency_protocol="postgres-exported-snapshot-odoo-hardlinks-v1",
            postgres_snapshot_id=snapshot_id,
            recovery_point_at=recovery_point.isoformat(),
            image_id=image_id,
            attachment_count=attachment_count,
            attachment_file_count=len(references),
            attachment_inventory_sha256=digest_file(inventory_path),
        )
        manifest = dict(
            evidence, backup_dir=str(backup_dir), database_dump_path=str(dump_path),
            filestore_archive_path=str(archive_path), manifest_path=str(manifest_path),
            captured_at=recovery_point.isoformat(),
        )
        for artifact in (dump_path, archive_path, inventory_path):
            sync_file(artifact)
        temporary_manifest = backup_dir / ".manifest.json"
        temporary_manifest.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        sync_file(temporary_manifest)
        os.replace(temporary_manifest, manifest_path)
        result = dict(evidence, backup_nonce=os.environ["BACKUP_NONCE"])
        encoded = base64.b64encode(json.dumps(result, sort_keys=True).encode()).decode("ascii")
        print(os.environ["RESULT_MARKER"] + "=" + encoded, flush=True)
    finally:
        shutil.rmtree(pin_parent)


capture()
"""
