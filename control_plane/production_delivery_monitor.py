"""Delivery attention is independent of the health of the artifact still running."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re

from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

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
from control_plane.source_control_change import read_branch_revision, revision_contains
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.delivery_evidence import DeliveryEvidenceIndex, DeliveryEvidenceWork
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.public_ingress_monitor import _store_monitor_observation
from control_plane.workflows.ship import utc_now_timestamp

Store = PostgresRecordStore | FilesystemRecordStore
PRODUCTION_DELIVERY_LAG_SECONDS = 3600
DELIVERY_CHECK_PREFIX = "production-delivery-"
DELIVERY_LAG_CHECK = DELIVERY_CHECK_PREFIX + "lag"
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
    reason: str = "delivery_failed",
    retired: bool = False,
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
                url="provider://launchplane/production-delivery/" + reason,
                status="fail" if failed else "pass",
                failure_code="production_delivery_failed" if failed else None,
                summary=summary,
            ),
        ),
    )
    if retired:
        observation = observation.model_copy(
            update={
                "status": "skipped",
                "failure_code": "monitoring_intent_changed",
                "purpose": "reconciliation",
                "targets": (),
            }
        )
    # On the service store, observation, incident and notification outbox commit together.
    result = _store_monitor_observation(
        record_store=store,
        record=observation,
        incident_eligible=not retired,
        expected_profile_sha256=product_profile_record_sha256(profile),
        recovery_observation_threshold=1,
        notify=True,
        notification_drivers=None,
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
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    now = observed_at or utc_now_timestamp()
    for profile in store.list_product_profile_records():
        for lane in profile.lanes:
            if should_stop():
                return
            if lane.instance != "prod":
                continue
            if not profile.is_active or profile.production_use != "live":
                for incident in store.list_public_ingress_incident_records(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name="prod",
                    status="open",
                ):
                    if incident.check_name in {
                        DELIVERY_LAG_CHECK,
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
                _monitor_lane(store, profile, lane, now, transport_factory, max_age_seconds)
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
    max_age_seconds: int,
) -> None:
    with store.delivery_evidence_index() as index:
        latest = _latest_deployment(store, index, lane.context)
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
    head = source.commit
    source_seen_at = now
    if source.committed_at:
        try:
            if _time(source.committed_at) <= _time(now):
                source_seen_at = source.committed_at
        except ValueError:
            pass
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
    reached_pending = bool(pending and pending == current)
    source_available = True
    if pending and current and pending != current:
        try:
            comparison = revision_contains(
                repository=profile.repository,
                ancestor=pending,
                head=current,
                read=transport.get_json,
            )
            reached_pending = comparison is True
            source_available = comparison is not None
        except Exception:
            # Unknown comparison cannot resolve attention or stop the overdue check.
            _LOGGER.warning("Production source comparison unavailable for %s", profile.product)
            source_available = False
    if reached_pending or current == head:
        pending, since = "", ""
    if current != head and not pending:
        pending, since = head, source_seen_at
    _monitor_availability(store, profile, lane, now, component="source", available=source_available)
    if not source_available:
        return
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
    if overdue or ((not pending or reached_pending) and open_lag):
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


def _latest_deployment(
    store: Store, index: DeliveryEvidenceIndex, context: str
) -> DeploymentRecord | None:
    for _ in range(DELIVERY_EVIDENCE_PAGE_SIZE):
        latest_id = index.latest_deployment_id(context)
        if not latest_id:
            return None
        try:
            return store.read_deployment_record(latest_id)
        except FileNotFoundError:
            if not isinstance(store, FilesystemRecordStore):
                raise
            # A file writer may have crashed after publishing pending work but
            # before publishing a new source file. The missing file owns no outcome.
            with store._product_authority_bundle_lock():
                if not store._record_path("deployments", latest_id).exists():
                    index.forget_missing("deployment:" + latest_id)
    raise RuntimeError("Interrupted filesystem writes still need index reconciliation")


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
        if isinstance(store, FilesystemRecordStore):
            index.reconcile_file(work, record.model_dump(mode="json"))
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
        index.bind(decision.record_id, _release_dependencies(profile, decision))
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
    scan_key = "failures:" + profile.product
    scan = store.read_idempotency_record(scope=_SCOPE, route_path=_ROUTE, idempotency_key=scan_key)
    with (
        store.delivery_evidence_index() as index,
        index.lane_lock(lane.context, profile.product) as acquired,
    ):
        if not acquired:
            return
        checkpoint = index.initialize(
            lane.context,
            profile.product,
            baseline=scan is None,
            legacy_seen=dict(scan.response_payload.get("seen", {})) if scan else {},
        )
        latest = checkpoint["latest"]
        legacy_seen = checkpoint["legacy_seen"]
        failed = False
        for work in index.pending(
            lane.context, profile.product, limit=DELIVERY_EVIDENCE_PAGE_SIZE, now=now
        ):
            try:
                index.start(work)
                events = _work_events(store, profile, index, work)
                processed = dict(work.processed)
                for record_id, candidate, reason, trace, finished in events:
                    signature = _candidate_key(reason + ":" + finished)
                    if processed.get(record_id, legacy_seen.get(record_id)) == signature:
                        processed[record_id] = signature
                        continue
                    initial_eligible = work.source_id == latest.get(work.kind)
                    if initial_eligible and latest.get("success"):
                        initial_eligible = bool(
                            finished and _time(finished) >= _time(latest["success"])
                        )
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
        _write_receipt(store, key=scan_key, now=now, payload={"indexed": True})
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
            or incident.check_name == DELIVERY_LAG_CHECK
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
