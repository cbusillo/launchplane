from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.workflows.odoo_prod_backup_gate import (
    BACKUP_VERIFICATION_SOURCE,
    VERIFIABLE_BACKUP_GATE_SOURCES,
)


class OdooBackupProofStore(Protocol):
    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...


@dataclass(frozen=True)
class OdooBackupRestoreProof:
    backup_record: BackupGateRecord | None
    verification_record: BackupGateRecord | None
    evidence: dict[str, object]
    blockers: tuple[str, ...]


def read_odoo_backup_restore_proof(
    *,
    record_store: OdooBackupProofStore,
    context: str,
    instance: str,
    backup_record_id: str,
    verification_record_id: str,
    expected_paths: dict[str, str],
) -> OdooBackupRestoreProof:
    """Read the same exact backup and integrity proof for restore and replacement."""
    blockers: list[str] = []
    backup_record = _read_backup_record(
        record_store=record_store,
        record_id=backup_record_id,
        sources=VERIFIABLE_BACKUP_GATE_SOURCES,
        context=context,
        instance=instance,
        label="backup gate",
        blockers=blockers,
    )
    if backup_record is not None:
        _validate_backup_gate_paths(
            record=backup_record,
            expected_paths=expected_paths,
            blockers=blockers,
        )
    verification_record = _read_backup_record(
        record_store=record_store,
        record_id=verification_record_id,
        sources=frozenset({BACKUP_VERIFICATION_SOURCE}),
        context=context,
        instance=instance,
        label="backup verification",
        blockers=blockers,
    )
    evidence = _verification_evidence(
        record=verification_record,
        backup_record=backup_record,
        expected_paths=expected_paths,
        blockers=blockers,
    )
    return OdooBackupRestoreProof(backup_record, verification_record, evidence, tuple(blockers))


def odoo_backup_paths(
    *,
    backup_root: str,
    database_name: str,
    backup_record_id: str,
    filestore_path: str,
) -> dict[str, str]:
    if not backup_root or not database_name:
        return {}
    backup_dir = f"{backup_root}/{database_name}/{backup_record_id}"
    return {
        "database_name": database_name,
        "filestore_path": filestore_path,
        "backup_root": backup_root,
        "backup_dir": backup_dir,
        "database_dump_path": f"{backup_dir}/{database_name}.dump",
        "filestore_archive_path": f"{backup_dir}/{database_name}-filestore.tar.gz",
        "manifest_path": f"{backup_dir}/manifest.json",
    }


def _read_backup_record(
    *,
    record_store: OdooBackupProofStore,
    record_id: str,
    sources: frozenset[str],
    context: str,
    instance: str,
    label: str,
    blockers: list[str],
) -> BackupGateRecord | None:
    try:
        record = record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        blockers.append(f"Exact passing Odoo {label} record was not found.")
        return None
    if (
        record.record_id != record_id
        or record.context != context
        or record.instance != instance
        or record.source not in sources
        or not record.required
        or record.status != "pass"
    ):
        blockers.append(f"Odoo {label} record does not match exact production authority.")
    return record


def _validate_backup_gate_paths(
    *, record: BackupGateRecord, expected_paths: dict[str, str], blockers: list[str]
) -> None:
    required_keys = (
        "database_name",
        "filestore_path",
        "backup_root",
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    )
    if any(record.evidence.get(key) != expected_paths.get(key) for key in required_keys):
        blockers.append("Backup-gate paths differ from current DB-backed restore authority.")


def _verification_evidence(
    *,
    record: BackupGateRecord | None,
    backup_record: BackupGateRecord | None,
    expected_paths: dict[str, str],
    blockers: list[str],
) -> dict[str, object]:
    defaults: dict[str, object] = {
        "verification_nonce": "",
        "verification_status": "",
        "manifest_status": "",
        "sha256_status": "",
        "pg_restore_status": "",
        "tar_status": "",
        "staging_space_status": "",
        "database_dump_sha256": "",
        "filestore_archive_sha256": "",
        "database_dump_size": 0,
        "filestore_archive_size": 0,
        "pg_restore_entry_count": 0,
        "filestore_member_count": 0,
        "filestore_unpacked_size": 0,
        "data_volume_free_bytes": 0,
        "staging_required_bytes": 0,
    }
    if record is None:
        return defaults
    evidence = record.evidence
    if backup_record is None or evidence.get("backup_record_id") != backup_record.record_id:
        blockers.append("Backup verification record does not bind the exact backup gate.")
    if (
        backup_record is not None
        and evidence.get("backup_record_created_at") != backup_record.created_at
    ):
        blockers.append("Backup verification record does not bind backup creation evidence.")
    required_path_keys = (
        "database_name",
        "filestore_path",
        "backup_root",
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    )
    if any(evidence.get(key) != expected_paths.get(key) for key in required_path_keys):
        blockers.append("Backup verification paths differ from DB-backed restore authority.")
    if evidence.get("verification_status") != "pass" or evidence.get("failure_code"):
        blockers.append("Backup verification evidence is not passing.")
    verification_nonce = evidence.get("verification_nonce", "")
    if not isinstance(verification_nonce, str) or not re.fullmatch(
        r"[0-9a-f]{64}", verification_nonce
    ):
        blockers.append("Backup verification evidence does not include its request nonce.")
        verification_nonce = ""
    for status_key in (
        "manifest_status",
        "sha256_status",
        "pg_restore_status",
        "tar_status",
        "staging_space_status",
    ):
        if evidence.get(status_key) != "pass":
            blockers.append(f"Backup verification {status_key} is not passing.")
    database_digest = evidence.get("database_dump_sha256", "")
    filestore_digest = evidence.get("filestore_archive_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", database_digest):
        blockers.append("Backup verification database dump hash is invalid.")
    if not re.fullmatch(r"[0-9a-f]{64}", filestore_digest):
        blockers.append("Backup verification filestore archive hash is invalid.")
    parsed: dict[str, object] = {
        "verification_nonce": verification_nonce,
        "verification_status": str(evidence.get("verification_status", "")),
        "manifest_status": str(evidence.get("manifest_status", "")),
        "sha256_status": str(evidence.get("sha256_status", "")),
        "pg_restore_status": str(evidence.get("pg_restore_status", "")),
        "tar_status": str(evidence.get("tar_status", "")),
        "staging_space_status": str(evidence.get("staging_space_status", "")),
        "database_dump_sha256": database_digest,
        "filestore_archive_sha256": filestore_digest,
    }
    for key in (
        "database_dump_size",
        "filestore_archive_size",
        "pg_restore_entry_count",
        "filestore_member_count",
        "filestore_unpacked_size",
        "data_volume_free_bytes",
        "staging_required_bytes",
    ):
        try:
            value = int(evidence.get(key, "0"))
        except ValueError:
            value = 0
        if value < 1:
            blockers.append(f"Backup verification {key} must be positive.")
        parsed[key] = value
    data_volume_free_bytes = parsed["data_volume_free_bytes"]
    staging_required_bytes = parsed["staging_required_bytes"]
    if (
        not isinstance(data_volume_free_bytes, int)
        or not isinstance(staging_required_bytes, int)
        or data_volume_free_bytes < staging_required_bytes
    ):
        blockers.append("Backup verification staging-space evidence is insufficient.")
    return parsed
