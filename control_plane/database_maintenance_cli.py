import argparse
import json
import os
import signal
from threading import Event

from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.database_maintenance import (
    MAINTENANCE_LOCK,
    DatabaseMaintenanceHeld,
    _hold,
    maintenance_window,
)
from control_plane.storage.postgres import _build_engine


def status(engine: Engine) -> dict[str, object]:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        record = _hold(connection)
        available = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock_shared(hashtextextended(:key, 0))"),
                {"key": MAINTENANCE_LOCK},
            )
        )
        if available:
            connection.execute(
                text("SELECT pg_advisory_unlock_shared(hashtextextended(:key, 0))"),
                {"key": MAINTENANCE_LOCK},
            )
        return {"record": record, "exclusive_gate_observed": not available}


def release(engine: Engine, hold_id: str) -> None:
    from control_plane.database_maintenance import MAINTENANCE_RECORD
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(LaunchplaneServiceDeployDrainRow)
            .where(LaunchplaneServiceDeployDrainRow.record_id == MAINTENANCE_RECORD)
            .with_for_update()
        )
        if row is None or row.payload.get("hold_id") != hold_id:
            raise DatabaseMaintenanceHeld("The requested maintenance hold does not match.")
        if row.payload["state"] != "released":
            row.payload = {**row.payload, "state": "release_requested"}


def _wait_for_release(engine: Engine, receipt: dict[str, object], stop: Event) -> None:
    while not stop.wait(0.5):
        with engine.connect() as observer:
            current = _hold(observer)
        if current is None or current["hold_id"] != receipt["hold_id"]:
            raise DatabaseMaintenanceHeld("Maintenance receipt changed unexpectedly.")
        if current["state"] == "release_requested":
            return
        if not status(engine)["exclusive_gate_observed"]:
            raise DatabaseMaintenanceHeld("Maintenance gate is no longer held.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Launchplane database maintenance hold.")
    parser.add_argument("mode", choices=("status", "plan", "hold", "release"))
    parser.add_argument("--hold-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--target-id", default="")
    parser.add_argument("--repository", default="")
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--drain-seconds", type=int, default=60)
    arguments = parser.parse_args()
    if arguments.mode in {"plan", "hold"} and (
        not 1 <= arguments.drain_seconds < arguments.duration_seconds <= 3600
        or not arguments.reason.strip()
        or not arguments.target_id
        or len(arguments.repository.split("/")) != 2
    ):
        parser.error(
            "hold requires a reason, target, repository and 1 <= drain < duration <= 3600."
        )
    database_url = os.environ.get("LAUNCHPLANE_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("LAUNCHPLANE_DATABASE_URL is required in place.")
    engine = _build_engine(
        database_url,
        postgres_connect_timeout_seconds=5,
        postgres_statement_timeout_milliseconds=5000,
        unpooled=True,
    )
    try:
        if arguments.mode == "status":
            print(json.dumps(status(engine)), flush=True)
            return 0
        if arguments.mode == "release":
            if not arguments.hold_id:
                parser.error("release requires --hold-id.")
            release(engine, arguments.hold_id)
            print(json.dumps(status(engine)), flush=True)
            return 0
        stop = Event()
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            from control_plane.database_maintenance_deploy import deployment_window
            from pathlib import Path
            from datetime import timedelta

            if not arguments.target_id:
                parser.error("hold requires the explicit Launchplane --target-id.")
            with engine.connect() as clock:
                expires = clock.scalar(text("SELECT clock_timestamp()")) + timedelta(
                    seconds=arguments.duration_seconds
                )
            with deployment_window(
                control_plane_root=Path(__file__).resolve().parents[1],
                target_id=arguments.target_id,
                repository=arguments.repository,
                expires_at=expires.isoformat(),
                apply=arguments.mode == "hold",
            ):
                if arguments.mode == "plan":
                    print(
                        json.dumps(
                            {
                                "state": "plan_ready",
                                "duration_seconds": arguments.duration_seconds,
                                **status(engine),
                            }
                        ),
                        flush=True,
                    )
                    return 0
                with engine.connect() as clock:
                    remaining = int(
                        (expires - clock.scalar(text("SELECT clock_timestamp()"))).total_seconds()
                    )
                with maintenance_window(
                    engine,
                    duration_seconds=remaining,
                    drain_seconds=arguments.drain_seconds,
                    reason=arguments.reason,
                ) as receipt:
                    print(json.dumps(receipt), flush=True)
                    _wait_for_release(engine, receipt, stop)

        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        print(json.dumps(status(engine)), flush=True)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
