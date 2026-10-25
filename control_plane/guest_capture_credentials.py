from __future__ import annotations

from control_plane import secrets
from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
)
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.storage.postgres import PostgresRecordStore


SSH_PRIVATE_KEY = "PRODUCTION_BACKUP_SSH_PRIVATE_KEY"
SSH_KNOWN_HOSTS = "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"
CAPTURE_KEYS = (SSH_PRIVATE_KEY, SSH_KNOWN_HOSTS)


def capture_boundary(
    policy: ProductionBackupPolicyRecord,
    source: ProductionBackupTargetRecord,
    destination: ProductionBackupTargetRecord,
) -> tuple[str, str, str, str, str, str]:
    guest, storage = source.destination, destination.destination
    if not isinstance(guest, ProxmoxGuestBackupDestinationReference) or not isinstance(
        storage, ProxmoxStorageBackupDestinationReference
    ):
        raise ValueError("Capture requires guest and storage authority.")
    if (guest.host.lower(), guest.username) != (storage.host.lower(), storage.username):
        raise ValueError("Capture targets must use the same endpoint.")
    return (
        guest.host.lower(),
        guest.username,
        guest.guest_kind,
        guest.guest_id,
        storage.storage_id,
        policy.fast_snapshot.snapshot_prefix,
    )


def guest_capture_metadata(
    store: PostgresRecordStore,
    binding: ProductionBackupGateWorkerRequest,
) -> tuple[tuple[SecretBinding, SecretRecord], ...]:
    boundary = capture_boundary(binding.policy, binding.source_target, binding.destination_target)
    targets = {
        target.target_id: target
        for target in store.list_production_backup_target_records(status="active")
    }
    lanes: dict[tuple[str, str], set[tuple[str, str, str, str, str, str]]] = {}
    for policy in store.list_production_backup_policy_records(status="active"):
        source = targets.get(policy.fast_snapshot.source_target_id)
        destination = targets.get(policy.independent_backup.destination_target_id)
        if source is None or destination is None:
            continue
        try:
            candidate = capture_boundary(policy, source, destination)
        except ValueError:
            continue
        lanes.setdefault((policy.context, policy.instance), set()).add(candidate)
    records = {
        record.secret_id: record
        for record in store.list_secret_records(
            integration=secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
        )
        if record.scope == "context_instance" and record.status == "configured"
    }
    bindings = store.list_secret_bindings(integration=secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION)
    candidates: dict[tuple[str, ...], tuple[tuple[SecretBinding, SecretRecord], ...]] = {}
    for lane, boundaries in lanes.items():
        if boundary not in boundaries:
            continue
        matches_by_key = {
            key: [
                (credential, records[credential.secret_id])
                for credential in bindings
                if credential.secret_id in records
                and credential.binding_key == key
                and credential.status == "configured"
                and (credential.context, credential.instance) == lane
                and (records[credential.secret_id].context, records[credential.secret_id].instance)
                == lane
            ]
            for key in CAPTURE_KEYS
        }
        if not any(matches_by_key.values()):
            continue
        if boundaries != {boundary} or any(
            len(matches) != 1 for matches in matches_by_key.values()
        ):
            raise ValueError("Guest capture credential ownership is ambiguous.")
        selected = [matches[0] for matches in matches_by_key.values()]
        identifiers = tuple(sorted(record.secret_id for _, record in selected))
        if len(set(identifiers)) != len(CAPTURE_KEYS) or any(
            credential.secret_id in identifiers and credential not in [item[0] for item in selected]
            for credential in bindings
            if credential.status == "configured"
        ):
            raise ValueError("Guest capture credentials have ambiguous consumers.")
        candidates[identifiers] = tuple(selected)
    if len(candidates) != 1:
        raise ValueError("Guest capture credentials are missing or ambiguous.")
    return next(iter(candidates.values()))


def resolve_guest_capture_values(
    store: PostgresRecordStore,
    binding: ProductionBackupGateWorkerRequest,
) -> dict[str, str]:
    values = {}
    for credential, record in guest_capture_metadata(store, binding):
        version = store.read_secret_version(record.current_version_id)
        if version.secret_id != record.secret_id:
            raise ValueError("Guest capture version does not belong to its credential.")
        values[credential.binding_key] = secrets._decrypt_secret_value(
            version.ciphertext, version.key_id
        )
    return values


def shared_snapshot_retention(
    store: PostgresRecordStore, binding: ProductionBackupGateWorkerRequest
) -> int:
    source = binding.source_target.destination
    assert isinstance(source, ProxmoxGuestBackupDestinationReference)
    guest_identity = source.host.lower(), source.guest_kind, source.guest_id
    targets = {
        target.target_id: target
        for target in store.list_production_backup_target_records(status="active")
    }
    retention = binding.policy.fast_snapshot.retention_count
    for policy in store.list_production_backup_policy_records(status="active"):
        if policy.fast_snapshot.snapshot_prefix != binding.policy.fast_snapshot.snapshot_prefix:
            continue
        target = targets.get(policy.fast_snapshot.source_target_id)
        if target is None:
            raise ValueError("Shared-prefix retention authority is unavailable.")
        guest = target.destination
        if not isinstance(guest, ProxmoxGuestBackupDestinationReference):
            continue
        if (guest.host.lower(), guest.guest_kind, guest.guest_id) == guest_identity:
            retention = max(retention, policy.fast_snapshot.retention_count)
    return retention
