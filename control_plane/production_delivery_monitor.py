"""Delivery attention is independent of the health of the artifact still running."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import logging
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from control_plane.build_provenance import BuildProvenanceTransport
from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_profile_record_sha256,
    product_lane_monitoring_probe_effective,
)
from control_plane.contracts.public_ingress_monitoring import (
    RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
    PRODUCTION_REQUEST_REFUSED,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    PublicIngressIncidentRecord,
    public_ingress_incident_record_sha256,
    build_public_ingress_observation_id,
)
from control_plane.product_reconcile import resolve_build_provenance_transport
from control_plane.release_review import ProductionChangeRequiresPromotion
from control_plane.lane_runtime_verification import (
    monitor_observation_provenance,
    read_lane_runtime_verification,
)
from control_plane.source_control_change import read_branch_revision
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.delivery_evidence import DeliveryEvidenceIndex, DeliveryEvidenceWork
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.public_ingress_monitor import _store_monitor_observation
from control_plane.workflows.ship import utc_now_timestamp

Store = PostgresRecordStore | FilesystemRecordStore
DELIVERY_CHECK_PREFIX = "production-delivery-"
REFUSED_REQUEST_CHECK_PREFIX = "production-request-refused-"
DELIVERY_EVIDENCE_PAGE_SIZE = 100
_SCOPE = "launchplane:production-delivery-monitor"
_ROUTE = "service:production-delivery-monitor"
_LOGGER = logging.getLogger(__name__)
_NONTERMINAL_CODES = frozenset(
    {
        "mutation_in_progress",
        "lane_busy",
        "promotion_already_active",
        "rollback_already_active",
        "odoo_stable_lane_operation_active",
        "idempotency_key_required",
        "missing_idempotency_key",
        "idempotency_key_reused",
    }
)


class RefusedProductionRequestReconciliation(BaseModel):
    """An admin attests caller retirement; the service verifies refusal and health."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    incident_id: str = Field(min_length=1)
    caller_retired: Literal[True]
    caller_retired_at: AwareDatetime
    caller_retirement_evidence: str = Field(min_length=1, max_length=2000)


def is_guard_refusal_incident(incident: PublicIngressIncidentRecord) -> bool:
    return bool(
        incident.instance == "prod"
        and incident.check_name.startswith(DELIVERY_CHECK_PREFIX)
        and incident.material_fingerprint
        and incident.material_fingerprint.route_authority.provider_check
        == "production-delivery/" + ProductionChangeRequiresPromotion.code
    )


def reconcile_refused_production_requests(
    store: Store,
    *,
    requests: tuple[RefusedProductionRequestReconciliation, ...],
    observed_at: str,
) -> tuple[str, ...]:
    """One-time supported disposition; never infer caller retirement from runtime health."""
    now = _time(observed_at)
    resolved: list[str] = []
    latest_refusals: dict[tuple[str, str], datetime | None] = {}
    for request in requests:
        incident = store.read_public_ingress_incident_record(request.incident_id)
        with (
            store.delivery_evidence_index() as index,
            index.lane_lock(incident.context, incident.product) as acquired,
        ):
            if not acquired:
                raise ValueError(
                    "Delivery monitoring is active; retry reconciliation after it finishes."
                )
            lane_key = (incident.product, incident.context)
            if lane_key not in latest_refusals:
                latest_refusals[lane_key] = next(
                    (
                        _time(record.observed_at)
                        for record in store.list_public_ingress_observation_records(
                            product=incident.product,
                            context_name=incident.context,
                            instance_name="prod",
                            check_kind="provider",
                        )
                        if record.check_name.startswith(REFUSED_REQUEST_CHECK_PREFIX)
                    ),
                    None,
                )
            resolved.append(
                _reconcile_refused_production_request(
                    store,
                    request=request,
                    now=now,
                    latest_refusal_at=latest_refusals[lane_key],
                )
            )
    return tuple(resolved)


