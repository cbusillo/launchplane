from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import ValidationError

from control_plane.contracts.guest_capture_credential import (
    GUEST_CAPTURE_BINDING_PREFIX,
    GuestCaptureBoundary,
    GuestCaptureCredential,
    guest_capture_credential_from_event,
)
from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
)
from control_plane.contracts.secret_record import SecretAuditEvent, SecretRecord, SecretBinding
from control_plane.guest_capture_credentials import (
    capture_boundary,
    plan_guest_capture_credential,
    validate_guest_capture_enrollment,
)
from control_plane.secrets import LAUNCHPLANE_WORKER_SECRET_INTEGRATION
from control_plane.workflows.ship import utc_now_timestamp

GUEST_CAPTURE_BINDING_WRITE_STEP = "bind_guest_capture_credential"

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore


def read_guest_capture_binding(
    session: Session, boundary: GuestCaptureBoundary
) -> GuestCaptureCredential | None:
    from control_plane.storage.postgres import LaunchplaneSecretAuditEventRow

    row = session.get(LaunchplaneSecretAuditEventRow, boundary.event_id, populate_existing=True)
    if row is None:
        return None
    return guest_capture_credential_from_event(SecretAuditEvent.model_validate(row.payload))


def guest_capture_bindings(session: Session) -> tuple[GuestCaptureCredential, ...]:
    from control_plane.storage.postgres import LaunchplaneSecretAuditEventRow

    rows = session.scalars(
        select(LaunchplaneSecretAuditEventRow).where(
            LaunchplaneSecretAuditEventRow.event_id.startswith(GUEST_CAPTURE_BINDING_PREFIX)
        )
    ).all()
    return tuple(
        guest_capture_credential_from_event(SecretAuditEvent.model_validate(row.payload))
        for row in rows
    )


def enroll_guest_capture_binding_in_session(
    store: PostgresRecordStore,
    session: Session,
    credential: GuestCaptureCredential,
    state: dict[str, list[dict[str, Any]]],
) -> None:
    validate_guest_capture_enrollment(credential, state)
    if read_guest_capture_binding(session, credential.boundary) is not None:
        return
    session.add(store._secret_audit_event_row(credential.audit_event(utc_now_timestamp())))
    session.flush()
    store._after_product_authority_bundle_step(GUEST_CAPTURE_BINDING_WRITE_STEP)


def enrollment_state(
    session: Session, context: str, instance: str
) -> dict[str, list[dict[str, Any]]]:
    from control_plane.storage.postgres import (
        LaunchplaneProductionBackupPolicyRow,
        LaunchplaneProductionBackupTargetRow,
        LaunchplaneSecretRow,
        LaunchplaneSecretAuditEventRow,
    )

    policies = session.scalars(
        select(LaunchplaneProductionBackupPolicyRow)
        .where(
            LaunchplaneProductionBackupPolicyRow.context == context,
            LaunchplaneProductionBackupPolicyRow.instance == instance,
            LaunchplaneProductionBackupPolicyRow.status == "active",
        )
        .order_by(LaunchplaneProductionBackupPolicyRow.record_id)
    ).all()
    target_ids = {
        target
        for policy in policies
        for target in (policy.source_target_id, policy.destination_target_id)
    }
    targets = session.scalars(
        select(LaunchplaneProductionBackupTargetRow)
        .where(
            LaunchplaneProductionBackupTargetRow.target_id.in_(target_ids),
            LaunchplaneProductionBackupTargetRow.status == "active",
        )
        .order_by(LaunchplaneProductionBackupTargetRow.record_id)
    ).all()
    by_id = {
        target.target_id: ProductionBackupTargetRecord.model_validate(target.payload)
        for target in targets
    }
    boundaries = set()
    for row in policies:
        policy = ProductionBackupPolicyRecord.model_validate(row.payload)
        source, destination = (
            by_id.get(policy.fast_snapshot.source_target_id),
            by_id.get(policy.independent_backup.destination_target_id),
        )
        if source is not None and destination is not None:
            boundaries.add(capture_boundary(policy, source, destination))
    lane_ids = set(
        session.scalars(
            select(LaunchplaneSecretRow.secret_id).where(
                LaunchplaneSecretRow.integration == LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
                LaunchplaneSecretRow.context == context,
                LaunchplaneSecretRow.instance == instance,
            )
        )
    )
    events = session.scalars(
        select(LaunchplaneSecretAuditEventRow)
        .where(LaunchplaneSecretAuditEventRow.event_id.startswith(GUEST_CAPTURE_BINDING_PREFIX))
        .order_by(LaunchplaneSecretAuditEventRow.event_id)
    ).all()
    relevant = []
    for audit_row in events:
        try:
            pin = guest_capture_credential_from_event(
                SecretAuditEvent.model_validate(audit_row.payload)
            )
        except ValueError:
            if (
                audit_row.event_id in {boundary.event_id for boundary in boundaries}
                or audit_row.secret_id in lane_ids
            ):
                raise
            continue
        if pin.boundary in boundaries or pin.secret_ids & lane_ids:
            relevant.append(audit_row.payload)
    return {
        "policies": [policy_row.payload for policy_row in policies],
        "targets": [target_row.payload for target_row in targets],
        "pins": relevant,
    }


