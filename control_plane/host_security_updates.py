from collections.abc import Callable
from datetime import datetime, timezone
import inspect
import math
from pathlib import Path
import time
from typing import TYPE_CHECKING

import click

from control_plane.contracts.host_security_updates import (
    HostSecurityUpdateEvidence,
    HOST_SECURITY_UPDATE_CHECK,
    SECURITY_UPDATE_COMPLETION_SECONDS,
    SECURITY_UPDATE_MAX_AGE_SECONDS,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    build_public_ingress_observation_id,
    health_monitor_freshness_seconds,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.dokploy import api, native_copy_stream
from control_plane.host_network_capacity import read_bound_lane_host
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore


# https://sources.debian.org/src/apt/3.0.3/debian/apt.systemd.daily/#L493


def evaluate_security_update_stamps(
    *, observed_at: float, updater_result_at: float | None, completed_at: float | None
) -> HostSecurityUpdateEvidence:
    if completed_at is None:
        return HostSecurityUpdateEvidence(status="missing", observed_at=observed_at)
    if (
        updater_result_at is None
        or not all(
            math.isfinite(value) and 0 <= value <= observed_at
            for value in (completed_at, updater_result_at)
        )
        or not 0 <= completed_at - updater_result_at <= SECURITY_UPDATE_COMPLETION_SECONDS
    ):
        return HostSecurityUpdateEvidence(observed_at=observed_at)
    return HostSecurityUpdateEvidence(
        status="fresh"
        if observed_at - completed_at <= SECURITY_UPDATE_MAX_AGE_SECONDS
        else "stale",
        observed_at=observed_at,
        completed_at=completed_at,
    )


def read_security_update_stamps(*, root: Path = Path("/")) -> HostSecurityUpdateEvidence:
    def timestamp(name: str) -> float | None:
        try:
            return (root / "var/lib/apt/periodic" / name).stat().st_mtime
        except FileNotFoundError:
            return None

    now = time.time()
    try:
        return evaluate_security_update_stamps(
            observed_at=now,
            updater_result_at=timestamp("unattended-upgrades-stamp"),
            completed_at=timestamp("upgrade-stamp"),
        )
    except OSError:
        return HostSecurityUpdateEvidence(observed_at=now)


SECURITY_UPDATE_PROGRAM = (
    "import json,math,sys,time\nfrom pathlib import Path\n"
    "from typing import NamedTuple\n"
    f"SECURITY_UPDATE_COMPLETION_SECONDS={SECURITY_UPDATE_COMPLETION_SECONDS}\n"
    f"SECURITY_UPDATE_MAX_AGE_SECONDS={SECURITY_UPDATE_MAX_AGE_SECONDS}\n"
    "class HostSecurityUpdateEvidence(NamedTuple):\n"
    '    observed_at: float\n    status: str = "unknown"\n    completed_at: float | None = None\n'
    + inspect.getsource(evaluate_security_update_stamps)
    + "\n"
    + inspect.getsource(read_security_update_stamps)
    + '\nprint(sys.argv[2]+json.dumps({"type":"complete","result":read_security_update_stamps()._asdict()}),flush=True)\n'
)


def read_host_security_updates(
    *, host: str, token: str, server_id: str
) -> HostSecurityUpdateEvidence:
    now = time.time()
    frames: list[dict[str, object]] = []
    try:
        native_copy_stream._read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=SECURITY_UPDATE_PROGRAM,
            payload={},
            receive=frames.append,
            timeout_seconds=20,
        )
        if len(frames) != 1 or frames[0].get("type") != "complete":
            raise ValueError("Incomplete security-update observation.")
        evidence = HostSecurityUpdateEvidence.model_validate(frames[0].get("result"))
        if abs(evidence.observed_at - time.time()) > SECURITY_UPDATE_COMPLETION_SECONDS:
            raise ValueError("Host clock differs from the observation clock.")
        return evidence
    except (
        ValueError,
        OSError,
        native_copy_stream.NativeSourceUnavailable,
        native_copy_stream.NativeSourceAuthorizationDenied,
    ):
        return HostSecurityUpdateEvidence(observed_at=now)


