from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import os
import re
import subprocess
import time
from typing import Callable, Iterator

from control_plane.contracts.production_backup_authority import (
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
    production_backup_snapshot_prefix_valid,
)
from control_plane.contracts.production_backup_gate import (
    ProductionBackupGateWorkerRequest,
    ProductionBackupGateWorkerResult,
)
from control_plane.workflows.ship import utc_now_timestamp


class ProductionBackupProviderError(ValueError):
    """A bounded provider failure code, without SSH output or secret material."""


@contextmanager
def ssh_memory_files(private_key: str, known_hosts: str) -> Iterator[tuple[str, str]]:
    """Provide SSH's seekable files in anonymous RAM, never filesystem storage."""
    create_memory_file = getattr(os, "memfd_create", None)
    if create_memory_file is None:
        raise ProductionBackupProviderError("backup_ssh_memory_files_unavailable")
    descriptors: list[int] = []
    try:
        for name, value in (("backup-identity", private_key), ("backup-hosts", known_hosts)):
            descriptor = create_memory_file(name, flags=getattr(os, "MFD_CLOEXEC", 1))
            descriptors.append(descriptor)
            os.fchmod(descriptor, 0o600)
            content = (value.rstrip() + "\n").encode("utf-8")
            if os.write(descriptor, content) != len(content):
                raise ProductionBackupProviderError("backup_ssh_memory_write_failed")
            os.lseek(descriptor, 0, os.SEEK_SET)
        # ssh closes inherited descriptors; it reads our still-open memfds via procfs.
        descriptor_path = f"/proc/{os.getpid()}/fd"
        yield f"{descriptor_path}/{descriptors[0]}", f"{descriptor_path}/{descriptors[1]}"
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _snapshot_names(output: str, prefix: str) -> list[str]:
    pattern = re.compile(rf"(?:^|\s)({re.escape(prefix)}-\d{{8}}-\d{{6}}-[a-f0-9]{{6}})(?=\s|$)")
    return sorted(set(pattern.findall(output)))


def _verify_backup_volume(output: str, *, storage: str, archive: str) -> None:
    matches = {
        fields[0]
        for line in output.splitlines()
        if (fields := line.split()) and fields[0] == f"{storage}:backup/{archive}"
    }
    if len(matches) != 1:
        raise ProductionBackupProviderError("independent_backup_not_found")


def _failure_code(error: Exception, stage: str) -> str:
    if isinstance(error, ProductionBackupProviderError):
        return str(error)
    if isinstance(error, subprocess.TimeoutExpired):
        return "backup_timeout"
    return f"{stage}_unavailable"


