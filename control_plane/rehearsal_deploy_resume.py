"""Cooperative custody of an exact, already-triggered rehearsal execute job.

Only this new checkpoint format is resumed. Historical running journals and
unknown trigger outcomes keep their existing original-key recovery protocol.
The continuation observes; it never replays capture, upgrade or retirement.
"""

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.odoo_upgrade_rehearsal import (
    OdooUpgradeRehearsalRecord,
    OdooUpgradeRehearsalRequest,
)
from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter
from control_plane.service_deploy_drain import _read, lock
from control_plane.storage.postgres import (
    LaunchplaneServiceDeployDrainRow,
    PostgresRecordStore,
)


class RehearsalDeployCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation: LaunchplaneIdempotencyRecord
    request: OdooUpgradeRehearsalRequest
    rehearsal: OdooUpgradeRehearsalRecord
    product: str
    principal: str
    trace: str
    schedule_id: str
    deployment_id: str
    state: Literal["pending", "completed", "recovery_required"] = "pending"


def checkpoint_status(session: Session) -> tuple[dict[str, str], ...]:
    """Runtime diagnostics expose custody, never stored intent or provider values."""
    return tuple(
        {"operation_id": item.reservation.record_id, "state": item.state}
        for row in session.scalars(
            select(LaunchplaneServiceDeployDrainRow).where(
                LaunchplaneServiceDeployDrainRow.record_id.startswith("rehearsal-checkpoint:")
            )
        )
        if (item := RehearsalDeployCheckpoint.model_validate(row.payload)).state != "completed"
    )


def drain_requested(store: PostgresRecordStore) -> bool:
    with store._session_factory() as session:
        _, fence = _read(session)
        return bool(
            fence is not None
            and fence.state == "draining"
            and fence.expires_at > store._database_mutation_timestamp(session)
        )


def persist_checkpoint(
    adapter: OdooRehearsalAdapter,
    reservation: LaunchplaneIdempotencyRecord,
    schedule_id: str,
    deployment_id: str,
) -> LaunchplaneIdempotencyRecord:
    """Stop the API lease and save its continuation in one admission transaction."""
    store = adapter.store
    if (
        adapter.request.action != "execute"
        or adapter.request.mode != "apply"
        or not schedule_id.strip()
        or not deployment_id.strip()
        or reservation.state != "running"
        or reservation.scope != adapter.principal
        or reservation.provider_target_key != adapter.request.rehearsal_id
        or reservation.provider_effect_phase != "execute"
        or not reservation.provider_effect_started_at
    ):
        raise ValueError("Only an original dispatched execute can hand off job observation.")
    rehearsal = store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
    if rehearsal is None or rehearsal.binding_sha256 != adapter.request.reviewed_plan_sha256:
        raise ValueError("The checkpoint no longer matches the reviewed rehearsal.")
    adapter.operation_readback(rehearsal, reservation.idempotency_key)
    with store._session_factory() as session:
        lock(store, session)
        row = session.scalar(
            store._idempotency_statement(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                for_update=True,
            )
        )
        current = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
        now = store._database_mutation_timestamp(session)
        if current != reservation or reservation.lease_expires_at <= now:
            raise ValueError("The original executor no longer owns checkpoint admission.")
        held = reservation.model_copy(
            update={"state": "reconcile_required", "updated_at": now, "lease_expires_at": now}
        )
        checkpoint = RehearsalDeployCheckpoint(
            reservation=held,
            request=adapter.request,
            rehearsal=rehearsal,
            product=adapter.product,
            principal=adapter.principal,
            trace=adapter.trace,
            schedule_id=schedule_id,
            deployment_id=deployment_id,
        )
        checkpoint_id = "rehearsal-checkpoint:" + reservation.record_id
        if session.get(LaunchplaneServiceDeployDrainRow, checkpoint_id) is not None:
            raise ValueError("Original checkpoint custody already exists.")
        store._sync_idempotency_row(row, held)
        session.add(
            LaunchplaneServiceDeployDrainRow(
                record_id=checkpoint_id, payload=checkpoint.model_dump(mode="json")
            )
        )
        session.commit()
        return held


def remote_job_terminal(root: Path, checkpoint: RehearsalDeployCheckpoint) -> bool:
    from control_plane.dokploy import api, source

    host, token = source.read_dokploy_config(control_plane_root=root)
    job = api._schedule_deployment_by_key(
        host=host,
        token=token,
        schedule_id=checkpoint.schedule_id,
        deployment_id=checkpoint.deployment_id,
    )
    return job is not None and api._deployment_status(job) in (
        api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
    )


