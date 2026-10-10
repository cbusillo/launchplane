"""Durable release admission boundary for replacement of the control plane.

The fence shares a transaction lock with worker claims and generic-web mutation
reservations. Pending work is already durable; running work must commit before
replacement is dispatched. A requested replacement never expires: only startup
of its exact image and marker can confirm it. Old workers remain fenced even
after confirmation, so overlapping containers cannot admit work before exiting.
"""

from datetime import UTC, datetime, timedelta
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text


class ServiceDeployDrainRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_fingerprint: str
    target_type: str
    target_id: str
    image_reference: str
    deployment_marker: str
    state: Literal["draining", "dispatching", "requested", "confirmed"]
    updated_at: str
    expires_at: str = ""


class ServiceDeployDraining(ValueError):
    """No new provider effect was admitted while the service drains."""


class ServiceDeployOutcomeUnknown(ValueError):
    """The provider dispatch may have happened; never replay it automatically."""


def lock(store: Any, session: Any) -> None:
    if store.database_url.startswith("sqlite"):
        store._begin_serialized_write(session)
    else:
        session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "launchplane:service-deploy-release-admission"},
        )


def _read(session: Any) -> tuple[Any, ServiceDeployDrainRecord | None]:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    row = session.get(LaunchplaneServiceDeployDrainRow, "service")
    return row, ServiceDeployDrainRecord.model_validate(row.payload) if row else None


def admission_allowed(session: Any, now: str) -> bool:
    _, record = _read(session)
    if record is None:
        return True
    if record.state == "draining":
        # An abandoned pre-effect request cannot permanently park releases.
        return record.expires_at <= now
    return record.state == "confirmed" and (
        record.image_reference == os.environ.get("DOCKER_IMAGE_REFERENCE", "").strip()
        and record.deployment_marker == os.environ.get("LAUNCHPLANE_DEPLOYMENT_MARKER", "").strip()
    )


def _running_operations(session: Any) -> tuple[str, ...]:
    from control_plane.storage.postgres import (
        LaunchplaneIdempotencyRow,
        LaunchplaneOdooProdPromotionOperationRow,
        LaunchplaneOdooProdRollbackOperationRow,
        LaunchplaneVeriReelProdBackupGateOperationRow,
    )

    ids: list[str] = []
    for row_type in (
        LaunchplaneOdooProdPromotionOperationRow,
        LaunchplaneOdooProdRollbackOperationRow,
        LaunchplaneVeriReelProdBackupGateOperationRow,
    ):
        ids.extend(
            session.scalars(select(row_type.operation_id).where(row_type.status == "running"))
        )
    ids.extend(
        session.scalars(
            select(LaunchplaneIdempotencyRow.record_id).where(
                LaunchplaneIdempotencyRow.scope == "client-release",
                LaunchplaneIdempotencyRow.state == "running",
            )
        )
    )
    return tuple(sorted(ids))


def prepare(
    store: Any,
    *,
    request_fingerprint: str,
    target_type: str,
    target_id: str,
    image_reference: str,
    deployment_marker: str,
) -> tuple[ServiceDeployDrainRecord, tuple[str, ...], bool]:
    """Return the durable fence, blockers, and one-time dispatch ownership.

    A repeated request after dispatch returns the saved state without dispatching
    again, including after a lost HTTP response. A different authorized self-deploy
    can replace a requested fence (e.g. rollback), but cannot race a live drain.
    """
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    with store._session_factory() as session:
        lock(store, session)
        now = store._database_mutation_timestamp(session)
        row, current = _read(session)
        if current is not None and current.request_fingerprint == request_fingerprint:
            if current.state == "dispatching":
                raise ServiceDeployOutcomeUnknown(
                    "Self-deploy dispatch requires provider reconciliation."
                )
            if current.state != "draining":
                return current, (), False
        if not deployment_marker or deployment_marker == os.environ.get(
            "LAUNCHPLANE_DEPLOYMENT_MARKER"
        ):
            raise ValueError(
                "Self-deploy requires a fresh deployment marker before draining releases."
            )
        if current is not None and current.state == "dispatching":
            raise ServiceDeployOutcomeUnknown(
                "An earlier self-deploy dispatch requires provider reconciliation."
            )
        if (
            current is not None
            and current.request_fingerprint != request_fingerprint
            and current.state == "draining"
            and current.expires_at > now
        ):
            raise ValueError("Another self-deploy is draining release operations.")
        blockers = _running_operations(session)
        state: Literal["draining", "dispatching"] = "draining" if blockers else "dispatching"
        record = ServiceDeployDrainRecord(
            request_fingerprint=request_fingerprint,
            target_type=target_type,
            target_id=target_id,
            image_reference=image_reference,
            deployment_marker=deployment_marker,
            state=state,
            updated_at=now,
            expires_at=(
                (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=2))
                .isoformat()
                .replace("+00:00", "Z")
                if blockers
                else ""
            ),
        )
        if row is None:
            session.add(
                LaunchplaneServiceDeployDrainRow(record_id="service", payload=record.model_dump())
            )
        else:
            row.payload = record.model_dump()
        session.commit()
        return record, blockers, not blockers


def record_dispatch(store: Any, request_fingerprint: str) -> None:
    with store._session_factory() as session:
        lock(store, session)
        row, record = _read(session)
        if record is None or record.request_fingerprint != request_fingerprint:
            raise ServiceDeployOutcomeUnknown("Self-deploy fence changed during provider dispatch.")
        if record.state == "dispatching":
            row.payload = record.model_copy(update={"state": "requested"}).model_dump()
            session.commit()


def confirm_startup(store: Any) -> None:
    """Only the replacement's healthy startup releases admission on that worker image."""
    with store._session_factory() as session:
        lock(store, session)
        row, record = _read(session)
        if record is None or record.state not in {"dispatching", "requested"}:
            return
        if (
            record.image_reference != os.environ.get("DOCKER_IMAGE_REFERENCE", "").strip()
            or record.deployment_marker
            != os.environ.get("LAUNCHPLANE_DEPLOYMENT_MARKER", "").strip()
        ):
            return
        row.payload = record.model_copy(
            update={"state": "confirmed", "updated_at": store._database_mutation_timestamp(session)}
        ).model_dump()
        session.commit()


def read_status(store: Any) -> dict[str, object]:
    with store._session_factory() as session:
        _, record = _read(session)
        if record is None:
            return {"state": "idle", "running_operation_ids": ()}
        result = record.model_dump()
        result["running_operation_ids"] = _running_operations(session)
        result["admission_paused"] = not admission_allowed(session, datetime.now(UTC).isoformat())
        return result
