"""Delivery attention is independent of the health of the artifact still running."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from control_plane.build_provenance import BuildProvenanceTransport
from control_plane.contracts.deployment_record import (
    DEPLOYMENT_OPERATION_EVIDENCE_KEY,
    DeploymentRecord,
    deployment_operation_evidence,
    deployment_record_passed,
)
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_profile_record_sha256,
    product_lane_monitoring_probe_effective,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.generic_web_source_qualification import (
    GenericWebPromotionAdmissionRefused,
    GenericWebPromotionSourceRefused,
)
from control_plane.workflows.production_promotion_backup import (
    promotion_is_temporary_admission_refusal,
)
from control_plane.contracts.public_ingress_monitoring import (
    RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
    PRODUCTION_REQUEST_REFUSED,
    PublicIngressFailureCode,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    PublicIngressIncidentRecord,
    public_ingress_incident_record_sha256,
    build_public_ingress_observation_id,
    build_public_ingress_reconciliation_observation_id,
)
from control_plane.product_reconcile import resolve_build_provenance_transport
from control_plane.release_review import ProductionChangeRequiresPromotion
from control_plane.lane_runtime_verification import (
    monitor_observation_provenance,
    read_lane_runtime_verification,
)
from control_plane.source_control_change import read_branch_revision
from control_plane.split_ingress import (
    read_split_ingress_binding,
    split_ingress_monitoring_lane,
    split_ingress_advisory_check,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.delivery_evidence import (
    DeliveryEvidenceIndex,
    DeliveryEvidenceWork,
    DeliveryFailureObservation,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressIncidentChanged,
    _store_monitor_observation,
)
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
        GenericWebPromotionAdmissionRefused.failure_code,
        GenericWebPromotionSourceRefused.failure_code,
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


@dataclass(frozen=True)
class DeliveryFailureReference:
    trace_id: str
    failed_at: str


def is_guard_refusal_incident(incident: PublicIngressIncidentRecord) -> bool:
    return bool(
        incident.instance == "prod"
        and incident.check_name.startswith(DELIVERY_CHECK_PREFIX)
        and incident.material_fingerprint
        and incident.material_fingerprint.route_authority.provider_check
        == "production-delivery/" + ProductionChangeRequiresPromotion.code
    )


def _is_refusal_only_incident(store: Store, incident: PublicIngressIncidentRecord) -> bool:
    if not is_guard_refusal_incident(incident):
        return False
    history = store.list_public_ingress_observation_records(incident_id=incident.incident_id)
    return bool(history) and all(
        observation.status == "fail"
        and observation.targets
        and all(
            target.url == _delivery_target_url(ProductionChangeRequiresPromotion.code)
            for target in observation.targets
        )
        for observation in history
    )


def _resolve_guard_refusal(
    store: Store,
    *,
    incident: PublicIngressIncidentRecord,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    now: datetime,
    health_after: datetime,
    summary: str,
) -> str:
    binding = read_split_ingress_binding(store, profile, lane)
    lane = split_ingress_monitoring_lane(profile, lane, binding)
    checks = tuple(
        check
        for check in lane.health_monitoring.checks
        if check.enabled
        and not split_ingress_advisory_check(profile, lane, binding, check)
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
            or _time(observations[0].observed_at) <= health_after
            or monitor_observation_provenance(observations[0], now=now).freshness_status
            != "verified"
        ):
            raise ValueError("Production health must pass freshly after the last refusal.")
    _require_verified_production_runtime(store, profile, lane, now)
    if not _is_refusal_only_incident(store, incident):
        raise ValueError("Incident history includes a failure or uncertain outcome beyond refusal.")
    _observe(
        store,
        profile,
        lane,
        check_name=incident.check_name,
        failed=False,
        retired=True,
        observed_at=now.isoformat(),
        summary=summary + "Production health and runtime identity remain verified.",
        expected_incident_sha256=public_ingress_incident_record_sha256(incident),
    )
    return incident.incident_id


def _require_verified_production_runtime(
    store: Store, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, now: datetime
) -> None:
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


def _recheck_guard_refusals(
    store: Store, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, now: str
) -> None:
    with (
        store.delivery_evidence_index() as index,
        index.lane_lock(lane.context, profile.product) as acquired,
    ):
        if not acquired:
            return
        incidents = tuple(
            incident
            for incident in store.list_public_ingress_incident_records(
                product=profile.product,
                context_name=lane.context,
                instance_name="prod",
                status="open",
            )
            if _is_refusal_only_incident(store, incident)
        )
        if not incidents:
            return
        try:
            _require_verified_production_runtime(store, profile, lane, _time(now))
        except (ValueError, FileNotFoundError):
            # Do not scan refusal events while health/identity cannot qualify recovery.
            return
        refusals = store.list_public_ingress_observation_records(
            product=profile.product,
            context_name=lane.context,
            instance_name="prod",
            check_kind="provider",
            check_name_prefix=REFUSED_REQUEST_CHECK_PREFIX,
            limit=1,
        )
        latest_refusal = max(
            [_time(incident.opened_at) for incident in incidents]
            + [_time(record.observed_at) for record in refusals]
        )
        for incident in incidents:
            try:
                _resolve_guard_refusal(
                    store,
                    incident=incident,
                    profile=profile,
                    lane=lane,
                    now=_time(now),
                    health_after=latest_refusal,
                    summary="Production request refused, nothing changed. Scheduled recheck: ",
                )
            except (ValueError, FileNotFoundError, RuntimeError):
                # Unhealthy, stale, changed or uncertain evidence stays open for the next pass.
                continue


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
    failure_reference: DeliveryFailureReference | None = None,
    failure_event: bool = False,
) -> None:
    delivery_kind = "testing" if lane.instance == "testing" else "production"
    failure_code: PublicIngressFailureCode = (
        "testing_delivery_failed" if lane.instance == "testing" else "production_delivery_failed"
    )
    target_url = f"provider://launchplane/{delivery_kind}-delivery/{reason}"
    if failure_reference is not None:
        target_url += "?" + urlencode(
            {"trace": failure_reference.trace_id, "failed_at": failure_reference.failed_at}
        )
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
        failure_code=failure_code if failed else None,
        summary=summary,
        targets=(
            PublicIngressTargetObservation(
                target="provider",
                url=target_url,
                status="fail" if failed else "pass",
                failure_code=failure_code if failed else None,
                summary=summary,
            ),
        ),
    )
    if failure_event and failure_reference is not None:
        observation = observation.model_copy(
            update={
                "record_id": observation.record_id
                + "-"
                + _candidate_key(
                    reason + "\0" + failure_reference.trace_id + "\0" + failure_reference.failed_at
                )
            }
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
                "record_id": build_public_ingress_reconciliation_observation_id(
                    product=profile.product,
                    context=lane.context,
                    instance=lane.instance,
                    check_name=check_name,
                    observed_at=observed_at,
                ),
                "status": "skipped",
                "failure_code": "monitoring_intent_changed",
                "purpose": "reconciliation",
                "targets": (
                    PublicIngressTargetObservation(
                        target="monitoring_intent",
                        url=f"monitoring-intent://{delivery_kind}-delivery",
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
        if retired and expected_incident_sha256
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
    safe_trace = trace_id if re.fullmatch(r"[A-Za-z0-9_:\-.]{1,200}", trace_id) else "unavailable"
    failure_at = observed_at or utc_now_timestamp()
    reference = DeliveryFailureReference(
        trace_id=safe_trace,
        failed_at=_canonical_time(failure_at),
    )
    existing = store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        check_name=check_name,
        status="open",
    )
    if (
        existing
        and existing[0].material_fingerprint is not None
        and (
            existing[0].material_fingerprint.route_authority.provider_check
            == "production-delivery/" + safe_reason
        )
    ):
        previous = store.read_public_ingress_observation_record(existing[0].latest_observation_id)
        if (
            _delivery_failure_reference(
                DeliveryFailureObservation(
                    summary=previous.summary,
                    observed_at=previous.observed_at,
                    target_url=previous.targets[0].url if previous.targets else "",
                )
            )
            == reference
        ):
            return
    _observe(
        store,
        profile,
        lane,
        check_name=check_name,
        failed=True,
        summary=f"Production delivery blocked: {safe_reason}; trace: {safe_trace}. "
        "The previous runtime may still be healthy. Inspect the supported release path.",
        observed_at=failure_at,
        reason=safe_reason,
        failure_reference=reference
        if safe_reason != ProductionChangeRequiresPromotion.code
        else None,
        failure_event=True,
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
            try:
                _recheck_guard_refusals(store, profile, lane, now)
            except Exception:
                _LOGGER.exception("Could not recheck production refusals for %s", profile.product)


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
        if isinstance(record, PromotionRecord) and promotion_is_temporary_admission_refusal(record):
            return []
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
        failures = store.list_delivery_failure_observations(incident_id=incident.incident_id)
        failed_at, rollback_only = _delivery_failure_evidence(
            store, lane, incident, failures, index
        )

        def verify_deployment(record_id: str) -> bool:
            try:
                record = store.read_deployment_record(record_id)
            except FileNotFoundError:
                return False
            keys = {_candidate_key(record.source_git_ref)}
            if record.artifact_identity:
                keys.add(_candidate_key(record.artifact_identity.artifact_id))
            return bool(
                deployment_record_passed(record)
                and record.failure is None
                and key in keys
                and record.deploy.finished_at
                and _time(failed_at) < _time(record.deploy.finished_at) <= _time(now)
                and record.context == lane.context
                and record.instance == "prod"
            )

        recovery_summary = ""
        if index.matching_success(
            lane.context,
            key,
            failed_at,
            verify_deployment,
            finished_before=now,
        ):
            recovery_summary = "The matching production delivery succeeded."
        elif (
            profile.driver_id == "generic-web"
            and rollback_only
            and (recovery := _later_passing_rollback(store, lane, failed_at, now, index))
        ):
            recovery_summary = f"The failed production rollback recovered through {recovery}."
        elif recovery := _later_completed_recipient_release(
            store, profile, lane, key, failures, failed_at, now
        ):
            recovery_summary = (
                f"The retained failed recipient recovered through accepted release {recovery}."
            )
        if recovery_summary:
            try:
                _observe(
                    store,
                    profile,
                    lane,
                    check_name=incident.check_name,
                    failed=False,
                    summary=recovery_summary,
                    observed_at=now,
                    expected_incident_sha256=public_ingress_incident_record_sha256(incident),
                )
            except PublicIngressIncidentChanged:
                continue
        elif not _is_refusal_only_incident(store, incident) and incident.latest_observed_at != now:
            reason = (
                incident.material_fingerprint.route_authority.provider_check.rsplit("/", 1)[-1]
                if incident.material_fingerprint
                else "delivery_failed"
            )
            previous = store.read_public_ingress_observation_record(incident.latest_observation_id)
            _observe(
                store,
                profile,
                lane,
                check_name=incident.check_name,
                failed=True,
                summary=incident.summary,
                observed_at=now,
                reason=reason,
                failure_reference=_delivery_failure_reference(
                    DeliveryFailureObservation(
                        summary=previous.summary,
                        observed_at=previous.observed_at,
                        target_url=previous.targets[0].url if previous.targets else "",
                    )
                )
                if previous.targets and urlsplit(previous.targets[0].url).query
                else None,
            )


def _later_completed_recipient_release(
    store: Store,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    key: str,
    failures: tuple[DeliveryFailureObservation, ...],
    failed_at: str,
    now: str,
) -> str:
    if (
        not isinstance(store, PostgresRecordStore)
        or profile.driver_id != "generic-web"
        or not failures
    ):
        return ""
    from control_plane.client_release import (
        CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        _covering_decision,
        read_client_release_run,
    )
    from control_plane.production_recipient import custody
    from control_plane.production_recipient_publication import require_owned_publication_rows

    decisions = store.list_release_review_decision_records(product=profile.product, limit=1)
    if not decisions:
        return ""
    decision = decisions[0]
    if (
        _covering_decision(
            store, profile.product, decision_record_id=decision.record_id, recovering=True
        )
        is None
    ):
        return ""
    record = custody(store, profile.product, lane.context)
    if (
        decision.decision != "accepted"
        or decision.release_start not in {"promote", "promote_with_rollback_drill"}
        or not decision.release_issue_url
        or decision.checklist.repository != profile.repository
        or decision.checklist.owner_github_id != profile.owner.github_id
        or decision.actor_github_id != profile.owner.github_id
        or record is None
        or record.phase != "published"
        or record.image_reference != decision.checklist.candidate.artifact_id
        or record.request.source_commit != decision.checklist.candidate.source_commit
        or record.execution.get("release_trace") != "client-release-" + decision.record_id
    ):
        return ""
    run = read_client_release_run(store=store, profile=profile, decision=decision)
    if run is None or run.state != "passed":
        return ""
    final = store.read_idempotency_record_by_id(run.steps[-1].operation_id)
    if (
        final is None
        or final.state != "completed"
        or final.scope != CLIENT_RELEASE_IDEMPOTENCY_SCOPE
        or final.response_payload.get("result", {}).get("promotion_status") != "pass"
        or final.response_payload.get("result", {}).get("recipient_plan_sha256")
        != record.plan_sha256
        or not final.updated_at
        or not _time(failed_at) < _time(final.updated_at) <= _time(now)
    ):
        return ""
    settled_traces = (
        _settled_recipient_release_traces(store, profile, record, decision, final.updated_at)
        if key
        in {_candidate_key(record.image_reference), _candidate_key(record.request.source_commit)}
        else set()
    )
    for prior in record.prior_attempts:
        try:
            previous = ProductionRecipientRecord.model_validate(prior)
            if (
                previous.phase not in {"rolled_back", "preflight_refused"}
                or (previous.product, previous.context) != (record.product, record.context)
                or key
                not in {
                    _candidate_key(previous.image_reference),
                    _candidate_key(previous.request.source_commit),
                }
            ):
                continue
            trace = previous.execution.get("release_trace", "")
            if not trace.startswith("client-release-"):
                continue
            previous_decision = store.read_release_review_decision_record(
                product=profile.product, record_id=trace.removeprefix("client-release-")
            )
            settled_traces.update(
                _settled_recipient_release_traces(
                    store, profile, previous, previous_decision, final.updated_at
                )
            )
        except (ValueError, KeyError, TypeError, FileNotFoundError):
            continue
    if any(
        _delivery_failure_reference(failure).trace_id not in settled_traces for failure in failures
    ):
        return ""
    try:
        require_owned_publication_rows(store, record, inventory_timestamp_required=False)
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
            now=_time(now),
        )
    except (ValueError, FileNotFoundError):
        return ""
    if (
        verification.provenance.freshness_status != "verified"
        or verification.observation is None
        or _time(verification.observation.observed_at) <= _time(final.updated_at)
    ):
        return ""
    return decision.record_id


def _settled_recipient_release_traces(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    record: ProductionRecipientRecord,
    decision: ReleaseReviewDecisionRecord,
    finished_before: str,
) -> set[str]:
    from control_plane.client_release import (
        CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        _covering_decision,
        read_client_release_step_views,
    )
    from control_plane.integration_allowances import canonical_sha256
    from control_plane.production_recipient_cutover import cutover_reconciliation_key
    from control_plane.provider_operations import build_provider_operation_key

    trace = "client-release-" + decision.record_id
    if (
        decision.decision != "accepted"
        or _covering_decision(
            store, profile.product, decision_record_id=decision.record_id, recovering=True
        )
        is None
        or decision.checklist.repository != profile.repository
        or decision.checklist.candidate.artifact_id != record.image_reference
        or decision.checklist.candidate.source_commit != record.request.source_commit
        or record.execution.get("release_trace") != trace
    ):
        return set()
    traces: set[str] = set()
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
    if not views:
        return set()
    for view in views:
        if view.status == "not_started":
            continue
        if view.kind == "backup":
            if view.status != "pass":
                return set()
            continue
        receipt = store.read_idempotency_record_by_id(view.operation_id)
        if (
            view.status not in {"pass", "fail"}
            or receipt is None
            or receipt.state != "completed"
            or receipt.scope != CLIENT_RELEASE_IDEMPOTENCY_SCOPE
            or not receipt.updated_at
            or _time(receipt.updated_at) > _time(finished_before)
        ):
            return set()
        outcome = receipt.response_payload.get("result", {})
        if outcome.get("recipient_plan_sha256") != record.plan_sha256:
            return set()
        if view.kind == "rollback":
            if view.status != "pass" or outcome.get("rollback_status") != "pass":
                return set()
        else:
            if receipt.reconciliation_key != cutover_reconciliation_key(record.plan_sha256):
                return set()
            if view.status == "fail" and (
                outcome.get("rollback_status") == "fail"
                or not (
                    outcome.get("writer_effect") == "none"
                    or outcome.get("rollback_status") == "pass"
                )
            ):
                return set()
            provider_key = build_provider_operation_key(
                scope=receipt.scope,
                route_path=receipt.route_path,
                idempotency_key=receipt.idempotency_key,
                request_fingerprint=receipt.request_fingerprint,
                reconciliation_key=receipt.reconciliation_key,
            )
            traces.add("promotion-recipient-" + canonical_sha256(provider_key)[:24])
        traces.update((receipt.record_id, receipt.response_trace_id))
        if view.failure is not None:
            traces.update((view.failure.record_id, view.failure.trace_id))
    traces.discard("")
    return traces | {trace} if traces else set()


def _is_rollback_deployment(record: DeploymentRecord) -> bool:
    if DEPLOYMENT_OPERATION_EVIDENCE_KEY in record.runtime_source:
        return record.runtime_source.items() >= deployment_operation_evidence(rollback=True).items()
    return record.deploy.deploy_mode == "atomic-switch-back" or record.record_id.endswith(
        "-rollback"
    )


def _delivery_failure_trace(summary: str) -> str:
    match = re.search(r"; trace: ([A-Za-z0-9_:\-.]{1,200})\. ", summary)
    return match[1] if match else ""


def _delivery_failure_reference(
    observation: DeliveryFailureObservation,
) -> DeliveryFailureReference:
    parsed = urlsplit(observation.target_url)
    if parsed.query:
        metadata = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != "provider"
            or parsed.netloc != "launchplane"
            or not parsed.path.startswith("/production-delivery/")
            or set(metadata) != {"trace", "failed_at"}
            or any(len(values) != 1 for values in metadata.values())
            or re.fullmatch(r"[A-Za-z0-9_:\-.]{1,200}", metadata["trace"][0]) is None
        ):
            raise ValueError("Stored production delivery failure reference is invalid.")
        return DeliveryFailureReference(
            trace_id=metadata["trace"][0],
            failed_at=_canonical_time(metadata["failed_at"][0]),
        )
    return DeliveryFailureReference(
        trace_id=_delivery_failure_trace(observation.summary),
        failed_at=_canonical_time(observation.observed_at),
    )


def _delivery_failure_evidence(
    store: Store,
    lane: ProductLaneProfile,
    incident: PublicIngressIncidentRecord,
    failures: tuple[DeliveryFailureObservation, ...],
    index: DeliveryEvidenceIndex,
) -> tuple[str, bool]:
    references = tuple(_delivery_failure_reference(failure) for failure in failures)
    latest = max(
        [_time(incident.opened_at)] + [_time(reference.failed_at) for reference in references]
    )
    key = incident.check_name.removeprefix(DELIVERY_CHECK_PREFIX)
    traces = {reference.trace_id for reference in references}
    record_ids = dict.fromkeys(
        [
            *traces,
            *index.deployment_candidates(lane.context, key=key, finished_since=incident.opened_at),
        ]
    )
    rollback_only = bool(failures)
    for record_id in record_ids:
        try:
            record = store.read_deployment_record(record_id)
        except FileNotFoundError:
            if record_id in traces:
                rollback_only = False
            continue
        keys = {_candidate_key(record.source_git_ref)}
        if record.artifact_identity:
            keys.add(_candidate_key(record.artifact_identity.artifact_id))
        failed = record.failure is not None or "fail" in {
            record.deploy.status,
            record.post_deploy_update.status,
            record.destination_health.status,
        }
        matches = record.context == lane.context and record.instance == "prod" and key in keys
        if record_id in traces and not (matches and failed and record.deploy.finished_at):
            rollback_only = False
        if matches and failed and record.deploy.finished_at:
            latest = max(latest, _time(record.deploy.finished_at))
            rollback_only = rollback_only and _is_rollback_deployment(record)
    return latest.isoformat(), rollback_only


def _later_passing_rollback(
    store: Store,
    lane: ProductLaneProfile,
    failed_at: str,
    now: str,
    index: DeliveryEvidenceIndex,
) -> str:
    for record_id in index.deployment_candidates(
        lane.context, passed=True, finished_after=failed_at, finished_before=now
    ):
        try:
            record = store.read_deployment_record(record_id)
        except FileNotFoundError:
            continue
        if (
            record.context == lane.context
            and record.instance == "prod"
            and _is_rollback_deployment(record)
            and record.failure is None
            and deployment_record_passed(record)
            and record.deploy.finished_at
            and _time(failed_at) < _time(record.deploy.finished_at) <= _time(now)
        ):
            return record.record_id
    return ""


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
    incidents = store.list_public_ingress_incident_records(
        product=profile.product,
        context_name=lane.context,
        instance_name="prod",
        check_name=DELIVERY_CHECK_PREFIX + _candidate_key(candidate),
        status="open",
    )
    if not available and not incidents:
        record_production_delivery_failure(
            store,
            profile,
            lane,
            candidate=candidate,
            reason="delivery_" + component + "_unavailable",
            trace_id="production-delivery-monitor:" + _candidate_key(profile.product),
            observed_at=now,
        )
    elif available and incidents:
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


def _canonical_time(value: str) -> str:
    return (
        _time(value)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