def seed_guest_capture_bindings_in_session(
    store: PostgresRecordStore,
    session: Session,
) -> int:
    from control_plane.storage.postgres import (
        LaunchplaneProductionBackupPolicyRow,
        LaunchplaneProductionBackupTargetRow,
        LaunchplaneSecretRow,
        LaunchplaneSecretBindingRow,
        LaunchplaneSecretAuditEventRow,
    )

    records = []
    for secret_row in session.scalars(
        select(LaunchplaneSecretRow).where(
            LaunchplaneSecretRow.integration == LAUNCHPLANE_WORKER_SECRET_INTEGRATION
        )
    ):
        try:
            records.append(SecretRecord.model_validate(secret_row.payload))
        except ValidationError:
            continue
    bindings = []
    for binding_row in session.scalars(
        select(LaunchplaneSecretBindingRow).where(
            LaunchplaneSecretBindingRow.integration == LAUNCHPLANE_WORKER_SECRET_INTEGRATION
        )
    ):
        try:
            bindings.append(SecretBinding.model_validate(binding_row.payload))
        except ValidationError:
            continue
    existing: dict[str, GuestCaptureCredential | None] = {}
    reserved = set()
    for audit_row in session.scalars(
        select(LaunchplaneSecretAuditEventRow).where(
            LaunchplaneSecretAuditEventRow.event_id.startswith(GUEST_CAPTURE_BINDING_PREFIX)
        )
    ):
        reserved.add(audit_row.secret_id)
        try:
            pin = guest_capture_credential_from_event(
                SecretAuditEvent.model_validate(audit_row.payload)
            )
        except ValueError:
            existing[audit_row.event_id] = None
            continue
        existing[audit_row.event_id] = pin
        reserved.update(pin.secret_ids)
    targets = {}
    for target_row in session.scalars(
        select(LaunchplaneProductionBackupTargetRow).where(
            LaunchplaneProductionBackupTargetRow.status == "active"
        )
    ):
        try:
            targets[target_row.target_id] = ProductionBackupTargetRecord.model_validate(
                target_row.payload
            )
        except ValidationError:
            continue
    lanes: dict[tuple[str, str], set[GuestCaptureBoundary]] = {}
    for policy_row in session.scalars(
        select(LaunchplaneProductionBackupPolicyRow).where(
            LaunchplaneProductionBackupPolicyRow.status == "active"
        )
    ):
        try:
            policy = ProductionBackupPolicyRecord.model_validate(policy_row.payload)
            source, destination = (
                targets.get(policy.fast_snapshot.source_target_id),
                targets.get(policy.independent_backup.destination_target_id),
            )
            if source is None or destination is None:
                continue
            boundary = capture_boundary(policy, source, destination)
        except ValueError:
            continue
        lanes.setdefault((policy.context, policy.instance), set()).add(boundary)
    candidates: dict[GuestCaptureBoundary, dict[frozenset[str], GuestCaptureCredential]] = {}
    for (lane_context, lane_instance), boundaries in lanes.items():
        if len(boundaries) != 1:
            continue
        boundary = next(iter(boundaries))
        if boundary.event_id in existing:
            continue
        try:
            candidate = plan_guest_capture_credential(
                boundary,
                tuple(record for record in records if record.secret_id not in reserved),
                tuple(bindings),
                lane_context,
                lane_instance,
            )
        except ValueError:
            continue
        candidates.setdefault(boundary, {})[candidate.secret_ids] = candidate
    added = 0
    for boundary, options in candidates.items():
        if len(options) != 1:
            continue
        pin = next(iter(options.values()))
        if pin.secret_ids & reserved:
            continue
        session.add(store._secret_audit_event_row(pin.audit_event(utc_now_timestamp())))
        session.flush()
        store._after_product_authority_bundle_step(GUEST_CAPTURE_BINDING_WRITE_STEP)
        reserved.update(pin.secret_ids)
        added += 1
    return added
