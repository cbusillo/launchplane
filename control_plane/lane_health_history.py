"""Read retained lane probes, and account for elapsed time without inventing passes."""

import base64
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from itertools import groupby
import json
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressCheckKind,
    PublicIngressFailureCode,
    PublicIngressObservationPurpose,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetKind,
    health_monitor_freshness_seconds,
    PUBLIC_TLS_STALE_AFTER_SECONDS,
)
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    RuntimeIdentityStatus,
    compare_runtime_identity,
)


class HistoryIdentity(BaseModel):
    """Artifact/deployment references, without image locations or provider details."""

    model_config = ConfigDict(extra="forbid")
    deployment_record_id: str
    artifact_id: str
    source_git_ref: str
    image_digest: str


class HistoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: PublicIngressTargetKind
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None
    runtime_identity_status: RuntimeIdentityStatus
    observed_runtime_identity: HistoryIdentity | None


class HistoryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    check_name: str
    check_kind: PublicIngressCheckKind
    purpose: PublicIngressObservationPurpose
    observed_at: AwareDatetime
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None
    expected_runtime_identity: HistoryIdentity | None
    targets: tuple[HistoryTarget, ...]
    incident_id: str


class LaneHealthHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: str
    since: AwareDatetime
    until: AwareDatetime
    cursor: str | None
    next_cursor: str | None
    observations: tuple[HistoryObservation, ...]


class LaneHealthHistoryResponse(BaseModel):
    trace_id: str
    history: LaneHealthHistory


class HistoryStore(Protocol):
    def list_public_ingress_history_records(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
        since: datetime,
        until: datetime,
        before: tuple[str, str] | None,
        limit: int,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...


class HistoryQueryError(ValueError):
    """The lane/window/cursor request is invalid, rather than stored evidence."""


def _identity(value: RuntimeIdentity | None) -> HistoryIdentity | None:
    if value is None:
        return None
    return HistoryIdentity(
        deployment_record_id=value.deployment_record_id,
        artifact_id=value.artifact_id,
        source_git_ref=value.source_git_ref,
        image_digest=value.image_digest,
    )


def project_observation(record: PublicIngressObservationRecord) -> HistoryObservation:
    targets = []
    for target in record.targets:
        status = target.runtime_identity_status
        # Never trust a historical "match" that its own payload contradicts.
        if status == "match":
            status, _ = compare_runtime_identity(
                expected=record.expected_runtime_identity, observed=target.observed_runtime_identity
            )
        targets.append(
            HistoryTarget(
                target=target.target,
                status=target.status,
                failure_code=target.failure_code,
                runtime_identity_status=status,
                observed_runtime_identity=_identity(target.observed_runtime_identity),
            )
        )
    return HistoryObservation(
        record_id=record.record_id,
        check_name=record.check_name,
        check_kind=record.check_kind,
        purpose=record.purpose,
        observed_at=datetime.fromisoformat(record.observed_at.replace("Z", "+00:00")),
        status=record.status,
        failure_code=record.failure_code,
        expected_runtime_identity=_identity(record.expected_runtime_identity),
        targets=tuple(targets),
        incident_id=record.incident_id,
    )


def _window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    if since.tzinfo is None or until.tzinfo is None or not since < until:
        raise HistoryQueryError("Use an increasing window with timezone-aware timestamps.")
    if until - since > timedelta(days=8):
        raise HistoryQueryError("Read at most eight days of lane history per window.")
    return since.astimezone(timezone.utc), until.astimezone(timezone.utc)


def read_lane_health_history(
    store: HistoryStore,
    *,
    product: str,
    context: str,
    instance: str,
    since: datetime,
    until: datetime,
    limit: int = 200,
    cursor: str | None = None,
) -> LaneHealthHistory:
    since, until = _window(since, until)
    if not 1 <= limit <= 500:
        raise HistoryQueryError("Read between one and 500 observations per page.")
    binding = [product, context, instance, since.isoformat(), until.isoformat()]
    before = None
    if cursor is not None:
        try:
            data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(data, list) or len(data) != 7 or data[:5] != binding:
                raise ValueError
            if not isinstance(data[5], str) or not isinstance(data[6], str) or not data[6]:
                raise ValueError
            position = datetime.fromisoformat(data[5])
            if position.tzinfo is None:
                raise ValueError
            before = (data[5], data[6])
        except (ValueError, TypeError, UnicodeError) as error:
            raise HistoryQueryError(
                "History cursor does not match this lane and window."
            ) from error
    # Include bounded lookback so a pass just before the window can cover its beginning.
    lookback = max(health_monitor_freshness_seconds(instance), PUBLIC_TLS_STALE_AFTER_SECONDS)
    records = store.list_public_ingress_history_records(
        product=product,
        context_name=context,
        instance_name=instance,
        since=since - timedelta(seconds=lookback),
        until=until,
        before=before,
        limit=limit + 1,
    )
    page = records[:limit]
    next_cursor = None
    if len(records) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([*binding, last.observed_at, last.record_id]).encode()
        ).decode()
    return LaneHealthHistory(
        product=product,
        context=context,
        instance=instance,
        since=since,
        until=until,
        cursor=cursor,
        next_cursor=next_cursor,
        observations=tuple(project_observation(record) for record in page),
    )


