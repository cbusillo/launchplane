"""Delivery attention is independent of the health of the artifact still running."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
import logging
import re
from urllib.parse import quote

from starlette.requests import Request

from control_plane.build_provenance import BuildProvenanceTransport
from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_profile_record_sha256,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    build_public_ingress_observation_id,
)
from control_plane.product_reconcile import resolve_build_provenance_transport
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.public_ingress_monitor import _store_monitor_observation
from control_plane.workflows.ship import utc_now_timestamp

Store = PostgresRecordStore | FilesystemRecordStore
PRODUCTION_DELIVERY_LAG_SECONDS = 3600
DELIVERY_CHECK_PREFIX = "production-delivery-"
DELIVERY_LAG_CHECK = DELIVERY_CHECK_PREFIX + "lag"
_SCOPE = "launchplane:production-delivery-monitor"
_ROUTE = "service:production-delivery-monitor"
_LOGGER = logging.getLogger(__name__)


def _candidate_key(candidate: str) -> str:
    return hashlib.sha256(candidate.strip().encode()).hexdigest()[:24]


def _observe(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    check_name: str,
    failed: bool,
    summary: str,
    observed_at: str,
) -> None:
    observation = PublicIngressObservationRecord(
        record_id=build_public_ingress_observation_id(
            product=profile.product,
            context=lane.context,
            instance=lane.instance,
            check_name=check_name,
            observed_at=observed_at,
        ),
        product=profile.product,
        repository=profile.repository,
        driver_id=profile.driver_id,
        context=lane.context,
        instance=lane.instance,
        check_name=check_name,
        check_kind="provider",
        monitoring_intent=lane.health_monitoring.monitoring_intent,
        observed_at=observed_at,
        status="fail" if failed else "pass",
        failure_code="production_delivery_failed" if failed else None,
        summary=summary,
        targets=(
            PublicIngressTargetObservation(
                target="provider",
                url="provider://launchplane/production-delivery",
                status="fail" if failed else "pass",
                failure_code="production_delivery_failed" if failed else None,
                summary=summary,
            ),
        ),
    )
    # On the service store, observation, incident and notification outbox commit together.
    _store_monitor_observation(
        record_store=store,
        record=observation,
        incident_eligible=True,
        expected_profile_sha256=product_profile_record_sha256(profile),
        recovery_observation_threshold=1,
        notify=True,
        notification_drivers=None,
    )


def record_production_delivery_failure(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    candidate: str,
    reason: str,
    trace_id: str,
    observed_at: str = "",
) -> None:
    if lane.instance != "prod" or not profile.is_active:
        return
    check_name = DELIVERY_CHECK_PREFIX + _candidate_key(candidate)
    # Retain the original actionable trace. Replays must not produce material-change spam.
    if store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        check_name=check_name,
        status="open",
    ):
        return
    safe_reason = reason if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) else "delivery_failed"
    safe_trace = trace_id if re.fullmatch(r"[A-Za-z0-9_:\-.]{1,200}", trace_id) else "unavailable"
    _observe(
        store,
        profile,
        lane,
        check_name=check_name,
        failed=True,
        summary=f"Production delivery blocked: {safe_reason}; trace: {safe_trace}. "
        "The previous runtime may still be healthy. Inspect the supported release path.",
        observed_at=observed_at or utc_now_timestamp(),
    )


async def record_rejected_production_request(
    request: Request,
    store: object,
    *,
    reason: str,
    trace_id: str,
) -> None:
    """Use only typed delivery routes; never persist raw request or exception text."""
    path = request.url.path
    if request.method != "POST" or reason == "authentication_required":
        return
    if not (
        (
            path.startswith("/v1/drivers/")
            and path.rsplit("/", 1)[-1]
            in {
                "deploy",
                "prod-promotion",
                "prod-promotion-run",
                "prod-promotion-inputs",
                "target-replacement-apply",
            }
        )
        or path in {"/v1/odoo-prod-promotions", "/v1/odoo-prod-rollbacks"}
        or path.endswith("/environments/prod/promotion/workflow-dispatch")
    ) or not isinstance(store, (PostgresRecordStore, FilesystemRecordStore)):
        return
    payload = await request.json()
    if not isinstance(payload, dict):
        return
    product = payload.get("product") or request.path_params.get("product")
    if not isinstance(product, str):
        return
    profile = store.read_product_profile_record(product)
    details = next(
        (
            payload[key]
            for key in ("deploy", "promotion", "inputs", "replacement")
            if isinstance(payload.get(key), dict)
        ),
        payload,
    )
    instance = (
        details.get("instance") or details.get("to_instance") or ("prod" if "prod" in path else "")
    )
    lane = next((lane for lane in profile.lanes if lane.instance == instance == "prod"), None)
    if lane is None:
        return
    candidate = details.get("artifact_id") or details.get("source_git_ref") or "unspecified"
    if not isinstance(candidate, str):
        return
    if path == "/v1/drivers/generic-web/deploy" and details.get("artifact_id"):
        from control_plane.workflows.generic_web_deploy import normalize_generic_web_artifact_id

        candidate = normalize_generic_web_artifact_id(profile=profile, artifact_id=candidate)
    record_production_delivery_failure(
        store,
        profile,
        lane,
        candidate=candidate,
        reason=reason,
        trace_id=trace_id,
    )


def monitor_production_delivery_once(
    store: Store,
    *,
    observed_at: str = "",
    transport_factory: Callable[
        [object, LaunchplaneProductProfileRecord], BuildProvenanceTransport
    ] = resolve_build_provenance_transport,
    max_age_seconds: int = PRODUCTION_DELIVERY_LAG_SECONDS,
) -> None:
    now = observed_at or utc_now_timestamp()
    for profile in store.list_product_profile_records():
        if not profile.is_active:
            continue
        for lane in profile.lanes:
            if lane.instance != "prod":
                continue
            try:
                _monitor_lane(store, profile, lane, now, transport_factory, max_age_seconds)
            except Exception:
                # A failed source read is unknown, never recovery; other products still run.
                _LOGGER.exception("Production delivery monitor failed for %s", profile.product)


def _monitor_lane(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: str,
    transport_factory: Callable[
        [object, LaunchplaneProductProfileRecord], BuildProvenanceTransport
    ],
    max_age_seconds: int,
) -> None:
    deployments = store.list_deployment_records(context_name=lane.context, instance_name="prod")
    latest = deployments[0] if deployments else None
    scan_key = "failures:" + profile.product
    scan = store.read_idempotency_record(scope=_SCOPE, route_path=_ROUTE, idempotency_key=scan_key)
    cutoff = scan.recorded_at if scan else ""
    promotions = store.list_promotion_records(context_name=lane.context, to_instance_name="prod")
    for records in (deployments, promotions):
        for record in records if cutoff else records[:1]:
            finished = record.deploy.finished_at or record.deploy.started_at
            if cutoff and (not finished or _time(finished) <= _time(cutoff)):
                continue
            if record.failure is not None or any(
                status == "fail"
                for status in (
                    record.deploy.status,
                    record.post_deploy_update.status,
                    record.destination_health.status,
                )
            ):
                record_production_delivery_failure(
                    store,
                    profile,
                    lane,
                    candidate=record.artifact_identity.artifact_id
                    if record.artifact_identity
                    else record.source_git_ref
                    if isinstance(record, DeploymentRecord)
                    else record.record_id,
                    reason=record.failure.code if record.failure else "deployment_failed",
                    trace_id=record.record_id,
                    observed_at=finished or now,
                )
    operations = (
        store.list_odoo_prod_promotion_operation_records(
            product=profile.product,
            context_name=lane.context,
            instance_name="prod",
        )
        if isinstance(store, PostgresRecordStore)
        else ()
    )
    for operation in operations if cutoff else operations[:1]:
        if operation.status in {"fail", "cancelled"} and (
            not cutoff or _time(operation.updated_at) > _time(cutoff)
        ):
            record_production_delivery_failure(
                store,
                profile,
                lane,
                candidate=operation.result.artifact_id
                if operation.result and operation.result.artifact_id
                else operation.operation_id,
                reason=operation.error_code or "promotion_failed",
                trace_id=operation.runner_trace_id or operation.operation_id,
                observed_at=operation.finished_at or operation.updated_at,
            )
    if isinstance(store, PostgresRecordStore):
        from control_plane.client_release import read_client_release_step_views

        for decision in store.list_release_review_decision_records(
            product=profile.product, limit=1
        ):
            for step in read_client_release_step_views(
                store=store, profile=profile, decision=decision
            ):
                failure = step.failure
                if failure is None or (
                    cutoff and failure.recorded_at and _time(failure.recorded_at) <= _time(cutoff)
                ):
                    continue
                record_production_delivery_failure(
                    store,
                    profile,
                    lane,
                    candidate=decision.checklist.candidate.artifact_id,
                    reason=failure.code,
                    trace_id=failure.trace_id or failure.record_id,
                    observed_at=failure.recorded_at or now,
                )
    _write_receipt(store, key=scan_key, now=now, payload={})
    if latest is not None and deployment_record_passed(latest):
        keys = {_candidate_key(latest.source_git_ref)}
        if latest.artifact_identity is not None:
            keys.add(_candidate_key(latest.artifact_identity.artifact_id))
        for incident in store.list_public_ingress_incident_records(
            product=profile.product,
            context_name=lane.context,
            instance_name="prod",
            status="open",
        ):
            if (
                incident.check_name in {DELIVERY_CHECK_PREFIX + key for key in keys}
                and latest.deploy.finished_at
                and _time(latest.deploy.finished_at) > _time(incident.opened_at)
            ):
                _observe(
                    store,
                    profile,
                    lane,
                    check_name=incident.check_name,
                    failed=False,
                    summary="The matching production delivery succeeded.",
                    observed_at=now,
                )

    transport = transport_factory(store, profile)
    repository_path = "/repos/" + quote(profile.repository, safe="/")
    source = transport.get_json(
        repository_path + "/commits/" + quote(profile.default_branch, safe="")
    )
    if not isinstance(source, dict) or not re.fullmatch(
        r"[0-9a-f]{40}", str(source.get("sha", ""))
    ):
        raise ValueError("Source head is unavailable")
    head = str(source["sha"])
    current = (
        latest.source_git_ref if latest is not None and deployment_record_passed(latest) else ""
    )
    if not current:
        try:
            inventory = store.read_environment_inventory(
                context_name=lane.context, instance_name="prod"
            )
        except FileNotFoundError:
            pass
        else:
            if inventory.runtime_identity is not None:
                current = inventory.runtime_identity.source_git_ref
    receipt = store.read_idempotency_record(
        scope=_SCOPE, route_path=_ROUTE, idempotency_key=profile.product
    )
    pending = str(receipt.response_payload.get("pending_commit", "")) if receipt else ""
    since = str(receipt.response_payload.get("pending_since", "")) if receipt else ""
    if pending and current and pending != current:
        comparison = transport.get_json(
            repository_path + f"/compare/{pending}...{quote(current, safe='')}"
        )
        if isinstance(comparison, dict) and comparison.get("status") in {"ahead", "identical"}:
            pending, since = "", ""
    if current == head:
        pending, since = "", ""
    elif not pending or pending == current:
        pending, since = head, now
    _write_receipt(
        store,
        key=profile.product,
        now=now,
        payload={"pending_commit": pending, "pending_since": since},
    )
    overdue = bool(pending and _time(now) >= _time(since) + timedelta(seconds=max_age_seconds))
    open_lag = store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name="prod",
        check_name=DELIVERY_LAG_CHECK,
        status="open",
    )
    if overdue or (not pending and open_lag):
        _observe(
            store,
            profile,
            lane,
            check_name=DELIVERY_LAG_CHECK,
            failed=overdue,
            summary=(
                f"Merged default branch has not reached production within {max_age_seconds} "
                f"seconds; source: {pending}; trace: production-delivery-monitor:"
                f"{_candidate_key(profile.product)}. Check build, testing, acceptance, "
                "backup and promotion evidence."
                if overdue
                else "Production has reached the observed default-branch source."
            ),
            observed_at=now,
        )


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write_receipt(store: Store, *, key: str, now: str, payload: dict[str, str]) -> None:
    store.write_idempotency_record(
        LaunchplaneIdempotencyRecord(
            record_id="production-delivery-" + _candidate_key(key),
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=key,
            request_fingerprint="production-delivery-monitor-v1",
            response_status_code=200,
            response_trace_id="production-delivery-monitor:" + _candidate_key(key),
            recorded_at=now,
            response_payload=payload,
        )
    )
