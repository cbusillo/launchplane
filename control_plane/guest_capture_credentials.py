from __future__ import annotations

from control_plane import secrets
from control_plane.contracts.guest_capture_boundary import (
    GuestCaptureBoundary,
    GUEST_CAPTURE_INTEGRATION,
)
from control_plane.contracts.production_backup_authority import (
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
)
from control_plane.contracts.production_backup_gate import (
    ProductionBackupGateWorkerRequest,
    ProductionBackupGateRequest,
)
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.storage.postgres import PostgresRecordStore


CAPTURE_KEYS = ("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS")


def capture_boundary(binding: ProductionBackupGateWorkerRequest) -> GuestCaptureBoundary:
    source, destination = binding.source_target.destination, binding.destination_target.destination
    if not isinstance(source, ProxmoxGuestBackupDestinationReference) or not isinstance(
        destination, ProxmoxStorageBackupDestinationReference
    ):
        raise ValueError("Capture requires guest and storage authority.")
    return GuestCaptureBoundary(
        host=source.host,
        username=source.username,
        guest_kind=source.guest_kind,
        guest_id=source.guest_id,
        storage_id=destination.storage_id,
        snapshot_prefix=binding.policy.fast_snapshot.snapshot_prefix,
    )


def plan_guest_credential_adoption(
    *,
    binding: ProductionBackupGateWorkerRequest,
    records: tuple[SecretRecord, ...],
    bindings: tuple[SecretBinding, ...],
) -> tuple[tuple[SecretRecord, ...], tuple[SecretBinding, ...]]:
    boundary = capture_boundary(binding)
    if any(r.capture_boundary == boundary for r in records):
        raise ValueError("This guest boundary already has capture credentials.")
    lane = binding.request.context, binding.request.instance
    candidates = {
        r.secret_id: r
        for r in records
        if r.status == "configured"
        and r.integration == secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
        and r.scope == "context_instance"
        and (r.context, r.instance) == lane
    }
    selected: list[SecretBinding] = []
    for key in CAPTURE_KEYS:
        matches = [
            b
            for b in bindings
            if b.status == "configured"
            and b.binding_key == key
            and b.integration == secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION
            and (b.context, b.instance) == lane
            and b.secret_id in candidates
        ]
        if len(matches) != 1:
            raise ValueError("Capture credentials are missing or ambiguous.")
        selected.append(matches[0])
    selected_ids = {b.secret_id for b in selected}
    if len(selected_ids) != len(CAPTURE_KEYS) or any(
        b.secret_id in selected_ids and b not in selected for b in bindings
    ):
        raise ValueError("Capture credentials have ambiguous consumers.")
    adopted_records = tuple(
        candidates[b.secret_id].model_copy(
            update={
                "integration": GUEST_CAPTURE_INTEGRATION,
                "capture_boundary": boundary,
            }
        )
        for b in selected
    )
    adopted_bindings = tuple(
        b.model_copy(
            update={
                "integration": GUEST_CAPTURE_INTEGRATION,
                "declared_secret_class": None,
                "sharing_reason": None,
            }
        )
        for b in selected
    )
    return adopted_records, adopted_bindings


def guest_capture_metadata(
    store: PostgresRecordStore,
    binding: ProductionBackupGateWorkerRequest,
) -> tuple[tuple[SecretBinding, SecretRecord], ...]:
    boundary = capture_boundary(binding)
    records = {
        r.secret_id: r
        for r in store.list_secret_records(integration=GUEST_CAPTURE_INTEGRATION)
        if r.capture_boundary == boundary
    }
    bindings = store.list_secret_bindings(integration=GUEST_CAPTURE_INTEGRATION)
    selected = []
    for key in CAPTURE_KEYS:
        matches = [
            (b, records[b.secret_id])
            for b in bindings
            if b.secret_id in records
            and b.binding_key == key
            and (b.context, b.instance)
            == (records[b.secret_id].context, records[b.secret_id].instance)
        ]
        if (
            len(matches) != 1
            or matches[0][0].status != "configured"
            or matches[0][1].status != "configured"
        ):
            raise ValueError("Guest capture credentials are missing, disabled or ambiguous.")
        selected.append(matches[0])
    if len({r.secret_id for _, r in selected}) != len(CAPTURE_KEYS):
        raise ValueError("Guest capture credentials must be distinct.")
    return tuple(selected)