def collect_host_security_updates(
    store: "PostgresRecordStore",
    *,
    lanes: frozenset[tuple[str, str, str]],
    should_stop: Callable[[], bool],
) -> None:
    observations: dict[str, HostSecurityUpdateEvidence] = {}

    def read(host: str, token: str, server_id: str) -> HostSecurityUpdateEvidence:
        if server_id not in observations:
            observations[server_id] = read_host_security_updates(
                host=host, token=token, server_id=server_id
            )
        return observations[server_id]

    for profile in store.list_product_profile_records():
        if not profile.is_active or profile.production_use != "live":
            continue
        for lane in profile.lanes:
            if (
                lane.instance != "prod"
                or (profile.product, lane.context, lane.instance) not in lanes
            ):
                continue
            if should_stop():
                return
            evidence = HostSecurityUpdateEvidence(observed_at=time.time())
            try:
                bound = read_bound_lane_host(
                    root=Path(__file__).parent.parent,
                    store=store,
                    context=lane.context,
                    instance=lane.instance,
                    read=read,
                )
                evidence = bound.observation.model_copy(
                    update={
                        "server_id": bound.server_id,
                        "provider_target_sha256": bound.target_sha256,
                    }
                )
            except (
                ValueError,
                OSError,
                click.ClickException,
                api.DokployRequestFailed,
                native_copy_stream.NativeSourceUnavailable,
                native_copy_stream.NativeSourceAuthorizationDenied,
            ):
                evidence = HostSecurityUpdateEvidence(observed_at=time.time())
            stamp = datetime.fromtimestamp(evidence.observed_at, timezone.utc).isoformat()
            store.write_public_ingress_observation_record(
                PublicIngressObservationRecord(
                    schema_version=2,
                    record_id=build_public_ingress_observation_id(
                        product=profile.product,
                        context=lane.context,
                        instance=lane.instance,
                        observed_at=stamp,
                        check_name=HOST_SECURITY_UPDATE_CHECK,
                    ),
                    product=profile.product,
                    repository=profile.repository,
                    driver_id=profile.driver_id,
                    context=lane.context,
                    instance=lane.instance,
                    check_name=HOST_SECURITY_UPDATE_CHECK,
                    check_kind="provider",
                    monitoring_intent=lane.health_monitoring.monitoring_intent,
                    observed_at=stamp,
                    status="pass" if evidence.status == "fresh" else "fail",
                    failure_code=None
                    if evidence.status == "fresh"
                    else "provider_check_unavailable",
                    targets=(
                        PublicIngressTargetObservation(
                            target="provider",
                            url="provider://dokploy/host-security-updates",
                            status="pass" if evidence.status == "fresh" else "fail",
                            failure_code=None
                            if evidence.status == "fresh"
                            else "provider_check_unavailable",
                            summary="Production host security-update observation.",
                        ),
                    ),
                    summary="Production host security-update observation.",
                    host_security_updates=evidence,
                )
            )


def host_security_update_warnings(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    if lane.instance != "prod" or profile.production_use != "live":
        return ()
    read = getattr(record_store, "list_public_ingress_observation_records", None)
    current_time = time.time() if now is None else now
    evidence = None
    try:
        records = (
            read(
                product=profile.product,
                context_name=lane.context,
                instance_name=lane.instance,
                check_name=HOST_SECURITY_UPDATE_CHECK,
                check_kind="provider",
                limit=1,
            )
            if callable(read)
            else ()
        )
        if records:
            evidence = records[0].host_security_updates
        target_reader = getattr(record_store, "read_provider_target_record", None)
        target = (
            target_reader(context_name=lane.context, instance_name=lane.instance)
            if callable(target_reader)
            else None
        )
        if (
            evidence is not None
            and evidence.status in {"fresh", "stale"}
            and evidence.completed_at is not None
            and 0
            <= current_time - evidence.observed_at
            <= health_monitor_freshness_seconds(lane.instance)
            and 0 <= current_time - evidence.completed_at <= SECURITY_UPDATE_MAX_AGE_SECONDS
            and target is not None
            and evidence.provider_target_sha256
            == nonvalidating_compact_sorted_json_sha256(target.model_dump(mode="json"))
        ):
            return ()
    except (ValueError, OSError):
        evidence = None
    if evidence is not None and evidence.status == "missing":
        return ("A production host has no recorded successful security-update run.",)
    if (
        evidence is not None
        and evidence.completed_at is not None
        and current_time - evidence.completed_at > SECURITY_UPDATE_MAX_AGE_SECONDS
    ):
        return ("A production host has not completed security updates in more than two days.",)
    return ("Production host security-update completion could not be verified.",)
