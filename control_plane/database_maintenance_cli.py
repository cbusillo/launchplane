import argparse
import errno
import fcntl
from ipaddress import IPv4Address, IPv6Address
import socket
import struct
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import signal
import sys
from typing import Any
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from control_plane.database_maintenance import (
    apply_retirement,
    verify_committed_database,
    database_snapshot,
    database_system_identifier,
    require_isolated_rehearsal,
    require_no_other_clients,
    require_retired_functions_absent,
    locked_database,
    maintenance_admission,
    retained_snapshot,
)
from control_plane.deployment_maintenance import DeploymentMaintenance, recovery_marker
from control_plane.database_maintenance_host import (
    locked_state,
    save,
    RecoveryRequired,
    dump_application_name,
    DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
)
from control_plane.storage.postgres import _build_engine
from control_plane.storage.schema_invariants import verify_postgres_schema_invariants


def require_no_active_executors(connection: Connection) -> None:
    from sqlalchemy import select, func
    from sqlalchemy.orm import Session
    from control_plane.service_deploy_drain import _operation_observations
    from control_plane.storage.postgres import (
        PostgresRecordStore,
        LaunchplaneOdooStableBootstrapOperationRow,
        LaunchplaneOdooProdBackupRestoreOperationRow,
        LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow,
        LaunchplanePrivilegedOperationRow,
        LaunchplaneOutboxDeliveryRow,
        LaunchplaneMergeTrainControllerStateRow,
        LaunchplaneIdempotencyRow,
    )

    store = PostgresRecordStore(
        database_url=connection.engine.url.render_as_string(hide_password=False),
        postgres_connect_timeout_seconds=5,
        postgres_statement_timeout_milliseconds=10000,
    )
    try:
        with Session(bind=connection) as session:
            now = str(connection.scalar(text("SELECT clock_timestamp()")))
            observations = _operation_observations(store, session, now)
            if any(item["blocks_drain"] for item in observations):
                raise RuntimeError("An existing provider executor has not been excluded.")
            excluded = {
                item["operation_id"]
                for item in observations
                if item.get("execution_excluded") is True
            }
            if any(
                identifier not in excluded
                for identifier in session.scalars(
                    select(LaunchplaneIdempotencyRow.record_id).where(
                        LaunchplaneIdempotencyRow.state == "running"
                    )
                )
            ):
                raise RuntimeError("An unqualified API or scheduler reservation is still running.")
            for row_type, column, active in (
                (LaunchplaneOdooStableBootstrapOperationRow, "status", ("running",)),
                (LaunchplaneOdooProdBackupRestoreOperationRow, "status", ("running",)),
                (LaunchplaneOdooProdRetainedVolumeBackupImportOperationRow, "status", ("running",)),
                (LaunchplanePrivilegedOperationRow, "status", ("executing",)),
                (LaunchplaneOutboxDeliveryRow, "state", ("running",)),
            ):
                if session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(getattr(row_type, column).in_(active))
                ):
                    raise RuntimeError("An own worker still has admitted execution work.")
            controllers = session.scalars(
                select(LaunchplaneMergeTrainControllerStateRow).where(
                    LaunchplaneMergeTrainControllerStateRow.status != "idle"
                )
            )
            for controller in controllers:
                if (controller.status, controller.active_action, controller.active_phase) != (
                    "running",
                    "merge_train_controller_run_once",
                    "select_next_action",
                ):
                    raise RuntimeError(
                        "A merge train has not reached a qualified pre-effect observation phase."
                    )
    finally:
        store.close()


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def await_command(
    expires: datetime, observe: Callable[[], None], job_directory: Path
) -> dict[str, Any]:
    while datetime.now(timezone.utc) < expires:
        if (job_directory / "releasing").exists():
            raise InterruptedError("The host is releasing this maintenance attempt.")
        observe()
        if (job_directory / "releasing").exists():
            raise InterruptedError("The host is releasing this maintenance attempt.")
        ready, _, _ = select.select(
            [sys.stdin],
            [],
            [],
            min(5, max(0, (expires - datetime.now(timezone.utc)).total_seconds())),
        )
        if ready:
            if (job_directory / "releasing").exists():
                raise InterruptedError("The host is releasing this maintenance attempt.")
            line = sys.stdin.readline()
            if not line:
                raise EOFError("Maintenance controller input closed; roll back.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("A maintenance command must be an object.")
            if (job_directory / "releasing").exists():
                raise InterruptedError("The host is releasing this maintenance attempt.")
            return value
    raise TimeoutError("Maintenance deadline reached; roll back and release.")


SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
IFF_UP = 1
IFF_LOOPBACK = 8


def require_isolated_network() -> None:
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if status.get("NoNewPrivs", "").strip() != "1" or any(
        int(status[key].strip(), 16) for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise RuntimeError("The rehearsal requires no capabilities and no privilege gains.")
    loopback = set()
    active_loopback = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for _index, name in socket.if_nameindex():
            request = struct.pack("256s", name.encode())
            flags = struct.unpack_from(
                "H", fcntl.ioctl(control.fileno(), SIOCGIFFLAGS, request), 16
            )[0]
            local = bool(flags & IFF_LOOPBACK)
            if local:
                loopback.add(name)
                active_loopback = active_loopback or bool(flags & IFF_UP)
            elif flags & IFF_UP:
                raise RuntimeError("A non-loopback network interface is active.")
            try:
                address = IPv4Address(fcntl.ioctl(control.fileno(), SIOCGIFADDR, request)[20:24])
            except OSError as error:
                if error.errno != errno.EADDRNOTAVAIL:
                    raise
            else:
                if not address.is_unspecified and (not local or not address.is_loopback):
                    raise RuntimeError("A non-loopback IPv4 address is configured.")
    if not active_loopback:
        raise RuntimeError("The rehearsal loopback interface is unavailable.")
    for path, width, position in (
        ("/proc/self/net/route", 11, 0),
        ("/proc/self/net/ipv6_route", 10, -1),
    ):
        rows = Path(path).read_text().splitlines()
        if position == 0:
            if not rows or rows[0].split()[0] != "Iface":
                raise RuntimeError("IPv4 route metadata is unavailable.")
            rows = rows[1:]
        for line in rows:
            fields = line.split()
            if len(fields) != width or fields[position] not in loopback:
                raise RuntimeError("A non-loopback route or unknown route metadata is present.")
    for line in Path("/proc/self/net/if_inet6").read_text().splitlines():
        fields = line.split()
        if (
            len(fields) != 6
            or fields[-1] not in loopback
            or not IPv6Address(int(fields[0], 16)).is_loopback
        ):
            raise RuntimeError(
                "A non-loopback IPv6 address or unknown address metadata is present."
            )


def require_isolated_runtime(connection: Connection) -> None:
    from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS

    if connection.engine.url.host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("The rehearsal database must use isolated loopback.")
    if any(os.environ.get(key, "").strip() for key in PLATFORM_CREDENTIAL_KEYS):
        raise RuntimeError("The rehearsal must have no production keyring or provider credentials.")
    if sys.platform != "linux":
        raise RuntimeError("The rehearsal requires a qualified Linux network namespace.")
    require_isolated_network()


def rehearsal_receipt(
    connection: Connection,
    *,
    held: dict[str, Any],
    backup_sha256: str,
    expires: datetime,
) -> dict[str, Any]:
    from control_plane.storage.schema_migration import (
        migrate_schema_on_connection,
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
        if set(restored) - set(after) != set(retirement.RETIRED_TABLES):
            raise RuntimeError("The isolated rehearsal removed an unexpected table set.")
        require_retired_functions_absent(connection)
        verify_postgres_schema_invariants(connection)
    verify_committed_database(
        connection, {"system_identifier": identifier, "before": restored, "after": after}
    )
    verify_application_reads(connection)
    return {
        **{key: held[key] for key in ("hold_id", "snapshot", "transaction")},
        "system_identifier": identifier,
        "backup_sha256": backup_sha256,
        "restored": restored,
        "retained": retained_snapshot(after),
        "revision": revision,
        "action": "apply",
    }


def verify_application_reads(connection: Connection, *, service_health: bool = False) -> None:
    from control_plane.storage.factory import build_privileged_operation_worker_store

    options = " ".join(connection.engine.url.normalized_query.get("options", ()))
    url = connection.engine.url.update_query_dict(
        {"options": options + " -c default_transaction_read_only=on"}
    ).render_as_string(hide_password=False)
    store = build_privileged_operation_worker_store(database_url=url)
    try:
        for engine in (store._engine, store._preview_lock_engine):
            if engine is None:
                raise RecoveryRequired("The runtime verification engine is unavailable.")
            with engine.connect() as reader:
                if reader.scalar(text("SHOW transaction_read_only")) != "on":
                    raise RecoveryRequired("The actual runtime probe is not enforced read-only.")
        store.verify_schema()
        try:
            expired = store.expire_ordinary_agent_deliveries(limit=100)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "25006":
                raise RecoveryRequired(
                    "The existing expiry callback would change credentials."
                ) from error
            raise
        if expired:
            raise RecoveryRequired(
                "The existing expiry callback did not pass without credential changes."
            )
        store.list_ordinary_agent_delivery_activation_records()
        store.list_ordinary_agent_delivery_activation_event_records()
        store.list_merge_train_controller_state_records()
        if service_health:
            from control_plane.database_maintenance_health import verify_service_health

            try:
                verify_service_health(store)
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "25006":
                    raise RecoveryRequired(
                        "The actual service startup would change stored records."
                    ) from error
                raise
    finally:
        store.close()


def cancel_waiting_admissions(connection: Connection, addresses: list[str]) -> None:
    connection.execute(text("SELECT pg_stat_clear_snapshot()"))
    connection.execute(
        text(
            "SELECT pg_cancel_backend(a.pid) FROM pg_stat_activity a "
            "WHERE a.datname=current_database() AND a.usename=current_user "
            "AND a.backend_type='client backend' AND a.backend_xid IS NULL "
            "AND host(a.client_addr)=ANY(:addresses) "
            "AND a.wait_event_type='Lock' AND a.wait_event='advisory' "
            "AND pg_backend_pid()=ANY(pg_blocking_pids(a.pid))"
        ),
        {"addresses": addresses},
    )
    connection.commit()


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
    with deployment.fence(arguments.expires_at), maintenance_admission(connection):
        if (arguments.job_directory / "releasing").exists():
            raise RuntimeError("The host already released this maintenance attempt.")
        require_no_active_executors(connection)
        connection.commit()
        state = json.loads((arguments.job_directory / "state.json").read_text())
        if state["hold_id"] != arguments.hold_id or not state.get("original_addresses"):
            raise RuntimeError("The original writer address custody is missing.")
        emit(
            "deployment_held",
            target_id=deployment.target_id,
            pid=identity[0],
            backend_start=identity[1].isoformat(),
        )
        command = await_command(
            expires,
            lambda: cancel_waiting_admissions(connection, state["original_addresses"]),
            arguments.job_directory,
        )
        if command.get("action") != "stopped" or (arguments.job_directory / "releasing").exists():
            raise RuntimeError("The host did not confirm its complete physical writer hold.")
        deployment.observe(deadline=arguments.expires_at)
        require_no_active_executors(connection)
        connection.commit()
        with locked_database(
            connection,
            expires_at=expires,
        ):
            before = database_snapshot(connection)
            revision = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
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

            dump_client: dict[str, Any] = {"name": dump_application_name(arguments.hold_id)}

            def observe() -> None:
                require_no_other_clients(connection, dump_client=dump_client)

            receipt = await_command(expires, observe, arguments.job_directory)
            if receipt.get("action") == "apply":
                deployment.observe(deadline=arguments.expires_at)
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
                proof = {
                    "hold_id": arguments.hold_id,
                    "system_identifier": identifier,
                    "before": before,
                    "after": database_snapshot(connection),
                }
                with locked_state(arguments.job_directory / "state.json", active=True):
                    deployment.hold_recovery(arguments.expires_at, arguments.hold_id)
                    save(arguments.job_directory / "commit-pending.json", proof)
                emit("verified_before_commit", revision=revision)
            elif receipt.get("action") != "release":
                raise ValueError("Only an exact rehearsal or release command is accepted.")
    connection.close()
    emit("database_released", revision=revision)


def release_deployment(arguments: argparse.Namespace, url: str) -> None:
    if not arguments.target_id:
        raise ValueError("unfence requires the recorded own-service target identity.")
    deployment = DeploymentMaintenance(
        target_id=arguments.target_id,
        database_url=url,
        image=os.environ.get("DOCKER_IMAGE_REFERENCE", ""),
        root=Path("/app"),
        job_directory=arguments.job_directory,
        hold_id=arguments.hold_id,
    )
    from control_plane.deployment_maintenance import MAINTENANCE_UNTIL_ENV
    from control_plane.dokploy.api import parse_dokploy_env_text

    marker = parse_dokploy_env_text(str(deployment.read_target().get("env") or "")).get(
        MAINTENANCE_UNTIL_ENV
    )
    if (arguments.job_directory / "commit-pending.json").exists() and (
        not (arguments.job_directory / "database-safe.json").exists()
        or not (arguments.job_directory / "resumed").exists()
    ):
        raise RuntimeError(
            "Committed-state recovery must be verified before removing the deployment hold."
        )
    if marker is not None:
        if marker not in {arguments.expires_at, recovery_marker(arguments.hold_id)}:
            raise RecoveryRequired("The recorded deployment hold no longer owns this marker.")
    deployment.marker(marker or arguments.expires_at, remove=True)
    emit("deployment_released")


def interrupted(_signum: int, _frame: object) -> None:
    raise InterruptedError("Maintenance interrupted; release its bounded hold.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launchplane's bounded full writer-hold database keeper."
    )
    parser.add_argument(
        "mode", choices=("plan", "unfence", "hold", "rehearse", "verify", "revision", "health")
    )
    parser.add_argument("--before-file", type=Path)
    parser.add_argument("--backup-sha256", default="")
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--hold-id", required=True)
    parser.add_argument("--target-id", default="")
    parser.add_argument("--job-directory", type=Path, default=Path("/maintenance"))
    arguments = parser.parse_args()
    expires = datetime.fromisoformat(arguments.expires_at)
    if expires.tzinfo is None or (
        arguments.mode not in {"unfence", "verify", "revision"}
        and not 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 3600
    ):
        parser.error("The deadline must be within one hour and include a timezone.")
    url = os.environ["LAUNCHPLANE_DATABASE_URL"]
    if arguments.mode in {"verify", "health"}:
        from sqlalchemy.engine import make_url

        require_isolated_network()
        original_url = make_url(url)
        options = " ".join(original_url.normalized_query.get("options", ()))
        url = (
            original_url.difference_update_query(["host", "hostaddr", "service"])
            .set(host=None, port=None)
            .update_query_dict(
                {"host": "/pgsocket", "options": options + " -c default_transaction_read_only=on"}
            )
            .render_as_string(hide_password=False)
        )
    if arguments.mode == "unfence":
        try:
            release_deployment(arguments, url)
        except RecoveryRequired:
            emit(DEPLOYMENT_RECOVERY_REQUIRED_EVENT, hold_id=arguments.hold_id)
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
            if arguments.mode == "health":
                verify_application_reads(connection, service_health=True)
                emit(
                    "health_qualified",
                    hold_id=arguments.hold_id,
                    system_identifier=database_system_identifier(connection),
                    health_passed=True,
                )
                return 0
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
            if arguments.mode == "verify":
                proof = json.loads((arguments.job_directory / "commit-pending.json").read_text())
                if proof["hold_id"] != arguments.hold_id:
                    raise RuntimeError("The recovery proof belongs to another maintenance attempt.")
                try:
                    revision = verify_committed_database(connection, proof)
                    verify_application_reads(connection, service_health=True)
                except RecoveryRequired as error:
                    emit(error.event, hold_id=arguments.hold_id)
                    return 0
                from control_plane.storage.migrations.versions import (
                    e7c1d5e9a3b6_drop_retired_delivery_tables as retirement,
                )

                outcome = (
                    "before"
                    if set(retirement.RETIRED_TABLES)
                    <= set(inspect(connection).get_table_names(schema="public"))
                    else "after"
                )
                emit(
                    "database_verified",
                    revision=revision,
                    hold_id=arguments.hold_id,
                    outcome=outcome,
                    health_passed=True,
                )
                return 0
            if arguments.mode == "revision":
                emit(
                    "revision",
                    revision=str(
                        connection.scalar(text("SELECT version_num FROM alembic_version"))
                    ),
                )
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
                job_directory=arguments.job_directory,
                hold_id=arguments.hold_id,
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
