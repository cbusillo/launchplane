from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
import math
from typing import TYPE_CHECKING, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.dokploy import api, native_copy_stream
from control_plane.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore

MAX_UPDATE_AGE_SECONDS = 2 * 24 * 60 * 60
OBSERVATION_INTERVAL_SECONDS = 60 * 60
OBSERVATION_VALID_SECONDS = 2 * OBSERVATION_INTERVAL_SECONDS
SCOPE = "launchplane:host-security-updates"
ROUTE = "service:host-security-updates"

# https://sources.debian.org/src/apt/3.0.3/debian/apt.systemd.daily/#L493
SECURITY_UPDATE_PROGRAM = """import json,sys,time
from pathlib import Path
try:
    completed_at = Path('/var/lib/apt/periodic/upgrade-stamp').stat().st_mtime
except FileNotFoundError:
    completed_at = None
print(sys.argv[2]+json.dumps({'type':'complete','result': {
    'age_seconds': None if completed_at is None else time.time() - completed_at}}),flush=True)
"""


class HostSecurityUpdateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    completed_at: float | None = Field(default=None, ge=0)
    status: Literal["observed", "unknown", "denied"] = "observed"
    target_sha256: str = ""


def read_host_security_updates(host: str, token: str, server_id: str) -> HostSecurityUpdateEvidence:
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
        result = frames[0].get("result")
        if not isinstance(result, dict) or "age_seconds" not in result:
            raise ValueError("Missing security-update age.")
        age = result["age_seconds"]
        now = time.time()
        if age is not None and (
            not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= now
        ):
            raise ValueError("Invalid security-update age.")
        return HostSecurityUpdateEvidence(completed_at=None if age is None else now - age)
    except native_copy_stream.NativeSourceAuthorizationDenied:
        return HostSecurityUpdateEvidence(status="denied")
    except (ValueError, OSError, native_copy_stream.NativeSourceUnavailable):
        return HostSecurityUpdateEvidence(status="unknown")


def collect_host_security_updates(
    store: PostgresRecordStore, *, should_stop: Callable[[], bool]
) -> None:
    from control_plane.host_network_capacity import read_bound_lane_host

    hosts: dict[tuple[str, str], HostSecurityUpdateEvidence] = {}

    def read(host: str, token: str, server_id: str) -> HostSecurityUpdateEvidence:
        key = (host, server_id)
        if key not in hosts:
            hosts[key] = read_host_security_updates(host, token, server_id)
        return hosts[key]

    for profile in store.list_product_profile_records():
        if not profile.is_active or profile.production_use != "live":
            continue
        for lane in profile.lanes:
            if lane.instance != "prod":
                continue
            if should_stop():
                return
            target_sha256 = ""
            try:
                target = store.read_provider_target_record(
                    context_name=lane.context, instance_name=lane.instance
                )
                target_sha256 = host_target_sha256(target)
                previous = read_recent_host_update(
                    store, lane, now=time.time(), max_age=OBSERVATION_INTERVAL_SECONDS
                )
                if previous is not None and previous.target_sha256 == target_sha256:
                    continue
                bound = read_bound_lane_host(
                    root=Path(__file__).parent.parent,
                    store=store,
                    context=lane.context,
                    instance=lane.instance,
                    read=read,
                )
                evidence = bound.observation
                target_sha256 = host_target_sha256(bound.target)
            except api.DokployRequestFailed as error:
                evidence = HostSecurityUpdateEvidence(
                    status="denied" if error.status_code in {401, 403} else "unknown"
                )
            except (ValueError, OSError, click.ClickException):
                evidence = HostSecurityUpdateEvidence(status="unknown")
            evidence = evidence.model_copy(update={"target_sha256": target_sha256})
            key = f"{lane.context}:{lane.instance}"
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_mutation_reservation_id(
                        scope=SCOPE, route_path=ROUTE, idempotency_key=key
                    ),
                    scope=SCOPE,
                    route_path=ROUTE,
                    idempotency_key=key,
                    request_fingerprint=SCOPE,
                    response_status_code=200,
                    response_trace_id=key,
                    recorded_at=utc_now_timestamp(),
                    response_payload=evidence.model_dump(mode="json"),
                )
            )


def host_security_update_warnings(
    store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    if lane.instance != "prod" or profile.production_use != "live":
        return ()
    current = time.time() if now is None else now
    warning = "Production host security-update completion could not be verified."
    try:
        evidence = read_recent_host_update(
            store,
            lane,
            now=current,
            max_age=OBSERVATION_VALID_SECONDS,
        )
        if evidence is None:
            return (warning,)
        if evidence.status == "denied":
            return ("The provider denied the production host security-update read.",)
        target = getattr(store, "read_provider_target_record")(
            context_name=lane.context, instance_name=lane.instance
        )
        if evidence.target_sha256 != host_target_sha256(target):
            return (warning,)
        if evidence.status == "observed":
            if evidence.completed_at is None:
                warning = "A production host has no recorded successful security-update run."
            elif 0 <= current - evidence.completed_at <= MAX_UPDATE_AGE_SECONDS:
                return ()
            elif evidence.completed_at < current:
                warning = (
                    "A production host has not completed security updates in more than two days."
                )
    except (ValueError, OSError, AttributeError):
        return (warning,)
    return (warning,)


def read_recent_host_update(
    store: object, lane: ProductLaneProfile, *, now: float, max_age: float
) -> HostSecurityUpdateEvidence | None:
    reader = getattr(store, "read_idempotency_record", None)
    if not callable(reader):
        return None
    try:
        receipt = reader(
            scope=SCOPE, route_path=ROUTE, idempotency_key=f"{lane.context}:{lane.instance}"
        )
        if (
            receipt is not None
            and receipt.state == "completed"
            and 0
            <= now
            - parse_launchplane_mutation_timestamp(
                receipt.recorded_at, field_name="recorded_at"
            ).timestamp()
            <= max_age
        ):
            return HostSecurityUpdateEvidence.model_validate(receipt.response_payload)
    except (ValueError, OSError):
        return None
    return None


def host_target_sha256(target: ProviderTargetRecord) -> str:
    return nonvalidating_compact_sorted_json_sha256(
        target.model_dump(include={"provider_id", "target_category", "target_id"})
    )