def _reconcile_refused_production_request(
    store: Store,
    *,
    request: RefusedProductionRequestReconciliation,
    now: datetime,
    latest_refusal_at: datetime | None,
) -> str:
    incident = store.read_public_ingress_incident_record(request.incident_id)
    observed_at = now.isoformat()
    if not is_guard_refusal_incident(incident):
        raise ValueError("Only a promotion guard refusal can be reconciled as refused.")
    if incident.status == "resolved":
        if incident.resolution_reason == PRODUCTION_REQUEST_REFUSED:
            return incident.incident_id
        raise ValueError(
            "This incident already resolved by another outcome; retain that disposition."
        )
    if not _time(incident.opened_at) <= request.caller_retired_at <= now:
        raise ValueError("Caller retirement must follow the refused request and precede now.")
    profile = store.read_product_profile_record(incident.product)
    lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
    if lane is None or not profile.is_active or lane.context != incident.context:
        raise ValueError("The production lane has changed; inspect its current evidence.")
    history = store.list_public_ingress_observation_records(incident_id=incident.incident_id)
    if not history or any(
        observation.status != "fail"
        or not observation.targets
        or any(
            target.url != _delivery_target_url(ProductionChangeRequiresPromotion.code)
            for target in observation.targets
        )
        for observation in history
    ):
        raise ValueError("Incident history includes a failure or uncertain outcome beyond refusal.")
    if latest_refusal_at is not None and latest_refusal_at >= request.caller_retired_at:
        raise ValueError("A production caller is still making refused requests.")
    checks = tuple(
        check
        for check in lane.health_monitoring.checks
        if check.enabled
        and product_lane_monitoring_probe_effective(
            monitoring_intent=lane.health_monitoring.monitoring_intent,
            check_kind=check.kind,
        )
    )
    if not checks:
        raise ValueError("Production has no effective health checks.")
    for check in checks:
        observations = store.list_public_ingress_observation_records(
            product=profile.product,
            context_name=lane.context,
            instance_name="prod",
            check_name=check.name,
            check_kind=check.kind,
            limit=1,
        )
        if (
            not observations
            or observations[0].status != "pass"
            or _time(observations[0].observed_at) < request.caller_retired_at
            or monitor_observation_provenance(observations[0], now=now).freshness_status
            != "verified"
        ):
            raise ValueError("Production health must pass freshly after caller retirement.")
    verification = read_lane_runtime_verification(
        record_store=store,
        profile=profile,
        lane=lane,
        summary=LaunchplaneLaneSummary(
            context=lane.context,
            instance="prod",
            inventory=store.read_environment_inventory(
                context_name=lane.context, instance_name="prod"
            ),
        ),
        now=now,
    )
    if verification.provenance.freshness_status != "verified":
        raise ValueError("Current production runtime identity is not verified.")
    _observe(
        store,
        profile,
        lane,
        check_name=incident.check_name,
        failed=False,
        retired=True,
        observed_at=observed_at,
        summary="Production request refused, nothing changed. Caller retired at "
        f"{request.caller_retired_at.isoformat()}; evidence: {request.caller_retirement_evidence}. "
        "Production health and runtime identity remain verified.",
        expected_incident_sha256=public_ingress_incident_record_sha256(incident),
    )
    return incident.incident_id


def _candidate_key(candidate: str) -> str:
    return hashlib.sha256(candidate.strip().encode()).hexdigest()[:24]


def _delivery_target_url(reason: str) -> str:
    return "provider://launchplane/production-delivery/" + reason


