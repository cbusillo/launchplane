import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import sys
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from control_plane.database_maintenance import (
    apply_retirement,
    database_snapshot,
    locked_database,
    retained_snapshot,
)
from control_plane.deployment_maintenance import DeploymentMaintenance
from control_plane.storage.postgres import _build_engine


def in_flight_work(connection: Connection) -> dict[str, int]:
    observed: dict[str, int] = {}
    now = connection.scalar(text("SELECT clock_timestamp()"))
    inspector = inspect(connection)
    for table in inspector.get_table_names(schema="public"):
        quoted = connection.dialect.identifier_preparer.quote(table)
        for column in inspector.get_columns(table):
            name = str(column["name"])
            quoted_column = connection.dialect.identifier_preparer.quote(name)
            if name in {"state", "status"}:
                count = connection.scalar(
                    text(
                        f"SELECT count(*) FROM {quoted} WHERE {quoted_column} IN "
                        "('running','executing','dispatching','processing','claimed')"
                    )
                )
                if count:
                    observed[f"{table}.{name}"] = int(count)
            elif "lease" in name and any(part in name for part in ("expires", "deadline", "until")):
                count = 0
                for (value,) in connection.execute(
                    text(f"SELECT {quoted_column} FROM {quoted} WHERE {quoted_column} IS NOT NULL")
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


def await_command(expires: datetime, observe: Any) -> dict[str, Any]:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launchplane's bounded stopped-service database keeper."
    )
    parser.add_argument(
        "mode", choices=("plan", "fence", "unfence", "census", "hold", "snapshot", "rehearse")
    )
    parser.add_argument("--before-file", type=Path)
    parser.add_argument("--backup-sha256", default="")
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--hold-id", required=True)
    arguments = parser.parse_args()
    expires = datetime.fromisoformat(arguments.expires_at)
    if expires.tzinfo is None or (
        arguments.mode != "unfence"
        and not 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 3600
    ):
        parser.error("The deadline must be within one hour and include a timezone.")
    url = os.environ["LAUNCHPLANE_DATABASE_URL"]
    engine = _build_engine(
        url,
        postgres_connect_timeout_seconds=5,
        postgres_statement_timeout_milliseconds=300000,
        unpooled=True,
    )
    try:
        with engine.connect() as connection:
            if arguments.mode == "rehearse":
                from control_plane.storage.schema_migration import migrate_schema_on_connection
                from control_plane.storage.schema_invariants import (
                    verify_postgres_schema_invariants,
                )
                from control_plane.storage.migrations.versions import (
                    e7c1d5e9a3b6_drop_retired_delivery_tables as retirement,
                )

                if arguments.before_file is None:
                    raise ValueError("The held-snapshot receipt is required.")
                held = json.loads(arguments.before_file.read_text())
                restored = database_snapshot(connection)
                if restored != held["before"]:
                    raise RuntimeError(
                        "The actual restored backup does not match the held database."
                    )
                connection.rollback()
                with locked_database(connection, expires_at=expires):
                    revision = migrate_schema_on_connection(
                        connection, target_revision=retirement.revision
                    )
                    after = database_snapshot(connection)
                    if retained_snapshot(after) != retained_snapshot(restored):
                        raise RuntimeError("The isolated rehearsal changed retained objects.")
                verify_postgres_schema_invariants(engine)
                emit(
                    "rehearsed",
                    hold_id=held["hold_id"],
                    snapshot=held["snapshot"],
                    transaction=held["transaction"],
                    backup_sha256=arguments.backup_sha256,
                    restored=restored,
                    retained=retained_snapshot(after),
                    revision=revision,
                    schema_invariants_verified=True,
                    action="apply",
                )
                return 0
            if arguments.mode == "snapshot":
                emit("snapshot", snapshot=database_snapshot(connection))
                return 0
            if arguments.mode == "census":
                blockers = in_flight_work(connection)
                emit("census", blockers=blockers)
                return 0
            row = connection.execute(
                text(
                    "SELECT payload->>'target_id', payload->>'target_type' FROM launchplane_service_deploy_drains WHERE record_id='service'"
                )
            ).one()
            if row[1] != "compose" or not row[0]:
                raise ValueError("The current own-service target authority is missing.")
            deployment = DeploymentMaintenance(
                target_id=str(row[0]),
                database_url=url,
                image=os.environ.get("DOCKER_IMAGE_REFERENCE", ""),
                root=Path("/app"),
            )
            connection.commit()
            if not connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock(hashtextextended('launchplane:service-deploy-provider-dispatch',0))"
                )
            ):
                raise RuntimeError("An admitted self-deploy still owns dispatch.")
            connection.commit()
            if arguments.mode == "unfence":
                deployment.marker(arguments.expires_at, remove=True)
                emit("deployment_released")
                return 0
            deployment.observe(deadline=arguments.expires_at if arguments.mode == "hold" else None)
            if arguments.mode == "fence":
                deployment.marker(arguments.expires_at)
                deployment.observe(deadline=arguments.expires_at)
                emit("deployment_held", repository=deployment.repository)
                return 0
            if arguments.mode == "plan":
                emit(
                    "plan",
                    database=str(connection.engine.url.database),
                    server_version=connection.dialect.server_version_info,
                    system_identifier=str(
                        connection.scalar(text("SELECT system_identifier FROM pg_control_system()"))
                    ),
                )
                return 0
            identity = connection.execute(
                text(
                    "SELECT pg_backend_pid(), backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
                )
            ).one()
            connection.commit()
            emit("keeper", pid=identity[0], backend_start=identity[1].isoformat())
            with locked_database(connection, expires_at=expires):
                blockers = in_flight_work(connection)
                if blockers:
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
                    before=before,
                )

                def observe() -> None:
                    deployment.observe(deadline=arguments.expires_at)
                    from control_plane.database_maintenance import require_no_other_clients

                    require_no_other_clients(connection, allow_dump=True)

                receipt = await_command(expires, observe)
                if receipt.get("action") == "apply":
                    if (
                        receipt.get("hold_id") != arguments.hold_id
                        or receipt.get("snapshot") != snapshot
                        or receipt.get("transaction") != transaction
                    ):
                        raise ValueError(
                            "The rehearsal receipt belongs to another maintenance snapshot."
                        )
                    revision = apply_retirement(connection, before=before, rehearsal=receipt)
                    emit("verified_before_commit", revision=revision)
                elif receipt.get("action") != "release":
                    raise ValueError("Only an exact rehearsal or release command is accepted.")
            from control_plane.storage.schema_invariants import verify_postgres_schema_invariants

            verify_postgres_schema_invariants(engine)
            emit(
                "database_released",
                revision=str(connection.scalar(text("SELECT version_num FROM alembic_version"))),
            )
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
