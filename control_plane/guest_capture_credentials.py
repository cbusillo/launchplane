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
from control_plane.contracts.guest_capture_credential import (
    GuestCaptureBoundary,
    GuestCaptureCredential,
)


SSH_PRIVATE_KEY = "PRODUCTION_BACKUP_SSH_PRIVATE_KEY"
SSH_KNOWN_HOSTS = "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"
CAPTURE_KEYS = (SSH_PRIVATE_KEY, SSH_KNOWN_HOSTS)


def capture_boundary(
    policy: ProductionBackupPolicyRecord,
    source: ProductionBackupTargetRecord,
    destination: ProductionBackupTargetRecord,
) -> GuestCaptureBoundary:
    guest, storage = source.destination, destination.destination
    if not isinstance(guest, ProxmoxGuestBackupDestinationReference) or not isinstance(
        storage, ProxmoxStorageBackupDestinationReference
    ):
        raise ValueError("Capture requires guest and storage authority.")
    if (guest.host.lower(), guest.username) != (storage.host.lower(), storage.username):
        raise ValueError("Capture targets must use the same endpoint.")
    return GuestCaptureBoundary(
        host=guest.host.lower(),
        username=guest.username,
        guest_kind=guest.guest_kind,
        guest_id=guest.guest_id,
        storage_id=storage.storage_id,
        snapshot_prefix=policy.fast_snapshot.snapshot_prefix,
    )


def plan_guest_capture_credential(
    boundary: GuestCaptureBoundary,
    records: tuple[SecretRecord, ...],
    bindings: tuple[SecretBinding, ...],
    context: str,
    instance: str,
) -> GuestCaptureCredential:
    available = {
        record.secret_id: record
        for record in records
        if record.scope == "context_instance"
        and record.integration == secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
        and (record.context, record.instance) == (context, instance)
        and record.status == "configured"
    }
    selected = {}
    for key in CAPTURE_KEYS:
        matches = [
            binding
            for binding in bindings
            if binding.secret_id in available
            and binding.integration == secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
            and (binding.context, binding.instance) == (context, instance)
            and binding.binding_key == key
            and binding.status == "configured"
        ]
        if len(matches) != 1:
            raise ValueError("Capture credentials are missing or ambiguous.")
        selected[key] = matches[0]
    private_key, hosts = selected[SSH_PRIVATE_KEY], selected[SSH_KNOWN_HOSTS]
    if private_key.secret_id == hosts.secret_id or any(
        binding.secret_id in {private_key.secret_id, hosts.secret_id}
        and binding.binding_id not in {private_key.binding_id, hosts.binding_id}
        and binding.status == "configured"
        for binding in bindings
    ):
        raise ValueError("Capture credentials have ambiguous consumers.")
    return GuestCaptureCredential(
        boundary=boundary,
        private_key_secret_id=private_key.secret_id,
        known_hosts_secret_id=hosts.secret_id,
        custody_context=context,
        custody_instance=instance,
    )


def guest_capture_metadata(
    store: PostgresRecordStore,
    binding: ProductionBackupGateWorkerRequest,
) -> tuple[tuple[SecretBinding, SecretRecord], ...]:
    boundary = capture_boundary(binding.policy, binding.source_target, binding.destination_target)
    credential = store.read_guest_capture_binding(boundary)
    if credential is None:
        raise ValueError("The guest capture credential has not been bound.")
    selected = []
    for secret_id, key in (
        (credential.private_key_secret_id, SSH_PRIVATE_KEY),
        (credential.known_hosts_secret_id, SSH_KNOWN_HOSTS),
    ):
        record = store.read_secret_record(secret_id)
        matches = [
            item
            for item in store.list_secret_bindings(
                integration=secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
                context_name=credential.custody_context,
                instance_name=credential.custody_instance,
            )
            if item.secret_id == secret_id
            and item.binding_key == key
            and item.status == "configured"
            and (item.context, item.instance)
            == (credential.custody_context, credential.custody_instance)
        ]
        if (
            record.integration != secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
            or record.status != "configured"
            or record.scope != "context_instance"
            or not matches
            or (record.context, record.instance)
            != (credential.custody_context, credential.custody_instance)
        ):
            raise ValueError("The guest capture credential is unavailable or changed.")
        selected.append((max(matches, key=lambda item: (item.updated_at, item.binding_id)), record))
    return tuple(selected)


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