def _observe(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    check_name: str,
    failed: bool,
    summary: str,
    observed_at: str,
    reason: str = "delivery_failed",
    retired: bool = False,
    refused: bool = False,
    expected_incident_sha256: str = "",
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
                url=_delivery_target_url(reason),
                status="fail" if failed else "pass",
                failure_code="production_delivery_failed" if failed else None,
                summary=summary,
            ),
        ),
    )
    if refused:
        observation = observation.model_copy(
            update={
                "status": "skipped",
                "failure_code": None,
                "targets": tuple(
                    target.model_copy(update={"status": "skipped", "failure_code": None})
                    for target in observation.targets
                ),
            }
        )
    if retired:
        observation = observation.model_copy(
            update={
                "status": "skipped",
                "failure_code": "monitoring_intent_changed",
                "purpose": "reconciliation",
                "targets": (
                    PublicIngressTargetObservation(
                        target="monitoring_intent",
                        url="monitoring-intent://production-delivery",
                        status="skipped",
                        failure_code="monitoring_intent_changed",
                        summary=summary,
                    ),
                ),
            }
        )
    # On the service store, observation, incident and notification outbox commit together.
    result = _store_monitor_observation(
        record_store=store,
        record=observation,
        incident_eligible=not (retired or refused),
        expected_profile_sha256=product_profile_record_sha256(profile),
        recovery_observation_threshold=1,
        notify=not (
            refused
            or (
                retired
                and (
                    check_name == RETIRED_PRODUCTION_DELIVERY_LAG_CHECK or expected_incident_sha256
                )
            )
        ),
        notification_drivers=None,
        expected_incident_sha256=expected_incident_sha256,
        reconciliation_reason=PRODUCTION_REQUEST_REFUSED
        if expected_incident_sha256
        else "monitoring_intent_changed",
    )
    if result.authority_changed:
        raise RuntimeError("Production delivery alert authority changed during persistence")


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
    safe_reason = reason if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) else "delivery_failed"
    existing = store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        check_name=check_name,
        status="open",
    )
    # New stages update the same incident; exact retries retain their original trace.
    if (
        existing
        and existing[0].material_fingerprint is not None
        and (
            existing[0].material_fingerprint.route_authority.provider_check
            == "production-delivery/" + safe_reason
        )
    ):
        return
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
        reason=safe_reason,
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
    # This diagnostic scope is set only after the route's existing authorization check.
    # It supplies no authorization and is never accepted from a request body/header.
    authorized_scope = getattr(request.state, "production_delivery_scope", None)
    if request.method != "POST" or authorized_scope is None or reason in _NONTERMINAL_CODES:
        return
    if not isinstance(store, (PostgresRecordStore, FilesystemRecordStore)):
        return
    try:
        payload = await request.json()
    except RuntimeError:
        # A propagated driver crash is observed by outer middleware after body consumption.
        payload = {"product": authorized_scope[0]}
    if not isinstance(payload, dict):
        return
    product = payload.get("product") or request.path_params.get("product")
    if not isinstance(product, str):
        return
    details = next(
        (
            payload[key]
            for key in ("deploy", "promotion", "run", "replacement", "rollback", "workflow")
            if isinstance(payload.get(key), dict)
        ),
        payload,
    )
    if details.get("dry_run") is True:
        return
    instance = details.get("instance") or details.get("to_instance") or authorized_scope[2]
    if instance != "prod":
        return
    candidate = (
        details.get("artifact_id")
        or details.get("expected_artifact_id")
        or details.get("source_git_ref")
        or getattr(request.state, "production_delivery_candidate", "")
        or ""
    )
    if not isinstance(candidate, str):
        return
    await run_in_threadpool(
        _record_rejected_payload,
        store,
        product=product,
        instance=instance,
        candidate=candidate,
        authorized_scope=authorized_scope,
        path=path,
        reason=reason,
        trace_id=trace_id,
        request_key=request.headers.get("Idempotency-Key", ""),
    )