def resume_rehearsals(store: PostgresRecordStore, root: Path) -> tuple[str, ...]:
    """One bounded sweep, called by the existing Odoo worker from its first poll."""
    with store._session_factory() as session:
        _, fence = _read(session)
        # Worker containers carry the image identity. Only matching API startup
        # confirms the fence; an overlapping old image must never continue it.
        if fence is not None:
            timed_out_pause = (
                fence.state == "draining"
                and fence.expires_at <= store._database_mutation_timestamp(session)
            )
            matching_startup = (
                fence.state == "confirmed"
                and fence.image_reference == os.environ.get("DOCKER_IMAGE_REFERENCE", "").strip()
            )
            if not (timed_out_pause or matching_startup):
                return ()
        rows = tuple(
            (row.record_id, row.payload)
            for row in session.scalars(
                select(LaunchplaneServiceDeployDrainRow)
                .where(
                    LaunchplaneServiceDeployDrainRow.record_id.startswith("rehearsal-checkpoint:")
                )
                .order_by(LaunchplaneServiceDeployDrainRow.record_id)
            )
            if row.payload.get("state") == "pending"
        )
    completed: list[str] = []
    for checkpoint_id, payload in rows:
        with store.testing_replacement_execution_lock("resume:" + checkpoint_id) as exclusive:
            if exclusive is None:
                continue
            try:
                checkpoint = RehearsalDeployCheckpoint.model_validate(payload)
                reservation = checkpoint.reservation
                with store._session_factory() as session:
                    row = session.get(LaunchplaneServiceDeployDrainRow, checkpoint_id)
                    if row is None or row.payload != payload:
                        continue
                current = store.read_idempotency_record(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                )
                if current is not None and current.state == "completed":
                    state: Literal["completed", "recovery_required"] = "completed"
                elif current != reservation:
                    state = "recovery_required"
                else:
                    adapter = OdooRehearsalAdapter(
                        root,
                        store,
                        checkpoint.product,
                        checkpoint.request,
                        checkpoint.principal,
                        checkpoint.trace,
                    )
                    with store.testing_replacement_execution_lock(adapter.target_key()) as check:
                        if check is None:
                            continue
                        record = store.read_odoo_upgrade_rehearsal(adapter.target_key())
                        if record is None:
                            continue
                        # A crash after publishing the proven host result but before
                        # journal adoption must also converge without another effect.
                        original = checkpoint.rehearsal
                        published = (
                            record.state == "upgraded"
                            and record.model_copy(
                                update={
                                    "state": original.state,
                                    "copies": original.copies,
                                    "elapsed_seconds": original.elapsed_seconds,
                                    "updated_at": original.updated_at,
                                }
                            )
                            == original
                        )
                        if record != original and not published:
                            continue
                        adapter.operation_readback(original, reservation.idempotency_key)
                        check()
                    if not remote_job_terminal(root, checkpoint):
                        continue
                    from control_plane.provider_operations import build_provider_operation_key

                    operation_key = build_provider_operation_key(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                        request_fingerprint=reservation.request_fingerprint,
                        reconciliation_key=reservation.reconciliation_key,
                    )
                    observation = adapter.observe(
                        operation_key,
                        reservation.provider_effect_phase,
                        reservation.reconciliation_key,
                    )
                    if observation.outcome != "present":
                        continue
                    exclusive()
                    adopted = store.adopt_reconciled_mutation(
                        reservation=reservation,
                        response_status_code=observation.response_status_code,
                        response_trace_id=checkpoint.trace,
                        response_payload=observation.response_payload,
                    )
                    if adopted.status not in {"adopted", "replayed"}:
                        continue
                    state = "completed"
                with store._session_factory() as session:
                    store._begin_serialized_write(session)
                    row = session.get(LaunchplaneServiceDeployDrainRow, checkpoint_id)
                    if row is not None and row.payload == payload:
                        row.payload = checkpoint.model_copy(update={"state": state}).model_dump(
                            mode="json"
                        )
                        session.commit()
                if state == "completed":
                    completed.append(reservation.record_id)
            except Exception:
                # A transient provider/readback failure preserves original custody.
                logging.exception("Rehearsal checkpoint observation remains pending.")
    return tuple(completed)
