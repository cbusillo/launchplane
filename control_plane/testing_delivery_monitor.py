"""Ordinary testing delivery attention, independent of the runtime still serving."""

from collections.abc import Callable
import logging
import re

from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.product_reconcile import (
    ProductReconcileRequestRecord,
    product_reconcile_target_key,
)
from control_plane.production_delivery_monitor import _candidate_key, _observe, _time
from control_plane.product_reconcile import _testing_failure_reason
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

TESTING_DELIVERY_CHECK_PREFIX = "testing-delivery-"
_LOGGER = logging.getLogger(__name__)


def monitor_testing_delivery_once(
    store: PostgresRecordStore,
    *,
    observed_at: str = "",
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Read one coalesced testing request per active product, never preview history."""
    for profile in store.list_product_profile_records():
        if should_stop():
            return
        if not profile.is_active:
            continue
        try:
            request = store.read_product_reconcile_request(
                product_reconcile_target_key(
                    product=profile.product, target_kind="testing", pull_request_number=None
                )
            )
        except FileNotFoundError:
            continue
        except Exception:
            _LOGGER.exception("Could not read testing delivery evidence for %s", profile.product)
            continue
        record_testing_delivery_attention(store, request, observed_at=observed_at)


def record_testing_delivery_attention(
    store: PostgresRecordStore,
    request: ProductReconcileRequestRecord,
    *,
    observed_at: str = "",
) -> None:
    """Persist diagnostics/outbox only; notification trouble cannot stop reconciliation."""
    if request.target_kind != "testing":
        return
    try:
        profile = store.read_product_profile_record(request.product)
        lane = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
        if not profile.is_active or lane is None:
            return
        _record_attention(store, profile, lane, request, observed_at or utc_now_timestamp())
    except Exception:
        # The delivery scheduler retries the durable request independently of deploys.
        _LOGGER.exception("Could not record testing delivery attention for %s", request.product)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _record_attention(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    request: ProductReconcileRequestRecord,
    now: str,
) -> None:
    plan = request.last_plan
    if plan.get("context") not in {None, lane.context}:
        return
    candidate = _text(plan.get("desired_artifact_id")) or _text(plan.get("desired_commit"))
    check_name = TESTING_DELIVERY_CHECK_PREFIX + _candidate_key(candidate or request.target_key)
    reason = _text(plan.get("last_failed_error_code"))
    trace = _text(plan.get("failure_trace_id"))
    failed_at = request.updated_at
    failed = request.state == "failed" or bool(request.last_error)
    for key in ("queued_operation_id", "last_failed_operation_id"):
        operation_id = _text(plan.get(key))
        if not operation_id:
            continue
        operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
        if (
            operation.product != profile.product
            or operation.context != lane.context
            or operation.instance != "testing"
            or (candidate and operation.request.artifact_id != candidate)
        ):
            continue
        if operation.status not in {"fail", "reconciliation_required"}:
            continue
        failed = True
        reason, _summary = _testing_failure_reason(operation)
        trace = operation.runner_trace_id or operation.operation_id
        failed_at = operation.finished_at or operation.updated_at
        break
    # A hold or a known protective pre-effect refusal is not a failed service.
    if plan.get("held") is True or reason == "deploy_blocked.retained_database_credential_changed":
        return
    if not reason:
        reason = (
            "post_deploy_failed"
            if plan.get("post_deploy_status") == "fail"
            else "deploy_failed"
            if plan.get("deploy_status") == "fail"
            else "testing_qualification_failed"
            if plan.get("reason") == "testing_qualification_failed"
            else "testing_reconcile_failed"
        )
    safe_reason = (
        reason if re.fullmatch(r"[a-z][a-z0-9_.]{0,149}", reason) else "testing_reconcile_failed"
    )
    safe_trace = trace if re.fullmatch(r"[A-Za-z0-9_:\-.]{1,200}", trace) else "unavailable"
    # Query only the intended candidate on this testing lane. Health, production,
    # unrelated successful builds and an older success cannot claim recovery.
    deployments = (
        store.list_deployment_records(
            context_name=lane.context,
            instance_name="testing",
            artifact_id=candidate,
            limit=1,
        )
        if candidate
        else ()
    )
    successful = next((record for record in deployments if deployment_record_passed(record)), None)
    existing = store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name="testing",
        status="open",
    )
    recovered = False
    for incident in existing:
        if incident.check_name not in {
            check_name,
            TESTING_DELIVERY_CHECK_PREFIX + _candidate_key(request.target_key),
        }:
            continue
        cutoff = (
            max(_time(incident.opened_at), _time(failed_at))
            if failed
            else _time(incident.opened_at)
        )
        if (
            successful
            and successful.deploy.finished_at
            and _time(successful.deploy.finished_at) > cutoff
        ):
            _observe(
                store,
                profile,
                lane,
                check_name=incident.check_name,
                failed=False,
                reason=safe_reason,
                summary="The matching testing delivery succeeded.",
                observed_at=now,
            )
            recovered = recovered or incident.check_name == check_name
    if recovered or not failed:
        return
    if (
        successful
        and successful.deploy.finished_at
        and _time(successful.deploy.finished_at) > _time(failed_at)
    ):
        return
    _observe(
        store,
        profile,
        lane,
        check_name=check_name,
        failed=True,
        reason=safe_reason,
        summary=(
            f"Testing delivery failed at {safe_reason}; trace: {safe_trace}. "
            "The previous runtime may still be healthy. Inspect the testing path-check "
            "and reconcile request; use the supported testing retry after fixing the failed step."
        ),
        observed_at=now,
    )