def _record_rejected_payload(
    store: Store,
    *,
    product: str,
    instance: str,
    candidate: str,
    authorized_scope: tuple[str, str, str],
    path: str,
    reason: str,
    trace_id: str,
    request_key: str,
) -> None:
    profile = store.read_product_profile_record(product)
    lane = next((lane for lane in profile.lanes if lane.instance == instance), None)
    if lane is None or authorized_scope != (profile.product, lane.context, lane.instance):
        return
    if not candidate and "rollback" not in path:
        testing = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
        if testing is not None:
            try:
                inventory = store.read_environment_inventory(
                    context_name=testing.context, instance_name="testing"
                )
            except FileNotFoundError:
                pass
            else:
                if inventory.artifact_identity is not None:
                    candidate = inventory.artifact_identity.artifact_id
                elif inventory.runtime_identity is not None:
                    candidate = inventory.runtime_identity.artifact_id
    if not candidate:
        # Unknown intent stays actionable; no later testing promotion can prove a rollback.
        candidate = "request:" + _candidate_key(request_key or trace_id) + ":" + reason
    if path in {
        "/v1/drivers/generic-web/deploy",
        "/v1/drivers/generic-web/prod-promotion",
    } and not candidate.startswith("request:"):
        from control_plane.workflows.generic_web_deploy import normalize_generic_web_artifact_id

        candidate = normalize_generic_web_artifact_id(profile=profile, artifact_id=candidate)
    if reason == ProductionChangeRequiresPromotion.code:
        _observe(
            store,
            profile,
            lane,
            check_name=REFUSED_REQUEST_CHECK_PREFIX + _candidate_key(candidate + trace_id),
            failed=False,
            refused=True,
            summary=f"Production request refused, nothing changed; promotion_required; trace: {trace_id}.",
            observed_at=utc_now_timestamp(),
            reason=reason,
        )
        return
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
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    now = observed_at or utc_now_timestamp()
    for profile in store.list_product_profile_records():
        for lane in profile.lanes:
            if should_stop():
                return
            if lane.instance != "prod":
                continue
            try:
                for incident in store.list_public_ingress_incident_records(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name="prod",
                    check_name=RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
                    status="open",
                ):
                    _observe(
                        store,
                        profile,
                        lane,
                        check_name=incident.check_name,
                        failed=False,
                        summary="Merged source waiting for release is not a production failure.",
                        observed_at=now,
                        retired=True,
                    )
            except Exception:
                # Retirement is maintenance; it must not interrupt real failure scanning.
                _LOGGER.exception("Could not retire source-age attention for %s", profile.product)
            if not profile.is_active or profile.production_use != "live":
                for incident in store.list_public_ingress_incident_records(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name="prod",
                    status="open",
                ):
                    if incident.check_name in {
                        DELIVERY_CHECK_PREFIX + _candidate_key("monitor:source"),
                        DELIVERY_CHECK_PREFIX + _candidate_key("monitor:evidence"),
                    } or (
                        not profile.is_active
                        and incident.check_name.startswith(DELIVERY_CHECK_PREFIX)
                    ):
                        _observe(
                            store,
                            profile,
                            lane,
                            check_name=incident.check_name,
                            failed=False,
                            summary="Production delivery monitoring was retired by product lifecycle configuration.",
                            observed_at=now,
                            retired=True,
                        )
                if not profile.is_active:
                    continue
            try:
                _monitor_lane(store, profile, lane, now, transport_factory)
            except Exception:
                # A failed source read is unknown, never recovery; other products still run.
                _LOGGER.exception("Production delivery monitor failed for %s", profile.product)
                _monitor_availability(
                    store, profile, lane, now, component="source", available=False
                )


def _monitor_lane(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: str,
    transport_factory: Callable[
        [object, LaunchplaneProductProfileRecord], BuildProvenanceTransport
    ],
) -> None:
    try:
        _scan_failures(store, profile, lane, now)
    except Exception:
        _LOGGER.exception("Production delivery failure scan failed for %s", profile.product)
        _monitor_availability(store, profile, lane, now, component="evidence", available=False)
    else:
        _monitor_availability(store, profile, lane, now, component="evidence", available=True)
    if profile.production_use != "live":
        return

    transport = transport_factory(store, profile)
    source = read_branch_revision(
        repository=profile.repository,
        branch=profile.default_branch,
        read=transport.get_json,
    )
    if source is None:
        raise ValueError("Source head is unavailable")
    _monitor_availability(store, profile, lane, now, component="source", available=True)


def _release_dependencies(
    profile: LaunchplaneProductProfileRecord, decision: object
) -> tuple[str, ...]:
    from control_plane.client_release import (
        CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        client_release_step_operation_id,
        client_release_steps,
    )
    from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
    from control_plane.contracts.odoo_prod_rollback_operation import (
        build_odoo_prod_rollback_operation_id,
    )

    assert isinstance(decision, ReleaseReviewDecisionRecord)
    keys = []
    for step in client_release_steps(decision.release_start):
        operation_id = client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        )
        table = (
            "launchplane_verireel_prod_backup_gate_operations"
            if step.kind == "backup"
            else "launchplane_idempotency_records"
            if profile.driver_id == "generic-web"
            else "launchplane_odoo_prod_promotion_operations"
            if step.kind == "promote"
            else "launchplane_odoo_prod_rollback_operations"
        )
        keys.append(table + ":" + operation_id)
        if step.kind == "promote" and profile.driver_id != "generic-web":
            recovery_id = build_odoo_prod_rollback_operation_id(
                product=profile.product,
                context=next(lane.context for lane in profile.lanes if lane.instance == "prod"),
                idempotency_key=operation_id + ":failure-recovery",
                idempotency_scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            )
            keys.append("launchplane_odoo_prod_rollback_operations:" + recovery_id)
    return tuple(keys)