class CheckHistoryAccounting(BaseModel):
    check_name: str
    check_kind: PublicIngressCheckKind
    observation_count: int
    health_pass_seconds: float
    health_fail_seconds: float
    health_unknown_seconds: float
    identity_verified_seconds: float
    health_and_identity_seconds: float
    health_pass_percent: float
    health_and_identity_percent: float


class HealthHistoryAccounting(BaseModel):
    since: AwareDatetime
    until: AwareDatetime
    denominator_seconds: float
    checks: tuple[CheckHistoryAccounting, ...]
    required_checks: tuple[tuple[PublicIngressCheckKind, str], ...]
    identity_checks: tuple[tuple[PublicIngressCheckKind, str], ...]
    excluded_checks: tuple[tuple[PublicIngressCheckKind, str], ...]
    missing_checks: tuple[tuple[PublicIngressCheckKind, str], ...]
    lane_health_pass_seconds: float
    lane_health_fail_seconds: float
    lane_health_unknown_seconds: float
    lane_identity_verified_seconds: float
    lane_health_and_identity_seconds: float
    lane_health_pass_percent: float
    lane_health_and_identity_percent: float


class LaneHealthHistoryAccounting(HealthHistoryAccounting):
    product: str
    context: str
    instance: str


def summarize_observations(
    records: Iterable[PublicIngressObservationRecord | HistoryObservation],
    *,
    since: datetime,
    until: datetime,
    instance: str,
    required_checks: tuple[tuple[PublicIngressCheckKind, str], ...],
    identity_checks: tuple[tuple[PublicIngressCheckKind, str], ...],
    excluded_checks: tuple[tuple[PublicIngressCheckKind, str], ...] = (),
) -> HealthHistoryAccounting:
    """A lower bound on verified time for each stream, not a physical uptime claim."""
    since, until = _window(since, until)
    denominator = (until - since).total_seconds()
    if (
        not required_checks
        or len(set(required_checks)) != len(required_checks)
        or any(not name.strip() for _, name in required_checks)
        or not identity_checks
        or len(set(identity_checks)) != len(identity_checks)
        or not set(identity_checks) <= set(required_checks)
        or len(set(excluded_checks)) != len(excluded_checks)
        or set(required_checks) & set(excluded_checks)
    ):
        raise ValueError(
            "Supply distinct required checks and their nonempty identity-check subset."
        )
    streams: dict[tuple[PublicIngressCheckKind, str], list[HistoryObservation]] = defaultdict(list)
    seen = set()
    for record in records:
        item = (
            project_observation(record)
            if isinstance(record, PublicIngressObservationRecord)
            else record
        )
        if item.record_id in seen:
            raise ValueError("Duplicate history observation.")
        seen.add(item.record_id)
        if item.observed_at < until:
            streams[(item.check_kind, item.check_name)].append(item)
    unlisted = set(streams) - set(required_checks) - set(excluded_checks)
    if unlisted:
        raise ValueError(
            "Account for every observed stream with --check or --exclude-check: "
            + ", ".join(f"{kind}:{name}" for kind, name in sorted(unlisted))
        )
    results = []
    # End events precede start events at the same instant; a new observation
    # replaces expiry without manufacturing a gap or overwriting the new state.
    events: dict[datetime, list[tuple[int, tuple[PublicIngressCheckKind, str], str, bool]]] = (
        defaultdict(list)
    )
    for kind, name in sorted(set(streams) | set(required_checks)):
        observations = streams.get((kind, name), [])
        observations.sort(key=lambda item: (item.observed_at, item.record_id))
        passing = failing = verified = both = 0.0
        freshness = (
            PUBLIC_TLS_STALE_AFTER_SECONDS
            if kind == "tls"
            else health_monitor_freshness_seconds(instance)
        )
        groups = [
            (time, tuple(items))
            for time, items in groupby(observations, key=lambda item: item.observed_at)
        ]
        for index, (time, items) in enumerate(groups):
            next_time = groups[index + 1][0] if index + 1 < len(groups) else until
            end = min(until, next_time, time + timedelta(seconds=freshness))
            seconds = max(0.0, (end - max(since, time)).total_seconds())
            # Equal-time records have no causal ordering. Never let record-id
            # spelling choose a pass over conflicting evidence.
            if any(item.purpose != "probe" for item in items):
                continue
            passed = all(item.status == "pass" for item in items)
            if passed:
                passing += seconds
            elif any(item.status == "fail" for item in items):
                failing += seconds
            identity_matches = all(
                item.expected_runtime_identity is not None
                and any(target.runtime_identity_status == "match" for target in item.targets)
                and all(
                    target.runtime_identity_status
                    not in {"mismatch", "malformed", "unverifiable", "missing"}
                    for target in item.targets
                )
                for item in items
            )
            if identity_matches:
                verified += seconds
                if passed:
                    both += seconds
            start = max(since, time)
            if seconds and (kind, name) in required_checks:
                state = (
                    "pass"
                    if passed
                    else "fail"
                    if any(item.status == "fail" for item in items)
                    else "unknown"
                )
                events[start].append((1, (kind, name), state, identity_matches))
                events[end].append((0, (kind, name), "unknown", False))
        results.append(
            CheckHistoryAccounting(
                check_name=name,
                check_kind=kind,
                observation_count=len(observations),
                health_pass_seconds=passing,
                health_fail_seconds=failing,
                health_unknown_seconds=denominator - passing - failing,
                identity_verified_seconds=verified,
                health_and_identity_seconds=both,
                health_pass_percent=100 * passing / denominator,
                health_and_identity_percent=100 * both / denominator,
            )
        )
    states = {key: ("unknown", False) for key in required_checks}
    lane_pass = lane_fail = lane_identity = lane_both = 0.0
    previous = since
    for time in sorted({*events, until}):
        seconds = (time - previous).total_seconds()
        passed = all(state[0] == "pass" for state in states.values())
        if passed:
            lane_pass += seconds
        elif any(state[0] == "fail" for state in states.values()):
            lane_fail += seconds
        identity = all(states[key][1] for key in identity_checks)
        if identity:
            lane_identity += seconds
            if passed:
                lane_both += seconds
        for _, key, state, identity in sorted(events.get(time, [])):
            states[key] = (state, identity)
        previous = time
    return HealthHistoryAccounting(
        since=since,
        until=until,
        denominator_seconds=denominator,
        checks=tuple(results),
        required_checks=required_checks,
        identity_checks=identity_checks,
        excluded_checks=excluded_checks,
        missing_checks=tuple(
            (check.check_kind, check.check_name)
            for check in results
            if (check.check_kind, check.check_name) in required_checks
            and check.health_pass_seconds + check.health_fail_seconds == 0
        ),
        lane_health_pass_seconds=lane_pass,
        lane_health_fail_seconds=lane_fail,
        lane_health_unknown_seconds=denominator - lane_pass - lane_fail,
        lane_identity_verified_seconds=lane_identity,
        lane_health_and_identity_seconds=lane_both,
        lane_health_pass_percent=100 * lane_pass / denominator,
        lane_health_and_identity_percent=100 * lane_both / denominator,
    )


