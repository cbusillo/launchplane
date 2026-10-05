from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_lane_monitoring_probe_effective,
)
from control_plane.contracts.public_ingress_monitoring import (
    PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity, compare_runtime_identity


@dataclass(frozen=True)
class LaneRuntimeVerification:
    provenance: DataProvenance
    observation: PublicIngressObservationRecord | None = None
    runtime_target: PublicIngressTargetObservation | None = None


def lane_expected_runtime_identity(summary: LaunchplaneLaneSummary) -> RuntimeIdentity | None:
    if summary.inventory is not None:
        return summary.inventory.runtime_identity
    if summary.latest_deployment is not None:
        return summary.latest_deployment.runtime_identity
    return None


def monitor_observation_provenance(
    observation: PublicIngressObservationRecord, *, now: datetime | None = None
) -> DataProvenance:
    evaluated_at = now or datetime.now(timezone.utc)
    observed_at = _timestamp(observation.observed_at)
    stale_at = (
        observed_at + timedelta(seconds=PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS)
        if observed_at is not None
        else None
    )
    status: FreshnessStatus = (
        "stale"
        if stale_at is None or observed_at is None or not observed_at <= evaluated_at <= stale_at
        else "unsupported"
        if observation.status == "skipped"
        else "verified"
    )
    return DataProvenance(
        source_kind="record",
        source_record_id=observation.record_id,
        recorded_at=observation.observed_at,
        refreshed_at=observation.observed_at,
        freshness_status=status,
        stale_after=stale_at.isoformat().replace("+00:00", "Z") if stale_at else "",
        detail="Launchplane monitor observation, current within its declared cadence.",
    )


def read_lane_runtime_verification(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    summary: LaunchplaneLaneSummary,
    now: datetime | None = None,
) -> LaneRuntimeVerification:
    read_observations = getattr(record_store, "list_public_ingress_observation_records", None)
    evaluated_at = now or datetime.now(timezone.utc)
    deployment_at = (
        summary.inventory.updated_at
        if summary.inventory is not None
        else (
            summary.latest_deployment.deploy.finished_at
            or summary.latest_deployment.deploy.started_at
        )
        if summary.latest_deployment is not None
        else ""
    )
    deployment_id = (
        summary.inventory.deployment_record_id
        if summary.inventory is not None
        else summary.latest_deployment.record_id
        if summary.latest_deployment is not None
        else ""
    )
    recorded_at = _timestamp(deployment_at)
    stale_at = (
        recorded_at + timedelta(seconds=PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS)
        if recorded_at is not None
        else None
    )
    historical_status: FreshnessStatus = (
        "missing"
        if not deployment_id
        else "stale"
        if stale_at is not None and evaluated_at > stale_at
        else "recorded"
    )
    missing = LaneRuntimeVerification(
        DataProvenance(
            source_kind="record",
            source_record_id=deployment_id,
            recorded_at=deployment_at,
            freshness_status=historical_status,
            stale_after=stale_at.isoformat().replace("+00:00", "Z") if stale_at else "",
            detail="Launchplane has no current monitor runtime-identity and health verification.",
        )
    )
    if not callable(read_observations):
        return missing
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
        return missing
    expected = lane_expected_runtime_identity(summary)
    results: list[LaneRuntimeVerification] = []
    for check in checks:
        observation = next(
            (
                record
                for record in read_observations(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name=lane.instance,
                    check_name=check.name,
                    check_kind=check.kind,
                    limit=50,
                )
                if isinstance(record, PublicIngressObservationRecord) and record.purpose == "probe"
            ),
            None,
        )
        if observation is None:
            results.append(missing)
            continue
        target = next(
            (
                target
                for target in observation.targets
                if target.target in {"health_url", "private_health_url"}
            ),
            None,
        )
        provenance = monitor_observation_provenance(observation, now=now)
        observed_at = _timestamp(observation.observed_at)
        authority_times = [profile.updated_at, deployment_at]
        if check.private_endpoint_key:
            read_endpoint = getattr(record_store, "read_private_health_endpoint_record", None)
            if not callable(read_endpoint):
                results.append(missing)
                continue
            try:
                endpoint = read_endpoint(check.private_endpoint_key)
            except (FileNotFoundError, KeyError):
                results.append(missing)
                continue
            if endpoint.status != "active" or (
                endpoint.product,
                endpoint.context,
                endpoint.instance,
            ) != (profile.product, lane.context, lane.instance):
                results.append(missing)
                continue
            authority_times.append(endpoint.updated_at)
        current = (
            observed_at is not None
            and observation.monitoring_intent == lane.health_monitoring.monitoring_intent
            and all(
                (at := _timestamp(value)) is not None and observed_at > at
                for value in authority_times
                if value
            )
            and expected is not None
            and compare_runtime_identity(
                expected=expected, observed=observation.expected_runtime_identity
            )[0]
            == "match"
        )
        passed = (
            current
            and observation.status == "pass"
            and target is not None
            and target.status == "pass"
            and target.runtime_identity_status == "match"
            and compare_runtime_identity(
                expected=expected, observed=target.observed_runtime_identity
            )[0]
            == "match"
        )
        if provenance.freshness_status == "verified" and not passed:
            provenance = provenance.model_copy(update={"freshness_status": "recorded"})
        results.append(
            LaneRuntimeVerification(provenance, observation, target if current else None)
        )
    # Every effective check must pass; never pick an older success over a failure.
    provenance = min(results, key=lambda result: result.provenance.stale_after).provenance
    for status in ("missing", "stale", "recorded", "unsupported"):
        failed = next(
            (result for result in results if result.provenance.freshness_status == status), None
        )
        if failed is not None:
            provenance = failed.provenance
            break
    identity_failure = next(
        (
            result
            for result in results
            if result.runtime_target is not None
            and result.runtime_target.runtime_identity_status
            in {"mismatch", "missing", "malformed", "unverifiable"}
        ),
        None,
    )
    evidence = identity_failure or next(
        (result for result in results if result.runtime_target is not None), None
    )
    return LaneRuntimeVerification(
        provenance,
        evidence.observation if evidence else None,
        evidence.runtime_target if evidence else None,
    )


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