def resolve_guest_capture_values(
    store: PostgresRecordStore,
    binding: ProductionBackupGateWorkerRequest,
) -> dict[str, str]:
    selected = guest_capture_metadata(store, binding)
    values = {}
    for credential, record in selected:
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
    source_guest = source.host.lower(), source.guest_kind, str(int(source.guest_id))
    targets = {t.target_id: t for t in store.list_production_backup_target_records(status="active")}
    retention = binding.policy.fast_snapshot.retention_count
    for policy in store.list_production_backup_policy_records(status="active"):
        if policy.fast_snapshot.snapshot_prefix != binding.policy.fast_snapshot.snapshot_prefix:
            continue
        target = targets.get(policy.fast_snapshot.source_target_id)
        if target is None or not isinstance(
            target.destination, ProxmoxGuestBackupDestinationReference
        ):
            raise ValueError("Shared-prefix retention authority is unavailable.")
        guest = target.destination
        if (guest.host.lower(), guest.guest_kind, str(int(guest.guest_id))) == source_guest:
            retention = max(retention, policy.fast_snapshot.retention_count)
    return retention


def plan_guest_credential_adoptions(
    *,
    records: tuple[SecretRecord, ...],
    bindings: tuple[SecretBinding, ...],
    policies: tuple[ProductionBackupPolicyRecord, ...],
    targets: tuple[ProductionBackupTargetRecord, ...],
    context: str = "",
    instance: str = "",
) -> tuple[tuple[SecretRecord, ...], tuple[SecretBinding, ...]]:
    active_targets = {t.target_id: t for t in targets if t.status == "active"}
    candidates: dict[
        GuestCaptureBoundary,
        dict[tuple[str, ...], tuple[tuple[SecretRecord, ...], tuple[SecretBinding, ...]]],
    ] = {}
    lane_boundaries: dict[tuple[str, str], set[GuestCaptureBoundary]] = {}
    for policy in policies:
        if policy.status != "active":
            continue
        source = active_targets.get(policy.fast_snapshot.source_target_id)
        destination = active_targets.get(policy.independent_backup.destination_target_id)
        if source is None or destination is None:
            continue
        request = ProductionBackupGateRequest(
            product=policy.product,
            context=policy.context,
            instance=policy.instance,
            promotion_action=policy.promotion_action,
            backup_record_id="credential-adoption",
        )
        binding = ProductionBackupGateWorkerRequest(
            request=request, policy=policy, source_target=source, destination_target=destination
        )
        boundary = capture_boundary(binding)
        lane_boundaries.setdefault((policy.context, policy.instance), set()).add(boundary)
        try:
            adopted = plan_guest_credential_adoption(
                binding=binding, records=records, bindings=bindings
            )
        except ValueError:
            continue
        identifiers = tuple(sorted(r.secret_id for r in adopted[0]))
        candidates.setdefault(boundary, {})[identifiers] = adopted
    adopted_records: list[SecretRecord] = []
    adopted_bindings: list[SecretBinding] = []
    originals = {r.secret_id: r for r in records}
    for options in candidates.values():
        if len(options) != 1:
            continue
        record_changes, binding_changes = next(iter(options.values()))
        origin = originals[record_changes[0].secret_id]
        if len(lane_boundaries[(origin.context, origin.instance)]) != 1:
            continue
        if context and (origin.context, origin.instance) != (context, instance):
            continue
        adopted_records.extend(record_changes)
        adopted_bindings.extend(binding_changes)
    return tuple(adopted_records), tuple(adopted_bindings)