def summarize_history_pages(
    pages: Iterable[LaneHealthHistory],
    *,
    required_checks: tuple[tuple[PublicIngressCheckKind, str], ...],
    identity_checks: tuple[tuple[PublicIngressCheckKind, str], ...],
    excluded_checks: tuple[tuple[PublicIngressCheckKind, str], ...] = (),
) -> LaneHealthHistoryAccounting:
    """Refuse truncated, mixed or skipped pages before computing percentages."""
    first = None
    expected_cursor = None
    observations: list[HistoryObservation] = []
    for page in pages:
        if first is not None and expected_cursor is None:
            raise ValueError("Unexpected page after the end of history.")
        if first is None:
            first = page
        if page.cursor != expected_cursor or (
            page.product,
            page.context,
            page.instance,
            page.since,
            page.until,
        ) != (first.product, first.context, first.instance, first.since, first.until):
            raise ValueError("History pages must cover one lane/window in cursor order.")
        observations.extend(page.observations)
        expected_cursor = page.next_cursor
    if first is None or expected_cursor is not None:
        raise ValueError("Complete history pages are required; fetch the next cursor.")
    accounting = summarize_observations(
        observations,
        since=first.since,
        until=first.until,
        instance=first.instance,
        required_checks=required_checks,
        identity_checks=identity_checks,
        excluded_checks=excluded_checks,
    )
    return LaneHealthHistoryAccounting(
        **accounting.model_dump(),
        product=first.product,
        context=first.context,
        instance=first.instance,
    )