def _work_events(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    index: DeliveryEvidenceIndex,
    work: DeliveryEvidenceWork,
    now: str,
) -> list[tuple[str, str, str, str, str]]:
    if work.kind in {"deployment", "promotion"}:
        try:
            record = (
                store.read_deployment_record(work.source_id)
                if work.kind == "deployment"
                else store.read_promotion_record(work.source_id)
            )
        except FileNotFoundError:
            if not isinstance(store, FilesystemRecordStore):
                raise
            index.forget_missing(work.key, work.version)
            return []
        if (
            not isinstance(store, PostgresRecordStore)
            or store.database_dialect_name != "postgresql"
        ):
            index.reconcile_source(work, record.model_dump(mode="json"))
        if record.failure is not None or any(
            status == "fail"
            for status in (
                record.deploy.status,
                record.post_deploy_update.status,
                record.destination_health.status,
            )
        ):
            return [
                (
                    record.record_id,
                    record.artifact_identity.artifact_id
                    if record.artifact_identity
                    else record.source_git_ref
                    if isinstance(record, DeploymentRecord)
                    else record.record_id,
                    record.failure.code if record.failure else "deployment_failed",
                    record.record_id,
                    record.deploy.finished_at or record.deploy.started_at,
                )
            ]
        return []
    if not isinstance(store, PostgresRecordStore):
        return []
    if work.kind == "decision":
        from control_plane.client_release import read_client_release_step_views

        decision = store.read_release_review_decision_record(
            product=profile.product, record_id=work.source_id
        )
        # Register even unstarted operations before reading them. A subsequent
        # worker write dirties the decision in that same source transaction.
        index.bind(decision.record_id, _release_dependencies(profile, decision), now=now)
        return [
            (
                step.operation_id,
                decision.checklist.candidate.artifact_id,
                step.failure.code,
                step.failure.trace_id or step.failure.record_id,
                step.failure.recorded_at,
            )
            for step in read_client_release_step_views(
                store=store, profile=profile, decision=decision
            )
            if step.failure is not None and step.status != "cancelled"
        ]
    operation = store.read_odoo_prod_promotion_operation_record(work.source_id)
    if index.has_release_dependency("launchplane_odoo_prod_promotion_operations:" + work.source_id):
        return []
    if operation.idempotency_scope == "client-release":
        try:
            store.read_release_review_decision_record(
                product=profile.product, record_id=operation.idempotency_key.rsplit(":", 1)[0]
            )
        except FileNotFoundError:
            pass
        else:
            # During paged bootstrap its decision may not have been bound yet.
            return []
    if operation.status != "fail":
        return []
    candidate = (
        operation.result.artifact_id if operation.result else ""
    ) or operation.request.expected_artifact_id
    return [
        (
            operation.operation_id,
            candidate or "request:" + _candidate_key(operation.idempotency_key),
            operation.error_code or "promotion_failed",
            operation.runner_trace_id or operation.operation_id,
            operation.finished_at or operation.updated_at,
        )
    ]