def execute_production_backup_provider(
    binding: ProductionBackupGateWorkerRequest,
    *,
    ssh_private_key: str,
    ssh_known_hosts: str,
    checkpoint: Callable[[str], None] | None = None,
    record_progress: Callable[[dict[str, str]], None] | None = None,
) -> ProductionBackupGateWorkerResult:
    """Capture both policy operations through one exact forced-command boundary."""
    started_at = utc_now_timestamp()
    policy = binding.policy
    source = binding.source_target.destination
    destination = binding.destination_target.destination
    assert isinstance(source, ProxmoxGuestBackupDestinationReference)
    assert isinstance(destination, ProxmoxStorageBackupDestinationReference)
    evidence = {
        "provider": "proxmox",
        "product": policy.product,
        "context": policy.context,
        "instance": policy.instance,
        "promotion_action": policy.promotion_action,
        "policy_record_id": policy.record_id,
        "policy_revision": str(policy.policy_revision),
        "policy_digest": policy.policy_digest,
        "source_target_record_id": binding.source_target.record_id,
        "source_target_digest": binding.source_target.target_digest,
        "destination_target_record_id": binding.destination_target.record_id,
        "destination_target_digest": binding.destination_target.target_digest,
    }
    stage = "preflight"
    deadline = time.monotonic() + binding.request.timeout_seconds

    def save_progress() -> None:
        evidence["provider_stage"] = stage
        if record_progress is not None:
            try:
                record_progress(dict(evidence))
            except Exception as progress_error:
                raise ProductionBackupProviderError(
                    "backup_progress_unavailable"
                ) from progress_error

    try:
        save_progress()
        if not ssh_private_key.strip() or not ssh_known_hosts.strip():
            raise ProductionBackupProviderError("backup_ssh_material_missing")
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:%_-]*", source.host) is None
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", source.username) is None
            or re.fullmatch(r"[0-9]+", source.guest_id) is None
        ):
            raise ProductionBackupProviderError("backup_endpoint_invalid")
        prefix = policy.fast_snapshot.snapshot_prefix
        if not production_backup_snapshot_prefix_valid(prefix):
            raise ProductionBackupProviderError("snapshot_prefix_invalid")
        suffix = hashlib.sha256(binding.request.backup_record_id.encode()).hexdigest()[:6]
        snapshot = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{suffix}"
        if len(snapshot) > 40:
            raise ProductionBackupProviderError("snapshot_name_too_long")
        guest_command = "pct" if source.guest_kind == "lxc" else "qm"
        archive_kind = "ct" if source.guest_kind == "lxc" else "vm"
        storage = destination.storage_id

        with ssh_memory_files(ssh_private_key, ssh_known_hosts) as (
            identity_file,
            known_hosts_file,
        ):
            ssh_command = [
                "ssh",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts_file}",
                "-i",
                identity_file,
                "--",
                f"{source.username}@{source.host}",
            ]

            def run(command: list[str], *, include_stderr: bool = False) -> str:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProductionBackupProviderError("backup_timeout")
                result = subprocess.run(
                    [*ssh_command, *command],
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                )
                if result.returncode != 0:
                    raise ProductionBackupProviderError(f"{stage}_command_failed")
                return f"{result.stdout}\n{result.stderr}" if include_stderr else result.stdout

            try:
                boundary = json.loads(run(["launchplane-backup-boundary"]))
            except json.JSONDecodeError as error:
                raise ProductionBackupProviderError("backup_boundary_invalid") from error
            if boundary != {
                "schema_version": 1,
                "guest_kind": source.guest_kind,
                "guest_id": source.guest_id,
                "storage_id": storage,
                "snapshot_prefix": prefix,
                "restore_allowed": False,
            }:
                raise ProductionBackupProviderError("backup_host_binding_mismatch")
            storage_rows = [
                fields
                for line in run(["pvesm", "status", "--storage", storage]).splitlines()
                if (fields := line.split()) and fields[0] == storage
            ]
            if len(storage_rows) != 1 or storage_rows[0][1:3] != ["pbs", "active"]:
                raise ProductionBackupProviderError("backup_storage_not_active_pbs")

            stage = "snapshot"
            evidence["requested_snapshot_name"] = snapshot
            evidence["snapshot_started_at"] = utc_now_timestamp()
            save_progress()
            if checkpoint is not None:
                checkpoint(stage)
            run([guest_command, "snapshot", source.guest_id, snapshot])
            evidence["snapshot_name"] = snapshot
            if snapshot not in _snapshot_names(
                run([guest_command, "listsnapshot", source.guest_id]), prefix
            ):
                raise ProductionBackupProviderError("snapshot_not_found")
            evidence["snapshot_finished_at"] = utc_now_timestamp()
            save_progress()

            stage = "independent_backup"
            evidence["independent_backup_started_at"] = utc_now_timestamp()
            save_progress()
            if checkpoint is not None:
                checkpoint(stage)
            backup_output = run(
                ["vzdump", source.guest_id, "--mode", "snapshot", "--storage", storage],
                include_stderr=True,
            )
            archives = set(
                re.findall(
                    rf"'{archive_kind}/{re.escape(source.guest_id)}/[0-9TZ:-]+'", backup_output
                )
            )
            if len(archives) != 1:
                raise ProductionBackupProviderError("independent_backup_identity_missing")
            archive = archives.pop().strip("'")
            evidence["independent_backup_id"] = archive
            save_progress()
            _verify_backup_volume(
                run(["pvesm", "list", storage, "--vmid", source.guest_id, "--content", "backup"]),
                storage=storage,
                archive=archive,
            )
            evidence["independent_backup_finished_at"] = utc_now_timestamp()
            evidence["capture_status"] = "verified"
            save_progress()

            stage = "snapshot_retention"
            try:
                snapshots = _snapshot_names(
                    run([guest_command, "listsnapshot", source.guest_id]), prefix
                )
                delete_count = max(len(snapshots) - max(1, policy.fast_snapshot.retention_count), 0)
                for name in [name for name in snapshots if name != snapshot][:delete_count]:
                    save_progress()
                    if checkpoint is not None:
                        checkpoint(stage)
                    run([guest_command, "delsnapshot", source.guest_id, name])
                evidence["retention_status"] = "pass"
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                evidence["retention_status"] = "fail"
                evidence["retention_error_code"] = _failure_code(error, stage)
            if checkpoint is not None:
                checkpoint("backup_complete")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return ProductionBackupGateWorkerResult(
            status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            evidence=evidence,
            error_code=_failure_code(error, stage),
        )
    return ProductionBackupGateWorkerResult(
        status="pass",
        started_at=started_at,
        finished_at=utc_now_timestamp(),
        evidence=evidence,
    )
