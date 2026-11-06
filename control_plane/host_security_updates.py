from collections.abc import Callable
import inspect
import math
from pathlib import Path
import time
from typing import TYPE_CHECKING

import click

from control_plane.contracts.host_security_updates import (
    HostSecurityUpdateEvidence,
    HostSecurityUpdateObservation,
    SECURITY_UPDATE_COMPLETION_SECONDS,
)
from control_plane.dokploy import api, native_copy_stream
from control_plane.host_network_capacity import read_bound_lane_host

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
        return HostSecurityUpdateEvidence(
            observed_at=observed_at,
            completed_at=completed_at
            if completed_at <= observed_at and math.isfinite(completed_at)
            else None,
        )
    return HostSecurityUpdateEvidence(
        status="observed",
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
    except native_copy_stream.NativeSourceAuthorizationDenied:
        return HostSecurityUpdateEvidence(status="denied", observed_at=now)
    except (ValueError, OSError, native_copy_stream.NativeSourceUnavailable):
        return HostSecurityUpdateEvidence(observed_at=now)


def collect_host_security_updates(
    store: "PostgresRecordStore",
    *,
    lanes: frozenset[tuple[str, str, str]] | None = None,
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
            if lane.instance != "prod" or (
                lanes is not None and (profile.product, lane.context, lane.instance) not in lanes
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
            except native_copy_stream.NativeSourceAuthorizationDenied:
                evidence = HostSecurityUpdateEvidence(status="denied", observed_at=time.time())
            except api.DokployRequestFailed as error:
                evidence = HostSecurityUpdateEvidence(
                    status="denied" if error.status_code in {401, 403} else "unknown",
                    observed_at=time.time(),
                )
            except (
                ValueError,
                OSError,
                click.ClickException,
                native_copy_stream.NativeSourceUnavailable,
            ):
                evidence = HostSecurityUpdateEvidence(observed_at=time.time())
            store.write_host_security_update_observation(
                HostSecurityUpdateObservation(
                    context=lane.context,
                    instance=lane.instance,
                    evidence=evidence,
                )
            )