def _scan_failures(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: str,
) -> None:
    with (
        store.delivery_evidence_index() as index,
        index.lane_lock(lane.context, profile.product) as acquired,
    ):
        if not acquired:
            return
        checkpoint = index.checkpoint(lane.context, profile.product)
        if checkpoint is None:
            # Read the previous monitor's receipt once for bootstrap. Leave it
            # unchanged so an overlapping or rolled-back reader keeps its dedup.
            legacy = store.read_idempotency_record(
                scope=_SCOPE, route_path=_ROUTE, idempotency_key="failures:" + profile.product
            )
            checkpoint = index.initialize(
                lane.context,
                profile.product,
                baseline=legacy is None,
                legacy_seen=dict(legacy.response_payload.get("seen", {})) if legacy else {},
            )
        latest = checkpoint["latest"]
        legacy_seen = checkpoint["legacy_seen"]
        failed = False
        for work in index.pending(
            lane.context,
            profile.product,
            limit=DELIVERY_EVIDENCE_PAGE_SIZE,
            now=now,
            initial_latest=latest,
        ):
            try:
                index.start(work)
                events = _work_events(store, profile, index, work, now)
                processed = dict(work.processed)
                for record_id, candidate, reason, trace, finished in events:
                    signature = _candidate_key(reason + ":" + finished)
                    if processed.get(record_id, legacy_seen.get(record_id)) == signature:
                        processed[record_id] = signature
                        continue
                    initial_eligible = work.source_id == latest.get(work.kind)
                    if initial_eligible and latest.get("success"):
                        try:
                            initial_eligible = bool(
                                finished and _time(finished) >= _time(latest["success"])
                            )
                        except ValueError:
                            initial_eligible = False
                    if not work.baseline or not checkpoint["initial"] or initial_eligible:
                        record_production_delivery_failure(
                            store,
                            profile,
                            lane,
                            candidate=candidate,
                            reason=reason,
                            trace_id=trace,
                            observed_at=finished or now,
                        )
                    processed[record_id] = signature
                # Preserve the last failure for each fixed-plan step through
                # pass/running transitions, so an identical replay stays silent.
                index.acknowledge(work, processed)
            except Exception:
                failed = True
                index.retry_later(work)
                _LOGGER.exception("Could not process delivery evidence for %s", profile.product)
        index.finish_bootstrap(lane.context, profile.product)
        _recover_delivery_failures(store, profile, lane, now, index)
        if failed:
            raise RuntimeError("Production delivery evidence remains pending")


def _recover_delivery_failures(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: str,
    index: DeliveryEvidenceIndex,
) -> None:
    for incident in store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name="prod",
        status="open",
    ):
        if (
            not incident.check_name.startswith(DELIVERY_CHECK_PREFIX)
            or incident.check_name == RETIRED_PRODUCTION_DELIVERY_LAG_CHECK
            or incident.check_name
            in {
                DELIVERY_CHECK_PREFIX + _candidate_key("monitor:source"),
                DELIVERY_CHECK_PREFIX + _candidate_key("monitor:evidence"),
            }
        ):
            continue
        key = incident.check_name.removeprefix(DELIVERY_CHECK_PREFIX)

        def verify_file(record_id: str) -> bool:
            try:
                record = store.read_deployment_record(record_id)
            except FileNotFoundError:
                return False
            keys = {_candidate_key(record.source_git_ref)}
            if record.artifact_identity:
                keys.add(_candidate_key(record.artifact_identity.artifact_id))
            return bool(
                deployment_record_passed(record)
                and key in keys
                and record.deploy.finished_at
                and _time(record.deploy.finished_at) > _time(incident.opened_at)
            )

        if index.matching_success(
            lane.context,
            key,
            incident.opened_at,
            verify_file if isinstance(store, FilesystemRecordStore) else None,
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
        elif incident.latest_observed_at != now:
            reason = (
                incident.material_fingerprint.route_authority.provider_check.rsplit("/", 1)[-1]
                if incident.material_fingerprint
                else "delivery_failed"
            )
            _observe(
                store,
                profile,
                lane,
                check_name=incident.check_name,
                failed=True,
                summary=incident.summary,
                observed_at=now,
                reason=reason,
            )


def _monitor_availability(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: str,
    *,
    component: str,
    available: bool,
) -> None:
    candidate = "monitor:" + component
    if not available:
        record_production_delivery_failure(
            store,
            profile,
            lane,
            candidate=candidate,
            reason="delivery_" + component + "_unavailable",
            trace_id="production-delivery-monitor:" + _candidate_key(profile.product),
            observed_at=now,
        )
    elif store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name="prod",
        check_name=DELIVERY_CHECK_PREFIX + _candidate_key(candidate),
        status="open",
    ):
        _observe(
            store,
            profile,
            lane,
            check_name=DELIVERY_CHECK_PREFIX + _candidate_key(candidate),
            failed=False,
            summary="Production delivery " + component + " observation recovered.",
            observed_at=now,
        )


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _write_receipt(store: Store, *, key: str, now: str, payload: dict[str, object]) -> None:
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
