import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import signal
import sys
from typing import Any
from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from control_plane.database_maintenance import (
    apply_retirement,
    database_snapshot,
    database_system_identifier,
    require_isolated_rehearsal,
    require_no_other_clients,
    locked_database,
    retained_snapshot,
)
from control_plane.deployment_maintenance import DeploymentMaintenance
from control_plane.storage.postgres import _build_engine
from control_plane.storage.schema_invariants import verify_postgres_schema_invariants


def in_flight_work(connection: Connection) -> dict[str, int]:
    from control_plane.storage.postgres import LaunchplaneMergeTrainControllerStateRow
    from control_plane.storage.migrations.versions import (
        e7c1d5e9a3b6_drop_retired_delivery_tables as retirement,
    )

    observed: dict[str, int] = {}
    now = connection.scalar(text("SELECT clock_timestamp()"))
    inspector = inspect(connection)
    for table in inspector.get_table_names(schema="public"):
        if (
            table in retirement.RETIRED_TABLES
            or table == LaunchplaneMergeTrainControllerStateRow.__tablename__
        ):
            continue
        columns = inspector.get_columns(table, schema="public")
        state_columns = [
            str(column["name"]) for column in columns if column["name"] in {"state", "status"}
        ]
        if not state_columns:
            continue
        quote = connection.dialect.identifier_preparer.quote
        active = " OR ".join(
            quote(name) + "::text IN ('running','executing','dispatching','processing','claimed')"
            for name in state_columns
        )
        for column in columns:
            name = str(column["name"])
            if "lease" not in name or not any(
                part in name for part in ("expires", "deadline", "until")
            ):
                continue
            count = 0
            for (value,) in connection.execute(
                text(
                    f"SELECT {quote(name)} FROM public.{quote(table)} WHERE ({active}) AND {quote(name)} IS NOT NULL"
                )
            ):
                if value in ("", 0):
                    continue
                try:
                    expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        raise ValueError("Missing timezone.")
                except ValueError:
                    try:
                        expiry = datetime.fromtimestamp(float(value), timezone.utc)
                    except (TypeError, ValueError, OverflowError):
                        raise RuntimeError(
                            "An operation lease expiry could not be qualified."
                        ) from None
                count += expiry > now
            if count:
                observed[f"{table}.{name}"] = count
    return observed


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def await_command(expires: datetime, observe: Callable[[], None]) -> dict[str, Any]:
    while datetime.now(timezone.utc) < expires:
        observe()
        ready, _, _ = select.select(
            [sys.stdin],
            [],
            [],
            min(5, max(0, (expires - datetime.now(timezone.utc)).total_seconds())),
        )
        if ready:
            line = sys.stdin.readline()
            if not line:
                raise EOFError("Maintenance controller input closed; roll back.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("A maintenance command must be an object.")
            return value
    raise TimeoutError("Maintenance deadline reached; roll back and release.")


def require_isolated_runtime(connection: Connection) -> None:
    from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS

    if connection.engine.url.host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("The rehearsal database must use isolated loopback.")
    if any(os.environ.get(key, "").strip() for key in PLATFORM_CREDENTIAL_KEYS):
        raise RuntimeError("The rehearsal must have no production keyring or provider credentials.")
    if sys.platform != "linux" or {path.name for path in Path("/sys/class/net").iterdir()} != {
        "lo"
    }:
        raise RuntimeError("The rehearsal requires a loopback-only network namespace.")


def rehearsal_receipt(
    connection: Connection,
    *,
    held: dict[str, Any],
    backup_sha256: str,
    expires: datetime,
) -> dict[str, Any]:
    from control_plane.storage.schema_migration import (
        SCHEMA_MIGRATION_TARGET_REVISION,
        migrate_schema_on_connection,
        schema_migration_action,
    )
    from control_plane.storage.migrations.versions import (
        e7c1d5e9a3b6_drop_retired_delivery_tables as retirement,
    )

    require_isolated_runtime(connection)
    identifier = require_isolated_rehearsal(connection, held.get("system_identifier"))
    restored = database_snapshot(connection)
    if restored != held["before"]:
        raise RuntimeError("The actual restored backup does not match the held database.")
    connection.rollback()
    with locked_database(connection, expires_at=expires):
        revision = migrate_schema_on_connection(connection, target_revision=retirement.revision)
        after = database_snapshot(connection)
        if retained_snapshot(after) != retained_snapshot(restored):
            raise RuntimeError("The isolated rehearsal changed retained objects.")
    verify_postgres_schema_invariants(connection.engine)
    startup_action = schema_migration_action(
        current_revision=revision, target_revision=SCHEMA_MIGRATION_TARGET_REVISION
    )
    return {
        **{key: held[key] for key in ("hold_id", "snapshot", "transaction")},
        "system_identifier": identifier,
        "backup_sha256": backup_sha256,
        "restored": restored,
        "retained": retained_snapshot(after),
        "revision": revision,
        "startup_action": startup_action,
        "schema_invariants_verified": True,
        "action": "apply",
    }


def keep_database(
    connection: Connection,
    *,
    deployment: DeploymentMaintenance,
    arguments: argparse.Namespace,
    expires: datetime,
) -> None:
    identity = connection.execute(
        text(
            "SELECT pg_backend_pid(), backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
        )
    ).one()
    identifier = database_system_identifier(connection)
    connection.commit()
    if (arguments.job_directory / "releasing").exists():
        raise RuntimeError("The host already released this maintenance attempt.")
    with deployment.fence(
        arguments.expires_at, release_receipt=arguments.job_directory / "marker-release.json"
    ):
        if (arguments.job_directory / "releasing").exists():
            raise RuntimeError("The host already released this maintenance attempt.")
        emit(
            "deployment_held",
            target_id=deployment.target_id,
            pid=identity[0],
            backend_start=identity[1].isoformat(),
        )
        command = await_command(expires, lambda: deployment.observe(deadline=arguments.expires_at))
        if command.get("action") != "stopped" or (arguments.job_directory / "releasing").exists():
            raise RuntimeError("The host did not confirm its original services stopped.")
        with locked_database(connection, expires_at=expires):
            if in_flight_work(connection):
                raise RuntimeError(
                    "An operation is still running or leased after services stopped."
                )
            before = database_snapshot(connection)
            snapshot = str(connection.scalar(text("SELECT pg_export_snapshot()")))
            transaction = str(connection.scalar(text("SELECT txid_current()")))
            emit(
                "held",
                hold_id=arguments.hold_id,
                snapshot=snapshot,
                transaction=transaction,
                system_identifier=identifier,
                before=before,
            )

            def observe() -> None:
                deployment.observe(deadline=arguments.expires_at)
                require_no_other_clients(connection, allow_dump=True)

            receipt = await_command(expires, observe)
            if receipt.get("action") == "apply":
                if any(
                    receipt.get(key) != value
                    for key, value in (
                        ("hold_id", arguments.hold_id),
                        ("snapshot", snapshot),
                        ("transaction", transaction),
                    )
                ):
                    raise ValueError(
                        "The rehearsal receipt belongs to another maintenance snapshot."
                    )
                revision = apply_retirement(connection, before=before, rehearsal=receipt)
                emit("verified_before_commit", revision=revision)
            elif receipt.get("action") != "release":
                raise ValueError("Only an exact rehearsal or release command is accepted.")
        verify_postgres_schema_invariants(connection.engine)
        emit(
            "database_released",
            revision=str(connection.scalar(text("SELECT version_num FROM alembic_version"))),
        )
        while not (arguments.job_directory / "restarted").exists():
            if datetime.now(timezone.utc) >= expires:
                raise TimeoutError(
                    "Service restart did not complete before the maintenance deadline."
                )
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if ready and not sys.stdin.readline():
                raise EOFError("Maintenance host input closed during restart.")


def interrupted(_signum: int, _frame: object) -> None:
    raise InterruptedError("Maintenance interrupted; release its bounded hold.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launchplane's bounded stopped-service database keeper."
    )
    parser.add_argument("mode", choices=("plan", "unfence", "hold", "snapshot", "rehearse"))
    parser.add_argument("--before-file", type=Path)
    parser.add_argument("--backup-sha256", default="")
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--hold-id", required=True)
    parser.add_argument("--target-id", default="")
    parser.add_argument("--readback-only", action="store_true")
    parser.add_argument("--job-directory", type=Path, default=Path("/maintenance"))
    arguments = parser.parse_args()
    expires = datetime.fromisoformat(arguments.expires_at)
    if expires.tzinfo is None or (
        arguments.mode != "unfence"
        and not 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 3600
    ):
        parser.error("The deadline must be within one hour and include a timezone.")
    url = os.environ["LAUNCHPLANE_DATABASE_URL"]
    if arguments.mode == "unfence":
        if not arguments.target_id:
            parser.error("unfence requires the recorded own-service target identity.")
        deployment = DeploymentMaintenance(
            target_id=arguments.target_id,
            database_url=url,
            image=os.environ.get("DOCKER_IMAGE_REFERENCE", ""),
            root=Path("/app"),
        )
        if arguments.readback_only:
            from control_plane.deployment_maintenance import MAINTENANCE_UNTIL_ENV
            from control_plane.dokploy.api import parse_dokploy_env_text

            if MAINTENANCE_UNTIL_ENV in parse_dokploy_env_text(
                str(deployment.read_target().get("env") or "")
            ):
                raise RuntimeError(
                    "The original marker removal is not verified; no write was replayed."
                )
        else:
            deployment.marker(arguments.expires_at, remove=True)
        emit("deployment_released")
        return 0
    engine = _build_engine(
        url,
        postgres_connect_timeout_seconds=5,
        postgres_statement_timeout_milliseconds=300000,
        unpooled=True,
    )
    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGHUP, signal.SIGTERM)
    }
    try:
        with engine.connect() as connection:
            if arguments.mode == "rehearse":
                if arguments.before_file is None:
                    parser.error("rehearse requires the held-snapshot receipt.")
                emit(
                    "rehearsed",
                    **rehearsal_receipt(
                        connection,
                        held=json.loads(arguments.before_file.read_text()),
                        backup_sha256=arguments.backup_sha256,
                        expires=expires,
                    ),
                )
                return 0
            if arguments.mode == "snapshot":
                emit("snapshot", snapshot=database_snapshot(connection))
                return 0
            row = connection.execute(
                text(
                    "SELECT payload->>'target_id', payload->>'target_type' FROM launchplane_service_deploy_drains WHERE record_id='service'"
                )
            ).one()
            if row[1] != "compose" or not row[0]:
                raise ValueError("The current own-service target authority is missing.")
            if in_flight_work(connection):
                raise RuntimeError(
                    "An operation is running or leased; plan a later maintenance window."
                )
            deployment = DeploymentMaintenance(
                target_id=str(row[0]),
                database_url=url,
                image=os.environ.get("DOCKER_IMAGE_REFERENCE", ""),
                root=Path("/app"),
            )
            connection.commit()
            if (
                not connection.dialect.server_version_info
                or int(connection.dialect.server_version_info[0]) < 17
            ):
                raise RuntimeError(
                    "Database maintenance requires PostgreSQL transaction deadlines."
                )
            if arguments.mode == "plan":
                deployment.observe(deadline=None)
                deployment.require_idle_workflows()
                emit(
                    "plan",
                    target_id=deployment.target_id,
                    database=str(connection.engine.url.database),
                    server_version=connection.dialect.server_version_info,
                    system_identifier=database_system_identifier(connection),
                )
                return 0
            if not connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock(hashtextextended('launchplane:service-deploy-provider-dispatch',0))"
                )
            ):
                raise RuntimeError("An admitted self-deploy still owns dispatch.")
            connection.commit()
            keep_database(connection, deployment=deployment, arguments=arguments, expires=expires)
            return 0
    finally:
        engine.dispose()
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
