"""Reviewed reconciliation resumes only an existing accepted-release step key."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE
from control_plane.workflows.ship import utc_now_timestamp
from control_plane import client_release as releases
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody
from control_plane.storage.postgres import PostgresRecordStore

RELEASE_RECOVERY_ROUTE = "/v1/products/{product}/production-recipient/release-recovery"


class RecipientReleaseRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_record_id: str = Field(min_length=1)
    kind: Literal["promote", "rollback"]
    attempt: Literal[1, 2] = 1
    reason: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientReleaseRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    plan_sha256: str
    original_key: str
    operation_id: str
    state: str


def recover_release_step(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: RecipientReleaseRecoveryRequest,
    original_key: str,
) -> RecipientReleaseRecoveryPlan:
    record = custody(store, product, request.context)
    covered = releases._covering_decision(
        store, product, decision_record_id=request.decision_record_id
    )
    if record is None or record.plan_sha256 != request.preparation_plan_sha256 or covered is None:
        raise ValueError("Retain the original recipient and current Client release decision.")
    profile, decision = covered
    if profile.driver_id != "generic-web" or releases._prod_context(profile) != request.context:
        raise ValueError("The release belongs to another product lane.")
    step = releases.ClientReleaseStep(request.kind, request.attempt)
    if step not in releases.client_release_steps(decision.release_start):
        raise ValueError("The Client decision did not admit this release step.")
    key = releases._step_key(decision, step)
    route = (
        GENERIC_WEB_PROD_PROMOTION_ROUTE
        if request.kind == "promote"
        else GENERIC_WEB_ROLLBACK_ROUTE
    )
    reservation = store.read_idempotency_record(
        scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE, route_path=route, idempotency_key=key
    )
    if reservation is None or original_key != key:
        raise ValueError("Only the existing original release operation can be reconciled.")
    digest = canonical_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "custody": record.model_dump(mode="json"),
            "reservation": reservation.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }
    )
    if request.mode == "apply":
        if request.reviewed_plan_sha256 != digest:
            raise ValueError("Review the exact current stopped operation before continuation.")
        if request.kind == "promote":
            releases._run_generic_web_promotion(
                store,
                root,
                profile,
                decision,
                step,
                releases._backup_record_id(request.context, decision, request.attempt),
            )
        else:
            releases._queue_recipient_inverse(
                store,
                root,
                profile,
                decision,
                step,
                request.context,
                utc_now_timestamp(),
            )
        reservation = store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE, route_path=route, idempotency_key=key
        )
        assert reservation is not None
    elif request.mode == "readback" and reservation.state != "completed":
        raise ValueError("The original operation has no terminal receipt yet.")
    return RecipientReleaseRecoveryPlan(
        product=product,
        context=request.context,
        plan_sha256=digest,
        original_key=key,
        operation_id=reservation.record_id,
        state=reservation.state,
    )
