"""Durable release admission boundary for replacement of the control plane.

The fence shares a transaction lock with worker claims and generic-web mutation
reservations. Pending work is already durable; running work must commit before
replacement is dispatched. A requested replacement never expires: only startup
of its exact image and marker can confirm it. Old workers remain fenced even
after confirmation, so overlapping containers cannot admit work before exiting.
"""

from datetime import datetime, timedelta
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from control_plane.contracts.product_reconcile import RECONCILE_SOURCE
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp


SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS = 10 * 60


class ServiceDeployDrainRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_fingerprint: str
    target_type: str
    target_id: str
    image_reference: str
    deployment_marker: str
    previous_request_fingerprint: str = ""
    state: Literal["draining", "dispatching", "requested", "confirmed", "refused"]
    updated_at: str
    expires_at: str = ""


class ServiceDeployDrainBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drain_started_at: str
    drain_deadline_at: str
    drain_timed_out: bool = False


class ServiceDeployDraining(ValueError):
    """No new provider effect was admitted while the service drains."""


class ServiceDeployOutcomeUnknown(ValueError):
    """The provider dispatch may have happened; never replay it automatically."""


class ServiceDeployDispatchBusy(ValueError):
    """A live service request still owns dispatch; poll without caching this state."""


class ServiceDeployFenceConflict(ServiceDeployOutcomeUnknown):
    """This request did not acquire an earlier or mismatched replacement fence."""

    def __init__(self, message: str, *, fence: ServiceDeployDrainRecord | None = None) -> None:
        super().__init__(message)
        self.fence = fence


class ServiceDeployPreEffectRefused(ValueError):
    """The provider definitely refused the first env write; no replacement started."""


class ServiceDeployDrainTimeout(ServiceDeployPreEffectRefused):
    """The absolute pre-effect budget elapsed; no provider write was admitted."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        super().__init__(
            "Self-deploy drain timed out before provider effects; inspect runtime.release_drain."
        )
        self.blockers = blockers


@contextmanager
def dispatch_lock(store: Any) -> Iterator[None]:
    """A repair cannot overtake a still-running self-deploy provider call."""
    if store.database_dialect_name == "sqlite":
        from control_plane.storage.filesystem import FilesystemRecordStore

        database = store._engine.url.database
        if not database or database == ":memory:":
            raise ValueError("Self-deploy serialization requires a durable database.")
        database_path = Path(database).resolve()
        with FilesystemRecordStore(database_path.parent)._exclusive_record_lock(
            "service-deploy-dispatch", str(database_path)
        ):
            yield
        return
    with store._session_factory() as session, session.begin():
        if not session.scalar(
            text("select pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "launchplane:service-deploy-provider-dispatch"},
        ):
            raise ServiceDeployDispatchBusy("A self-deploy provider call is still in progress.")
        yield


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


def _write(session: Any, record: ServiceDeployDrainRecord) -> None:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    # Keep dispatch receipts independently of the current service fence. A later
    # replacement must not make an old lost-response request dispatchable again.
    for record_id in ("service", f"request:{record.request_fingerprint}"):
        row = session.get(LaunchplaneServiceDeployDrainRow, record_id)
        if row is None:
            session.add(
                LaunchplaneServiceDeployDrainRow(record_id=record_id, payload=record.model_dump())
            )
        else:
            row.payload = record.model_dump()


def _read_budget(session: Any, request_fingerprint: str) -> ServiceDeployDrainBudget | None:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    row = session.get(LaunchplaneServiceDeployDrainRow, f"budget:{request_fingerprint}")
    return ServiceDeployDrainBudget.model_validate(row.payload) if row else None


def _write_budget(session: Any, request_fingerprint: str, budget: ServiceDeployDrainBudget) -> None:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    # Separate receipt: older image/worker readers forbid extra fence fields.
    # Keep their exact fence payload compatible during overlap or rollback.
    record_id = f"budget:{request_fingerprint}"
    row = session.get(LaunchplaneServiceDeployDrainRow, record_id)
    if row is None:
        session.add(
            LaunchplaneServiceDeployDrainRow(record_id=record_id, payload=budget.model_dump())
        )
    else:
        row.payload = budget.model_dump()


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


def reservation_requires_admission(scope: str) -> bool:
    return scope.strip() == "client-release" or scope.strip().startswith(f"{RECONCILE_SOURCE}:")


def admit_reservation(store: Any, session: Any, scope: str) -> None:
    """Order every fresh/retried event effect against the self-deploy fence."""
    if not reservation_requires_admission(scope):
        return
    if not store.database_url.startswith("sqlite"):
        lock(store, session)
    if not admission_allowed(session, store._database_mutation_timestamp(session)):
        raise ServiceDeployDraining("Launchplane is draining product provider effects.")


def _operation_observations(store: Any, session: Any, now: str) -> tuple[dict[str, object], ...]:
    from control_plane.storage.postgres import (
        LaunchplaneIdempotencyRow,
        LaunchplaneOdooProdPromotionOperationRow,
        LaunchplaneOdooProdRollbackOperationRow,
        LaunchplaneOdooStableTargetReplacementOperationRow,
        LaunchplaneVeriReelProdBackupGateOperationRow,
    )

    observations: list[dict[str, object]] = []
    for row_type in (
        LaunchplaneOdooProdPromotionOperationRow,
        LaunchplaneOdooProdRollbackOperationRow,
        LaunchplaneOdooStableTargetReplacementOperationRow,
        LaunchplaneVeriReelProdBackupGateOperationRow,
    ):
        for row in session.scalars(
            select(row_type).where(row_type.status.in_(("running", "reconciliation_required")))
        ):
            observations.append(
                {
                    "operation_id": row.operation_id,
                    "kind": row_type.__tablename__,
                    "state": row.status,
                    "product": row.payload.get("product", ""),
                    "context": row.payload.get("context", ""),
                    "instance": row.payload.get("instance", ""),
                    "lease_owner": row.payload.get("lease_owner", ""),
                    "lease_expires_at": row.payload.get("lease_expires_at", ""),
                    "blocks_drain": row.status == "running",
                }
            )
    observed_at = parse_launchplane_mutation_timestamp(now, field_name="observed_at")
    from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
    from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
    from control_plane.odoo_preview_apply_http import ODOO_PREVIEW_APPLY_ROUTE
    from control_plane.odoo_preview_apply_execution import preview_execution_lock_name

    for row in session.scalars(
        select(LaunchplaneIdempotencyRow).where(
            (
                (LaunchplaneIdempotencyRow.scope == "client-release")
                | LaunchplaneIdempotencyRow.scope.startswith(f"{RECONCILE_SOURCE}:")
            ),
            LaunchplaneIdempotencyRow.state.in_(("running", "reconcile_required")),
        )
    ):
        lease_active = (
            bool(row.lease_expires_at)
            and parse_launchplane_mutation_timestamp(
                row.lease_expires_at, field_name="lease_expires_at"
            )
            > observed_at
        )
        # Lease expiry alone cannot rule out a provider call. The worker holds
        # its target execution lock through its effects. Admission
        # stays serialized until our fence commits, so a new lock owner cannot
        # start an effect between this probe and dispatch admission.
        execution_excluded = False
        execution_lock_name = ""
        if (
            not lease_active
            and row.scope.startswith(f"{RECONCILE_SOURCE}:")
            and row.provider_target_key
        ):
            if row.route_path == GENERIC_WEB_DEPLOY_ROUTE and row.state == "reconcile_required":
                execution_lock_name = testing_event_execution_lock_name(row.provider_target_key)
            elif row.route_path == ODOO_PREVIEW_APPLY_ROUTE and row.lease_expires_at:
                # Legacy preview rows may still say running. Exclusion, not
                # their persisted state or missing original intent, proves that
                # replacement will not interrupt an executor. Keep the row held.
                execution_lock_name = preview_execution_lock_name(row.provider_target_key)
        if execution_lock_name:
            with store.testing_replacement_execution_lock(execution_lock_name) as check:
                if check is not None:
                    check()
                    execution_excluded = True
        observations.append(
            {
                "operation_id": row.record_id,
                "kind": row.route_path,
                "state": row.state,
                "product": row.scope.removeprefix(f"{RECONCILE_SOURCE}:")
                if row.scope.startswith(f"{RECONCILE_SOURCE}:")
                else "",
                "lease_owner": row.lease_owner,
                "lease_expires_at": row.lease_expires_at,
                "updated_at": row.updated_at,
                "execution_excluded": execution_excluded,
                "blocks_drain": not execution_excluded,
            }
        )
    return tuple(sorted(observations, key=lambda item: cast(str, item["operation_id"])))


def _running_operations(store: Any, session: Any, now: str) -> tuple[str, ...]:
    return tuple(
        cast(str, item["operation_id"])
        for item in _operation_observations(store, session, now)
        if item["blocks_drain"]
    )


def prepare(
    store: Any,
    *,
    request_fingerprint: str,
    target_type: str,
    target_id: str,
    image_reference: str,
    deployment_marker: str,
    supersedes_deployment_marker: str = "",
) -> tuple[ServiceDeployDrainRecord, tuple[str, ...], bool]:
    """Return the durable fence, blockers, and one-time dispatch ownership.

    A repeated request after dispatch returns the saved state without dispatching
    again, including after a lost HTTP response. A different authorized self-deploy
    can replace an unsettled fence with an explicitly marker-bound repair; this
    is a new replacement intent, never a replay of the uncertain dispatch.
    """
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    with store._session_factory() as session:
        lock(store, session)
        now = store._database_mutation_timestamp(session)
        _, current = _read(session)
        prior_row = session.get(LaunchplaneServiceDeployDrainRow, f"request:{request_fingerprint}")
        prior = ServiceDeployDrainRecord.model_validate(prior_row.payload) if prior_row else None
        budget = _read_budget(session, request_fingerprint)
        if prior is not None:
            if budget is not None and budget.drain_timed_out:
                raise ServiceDeployDrainTimeout(_running_operations(store, session, now))
            if prior.state == "refused":
                raise ServiceDeployPreEffectRefused(
                    "This self-deploy was refused before effects; use a new request after resolving the refusal."
                )
            if prior.state == "dispatching":
                raise ServiceDeployOutcomeUnknown(
                    "Self-deploy dispatch requires provider reconciliation."
                )
            if prior.state != "draining":
                return prior, (), False
        if not deployment_marker or deployment_marker == os.environ.get(
            "LAUNCHPLANE_DEPLOYMENT_MARKER"
        ):
            raise ValueError(
                "Self-deploy requires a fresh deployment marker before draining releases."
            )
        if session.scalar(
            select(LaunchplaneServiceDeployDrainRow.record_id)
            .where(
                LaunchplaneServiceDeployDrainRow.payload["deployment_marker"].as_string()
                == deployment_marker,
                LaunchplaneServiceDeployDrainRow.payload["request_fingerprint"].as_string()
                != request_fingerprint,
            )
            .limit(1)
        ):
            raise ValueError(
                "Self-deploy requires an unused deployment marker for each new request."
            )
        repair_matches = current is not None and (
            supersedes_deployment_marker == current.deployment_marker
            and target_type == current.target_type
            and target_id == current.target_id
        )
        if supersedes_deployment_marker and not repair_matches:
            raise ServiceDeployFenceConflict(
                "The superseded self-deploy fence does not match.", fence=current
            )
        if (
            current is not None
            and current.state in {"dispatching", "requested"}
            and not repair_matches
        ):
            raise ServiceDeployFenceConflict(
                "An earlier self-deploy requires matching startup or an explicit "
                "marker-bound service repair.",
                fence=current,
            )
        if (
            current is not None
            and current.request_fingerprint != request_fingerprint
            and current.state == "draining"
            and current.expires_at > now
        ):
            raise ValueError("Another self-deploy is draining release operations.")
        blockers = _running_operations(store, session, now)
        if budget is None:
            drain_started_at = prior.updated_at if prior else now
            budget = ServiceDeployDrainBudget(
                drain_started_at=drain_started_at,
                drain_deadline_at=(
                    datetime.fromisoformat(drain_started_at.replace("Z", "+00:00"))
                    + timedelta(seconds=SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS)
                )
                .isoformat()
                .replace("+00:00", "Z"),
            )
        if prior is not None and parse_launchplane_mutation_timestamp(
            now, field_name="observed_at"
        ) >= parse_launchplane_mutation_timestamp(
            budget.drain_deadline_at, field_name="drain_deadline_at"
        ):
            # A timed-out request must never later dispatch on a poll. Expire only
            # this pre-effect admission pause; all product recovery fences survive.
            _write(
                session,
                prior.model_copy(
                    update={
                        "expires_at": now,
                        "updated_at": now,
                    }
                ),
            )
            _write_budget(
                session, request_fingerprint, budget.model_copy(update={"drain_timed_out": True})
            )
            session.commit()
            raise ServiceDeployDrainTimeout(blockers)
        state: Literal["draining", "dispatching"] = "draining" if blockers else "dispatching"
        record = ServiceDeployDrainRecord(
            request_fingerprint=request_fingerprint,
            target_type=target_type,
            target_id=target_id,
            image_reference=image_reference,
            deployment_marker=deployment_marker,
            previous_request_fingerprint=(
                current.previous_request_fingerprint
                if current.request_fingerprint == request_fingerprint
                else current.request_fingerprint
            )
            if current
            else "",
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
        _write(session, record)
        _write_budget(session, request_fingerprint, budget)
        session.commit()
        return record, blockers, not blockers


def record_dispatch(store: Any, request_fingerprint: str) -> None:
    with store._session_factory() as session:
        lock(store, session)
        _, record = _read(session)
        if record is None or record.request_fingerprint != request_fingerprint:
            raise ServiceDeployOutcomeUnknown("Self-deploy fence changed during provider dispatch.")
        if record.state == "dispatching":
            _write(session, record.model_copy(update={"state": "requested"}))
            session.commit()


def record_pre_effect_refusal(store: Any, request_fingerprint: str) -> None:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow

    with store._session_factory() as session:
        lock(store, session)
        _, record = _read(session)
        if (
            record is None
            or record.request_fingerprint != request_fingerprint
            or record.state != "dispatching"
        ):
            raise ServiceDeployOutcomeUnknown(
                "The service fence changed before recording provider refusal."
            )
        previous_row = (
            session.get(
                LaunchplaneServiceDeployDrainRow, f"request:{record.previous_request_fingerprint}"
            )
            if record.previous_request_fingerprint
            else None
        )
        if record.previous_request_fingerprint and previous_row is None:
            raise ServiceDeployOutcomeUnknown("The previous service fence receipt is unavailable.")
        previous_payload = previous_row.payload if previous_row else None
        _write(
            session,
            record.model_copy(
                update={
                    "state": "refused",
                    "updated_at": store._database_mutation_timestamp(session),
                }
            ),
        )
        row, _ = _read(session)
        if previous_payload is not None:
            row.payload = previous_payload
        else:
            session.delete(row)
        session.commit()


def confirm_startup(store: Any) -> None:
    """Only matching API process startup releases admission on that worker image."""
    with store._session_factory() as session:
        lock(store, session)
        _, record = _read(session)
        if record is None or record.state not in {"dispatching", "requested"}:
            return
        if (
            record.image_reference != os.environ.get("DOCKER_IMAGE_REFERENCE", "").strip()
            or record.deployment_marker
            != os.environ.get("LAUNCHPLANE_DEPLOYMENT_MARKER", "").strip()
        ):
            return
        _write(
            session,
            record.model_copy(
                update={
                    "state": "confirmed",
                    "updated_at": store._database_mutation_timestamp(session),
                }
            ),
        )
        session.commit()


def read_status(store: Any) -> dict[str, object]:
    with store._session_factory() as session:
        _, record = _read(session)
        if record is None:
            return {"state": "idle", "running_operation_ids": (), "admission_paused": False}
        result = record.model_dump()
        budget = _read_budget(session, record.request_fingerprint)
        if budget is not None:
            result.update(budget.model_dump())
        now = store._database_mutation_timestamp(session)
        observations = _operation_observations(store, session, now)
        result["running_operation_ids"] = tuple(
            item["operation_id"] for item in observations if item["blocks_drain"]
        )
        result["operation_details"] = observations[:100]
        result["operation_detail_count"] = len(observations)
        result["operation_details_truncated"] = len(observations) > 100
        result["drain_timeout_seconds"] = SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS
        result["admission_paused"] = not admission_allowed(session, now)
        return result
