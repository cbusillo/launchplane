import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import signal
import socket
from threading import Event
from typing import cast
import uuid

import click

from control_plane.agent_operator_contract import write_agent_operator_contract
from control_plane.cli_shared import DATABASE_URL_ENV_KEYS as _DATABASE_URL_ENV_KEYS
from control_plane.outbox_worker import (
    DEFAULT_OUTBOX_WORKER_ERROR_BACKOFF_SECONDS,
    DEFAULT_OUTBOX_WORKER_LEASE_SECONDS,
    DEFAULT_OUTBOX_WORKER_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_OUTBOX_WORKER_POLL_SECONDS,
    OutboxWorkerStore,
    run_outbox_worker_loop,
    run_outbox_worker_once,
)
from control_plane.openapi_export import write_canonical_openapi
from control_plane.privileged_operation_worker import (
    PrivilegedOperationExecutionStore,
    execute_approved_privileged_operations_once,
)
from control_plane.contracts.driver_descriptor import DriverContextView
from control_plane.drivers.registry import build_driver_context_view
from control_plane.service import serve_launchplane_service
from control_plane.storage.factory import build_privileged_operation_worker_store
from control_plane.workflows.odoo_stable_operation_worker import (
    DEFAULT_ODOO_STABLE_WORKER_ERROR_BACKOFF_SECONDS,
    DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS,
    DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS,
    DEFAULT_ODOO_STABLE_WORKER_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    DEFAULT_ODOO_STABLE_WORKER_POLL_SECONDS,
    OdooStableOperationWorkerStore,
    build_odoo_stable_operation_worker_status,
    reconcile_stale_odoo_stable_operation_records,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_ERROR_BACKOFF_SECONDS,
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS,
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS,
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_VERIREEL_BACKUP_GATE_WORKER_POLL_SECONDS,
    VeriReelProdBackupGateOperationWorkerStore,
    build_verireel_prod_backup_gate_operation_worker_status,
    reconcile_stale_verireel_prod_backup_gate_operation_records,
    run_verireel_prod_backup_gate_operation_worker_loop,
    run_verireel_prod_backup_gate_operation_worker_once,
)

from control_plane.workflows.public_ingress_monitor import public_ingress_notification_drivers
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source

_PRIVILEGED_OPERATION_WORKER_SCHEMA_PROBE_EVIDENCE = (
    b"launchplane-privileged-operation-worker-schema-probe-completed-v1\n"
)


def _consume_privileged_operation_worker_schema_probe_evidence(file_descriptor: int) -> None:
    try:
        with os.fdopen(file_descriptor, "rb", closefd=True) as evidence_file:
            evidence = evidence_file.read(
                len(_PRIVILEGED_OPERATION_WORKER_SCHEMA_PROBE_EVIDENCE) + 1
            )
    except OSError as error:
        raise click.ClickException(
            "Privileged-operation worker startup probe evidence is unavailable."
        ) from error
    if evidence != _PRIVILEGED_OPERATION_WORKER_SCHEMA_PROBE_EVIDENCE:
        raise click.ClickException("Privileged-operation worker startup probe evidence is invalid.")


_SERVICE_TARGET_TYPE_ENV_KEYS = ("LAUNCHPLANE_DOKPLOY_TARGET_TYPE",)
_SERVICE_TARGET_ID_ENV_KEYS = ("LAUNCHPLANE_DOKPLOY_TARGET_ID",)


@dataclass(frozen=True)
class ServiceCliCallbacks:
    store_factory: Callable[..., object]
    control_plane_root: Callable[[], Path]
    build_bootstrap_policy_payload: Callable[..., dict[str, object]]
    sync_launchplane_bootstrap_policy: Callable[..., dict[str, object]]
    inspect_launchplane_service_dokploy_target: Callable[
        ..., tuple[dokploy_api.JsonObject, dict[str, object]]
    ]
    launchplane_service_target_preflight_error_message: Callable[..., str]
    inspect_local_launchplane_config_boundary: Callable[..., dict[str, object]]
    build_config_authority_audit: Callable[..., dict[str, object]]
    evaluate_config_authority_gate: Callable[..., dict[str, object]]
    render_config_authority_markdown: Callable[..., str]


_callbacks: ServiceCliCallbacks | None = None


def register_service_commands(main: click.Group, *, callbacks: ServiceCliCallbacks) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(service)


def _service_callbacks() -> ServiceCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane service CLI callbacks are not configured.")
    return _callbacks


def _store(*, state_dir: Path, database_url: str | None = None) -> object:
    return _service_callbacks().store_factory(state_dir=state_dir, database_url=database_url)


def _control_plane_root() -> Path:
    return _service_callbacks().control_plane_root()


def _first_driver_payload(
    view_payload: DriverContextView, *, driver_id: str
) -> dict[str, object] | None:
    for driver_view in view_payload.drivers:
        if getattr(driver_view, "driver_id", "") == driver_id:
            return dict(driver_view.model_dump(mode="json"))
    return None


def _provenance_payload(raw_value: object) -> dict[str, object] | None:
    if not isinstance(raw_value, dict):
        return None
    provenance = raw_value.get("provenance")
    return provenance if isinstance(provenance, dict) else None


def _freshness_surface(
    *, name: str, driver_id: str, raw_value: object, unsupported: bool = False
) -> dict[str, object]:
    if unsupported:
        return {
            "name": name,
            "driver_id": driver_id,
            "freshness_status": "unsupported",
            "source_kind": "unsupported",
            "source_record_id": "",
            "recorded_at": "",
            "stale_after": "",
            "has_provenance": True,
        }
    provenance = _provenance_payload(raw_value)
    if provenance is None:
        return {
            "name": name,
            "driver_id": driver_id,
            "freshness_status": "missing",
            "source_kind": "record",
            "source_record_id": "",
            "recorded_at": "",
            "stale_after": "",
            "has_provenance": False,
        }
    freshness_status = str(provenance.get("freshness_status") or "missing")
    source_kind = str(provenance.get("source_kind") or "record")
    source_record_id = str(provenance.get("source_record_id") or "")
    has_provenance = freshness_status != "missing" and (
        source_kind != "record" or bool(source_record_id)
    )
    return {
        "name": name,
        "driver_id": driver_id,
        "freshness_status": freshness_status,
        "source_kind": source_kind,
        "source_record_id": source_record_id,
        "recorded_at": str(provenance.get("recorded_at") or ""),
        "stale_after": str(provenance.get("stale_after") or ""),
        "has_provenance": has_provenance,
    }


def _build_data_freshness_report(
    *, record_store: object, context_name: str, preview_context_name: str
) -> dict[str, object]:
    surfaces: list[dict[str, object]] = []
    for instance_name in ("prod", "testing"):
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context_name,
            instance_name=instance_name,
        )
        driver = _first_driver_payload(view, driver_id="verireel")
        lane_summary = driver.get("lane_summary") if isinstance(driver, dict) else None
        surfaces.append(
            _freshness_surface(
                name=f"{context_name}/{instance_name}/lane",
                driver_id="verireel",
                raw_value=lane_summary,
            )
        )

    preview_view = build_driver_context_view(
        record_store=record_store,
        context_name=preview_context_name,
    )
    preview_driver = _first_driver_payload(preview_view, driver_id="verireel")
    inventory_provenance = (
        preview_driver.get("preview_inventory_provenance")
        if isinstance(preview_driver, dict)
        else None
    )
    preview_summaries = (
        preview_driver.get("preview_summaries") if isinstance(preview_driver, dict) else None
    )
    if isinstance(preview_summaries, list) and preview_summaries:
        for summary in preview_summaries:
            preview = summary.get("preview") if isinstance(summary, dict) else None
            preview_id = "unknown-preview"
            if isinstance(preview, dict):
                preview_id = str(preview.get("preview_id") or preview_id)
            surfaces.append(
                _freshness_surface(
                    name=f"{preview_context_name}/{preview_id}",
                    driver_id="verireel",
                    raw_value=summary,
                )
            )
    else:
        surfaces.append(
            _freshness_surface(
                name=f"{preview_context_name}/preview-inventory",
                driver_id="verireel",
                raw_value={"provenance": inventory_provenance}
                if isinstance(inventory_provenance, dict)
                else None,
            )
        )

    missing_provenance = [surface for surface in surfaces if not surface["has_provenance"]]
    return {
        "status": "ok" if not missing_provenance else "rejected",
        "context": context_name,
        "preview_context": preview_context_name,
        "surface_count": len(surfaces),
        "missing_provenance_count": len(missing_provenance),
        "surfaces": surfaces,
    }


@click.group()
def service() -> None:
    """Launchplane service commands."""


@service.command("serve")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--policy-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8080, show_default=True)
@click.option(
    "--audience",
    envvar="LAUNCHPLANE_SERVICE_AUDIENCE",
    help="Expected GitHub OIDC audience for Launchplane service tokens.",
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default=None,
    help="Postgres connection string for Launchplane shared-service core records.",
)
def service_serve(
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
    database_url: str | None,
) -> None:
    if database_url is None or not database_url.strip():
        raise click.ClickException(
            "Launchplane service refuses startup without --database-url or "
            "LAUNCHPLANE_DATABASE_URL. Filesystem state is local-only."
        )
    if audience is None or not audience.strip():
        raise click.ClickException(
            "Launchplane service refuses startup without --audience or "
            "LAUNCHPLANE_SERVICE_AUDIENCE. The service audience is explicit "
            "operator/process wiring."
        )
    serve_launchplane_service(
        state_dir=state_dir,
        policy_file=policy_file,
        host=host,
        port=port,
        audience=audience,
        database_url=database_url,
    )


@service.group("outbox-workers")
def service_outbox_workers() -> None:
    """Run Launchplane-owned transactional outbox delivery workers."""


@service_outbox_workers.command("run-once")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by outbox delivery workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_OUTBOX_WORKER_LEASE_SECONDS,
    show_default=True,
)
def service_outbox_workers_run_once(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Outbox workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    record_store = _store(state_dir=state_dir, database_url=database_url)
    result = run_outbox_worker_once(
        record_store=cast(OutboxWorkerStore, record_store),
        control_plane_root=control_plane_root or _control_plane_root(),
        lease_owner=generated_lease_owner,
        lease_seconds=lease_seconds,
        notification_drivers=public_ingress_notification_drivers(record_store=record_store),
    )
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service_outbox_workers.command("run")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by outbox delivery workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_OUTBOX_WORKER_LEASE_SECONDS,
    show_default=True,
)
@click.option(
    "--poll-seconds",
    type=int,
    default=DEFAULT_OUTBOX_WORKER_POLL_SECONDS,
    show_default=True,
)
@click.option(
    "--error-backoff-seconds",
    type=int,
    default=DEFAULT_OUTBOX_WORKER_ERROR_BACKOFF_SECONDS,
    show_default=True,
)
@click.option(
    "--max-consecutive-errors",
    type=int,
    default=DEFAULT_OUTBOX_WORKER_MAX_CONSECUTIVE_ERRORS,
    show_default=True,
)
def service_outbox_workers_run(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
    poll_seconds: int,
    error_backoff_seconds: int,
    max_consecutive_errors: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Outbox workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    record_store = _store(state_dir=state_dir, database_url=database_url)
    stop_event = Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    try:
        run_outbox_worker_loop(
            record_store=cast(OutboxWorkerStore, record_store),
            control_plane_root=control_plane_root or _control_plane_root(),
            lease_owner=generated_lease_owner,
            should_stop=stop_event.is_set,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            error_backoff_seconds=error_backoff_seconds,
            max_consecutive_errors=max_consecutive_errors,
            notification_drivers=public_ingress_notification_drivers(record_store=record_store),
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    click.echo(json.dumps({"status": "stopped"}, indent=2, sort_keys=True))


@service.group("odoo-workers")
def service_odoo_workers() -> None:
    """Run Launchplane-owned Odoo operation workers."""


@service.group("privileged-operation-workers")
def service_privileged_operation_workers() -> None:
    """Run Launchplane-owned privileged-operation workers."""


@service_privileged_operation_workers.command("run-once")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20, show_default=True)
def service_privileged_operation_workers_run_once(
    state_dir: Path,
    database_url: str,
    lease_owner: str,
    limit: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Privileged-operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    records = execute_approved_privileged_operations_once(
        record_store=cast(
            PrivilegedOperationExecutionStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        lease_owner=generated_lease_owner,
        limit=limit,
    )
    click.echo(
        json.dumps(
            {
                "processed": len(records),
                "statuses": [record.status for record in records],
            },
            indent=2,
            sort_keys=True,
        )
    )


@service_privileged_operation_workers.command("run")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option("--poll-seconds", type=click.IntRange(min=1, max=300), default=15, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20, show_default=True)
@click.option(
    "--error-backoff-seconds", type=click.IntRange(min=1, max=300), default=15, show_default=True
)
@click.option(
    "--max-consecutive-errors", type=click.IntRange(min=1, max=100), default=3, show_default=True
)
@click.option(
    "--schema-probe-fd",
    type=click.IntRange(min=3, max=255),
    required=True,
    hidden=True,
)
def service_privileged_operation_workers_run(
    state_dir: Path,
    database_url: str,
    lease_owner: str,
    poll_seconds: int,
    limit: int,
    error_backoff_seconds: int,
    max_consecutive_errors: int,
    schema_probe_fd: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Privileged-operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    _consume_privileged_operation_worker_schema_probe_evidence(schema_probe_fd)
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    stop_event = Event()

    def request_stop(_signal_number: int, _frame: object) -> None:
        stop_event.set()

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    stopped_cleanly = False
    try:
        click.echo(
            json.dumps(
                {
                    "event": "privileged_operation_worker_started",
                    "limit": limit,
                    "poll_seconds": poll_seconds,
                },
                sort_keys=True,
            )
        )
        store: PrivilegedOperationExecutionStore | None = None
        schema_probe_succeeded = False
        store_initialized = False
        first_poll_attempted = False
        consecutive_errors = 0

        def report_schema_probe_succeeded() -> None:
            nonlocal schema_probe_succeeded
            if schema_probe_succeeded:
                return
            click.echo(
                json.dumps(
                    {"event": "privileged_operation_worker_schema_probe_succeeded"},
                    sort_keys=True,
                )
            )
            schema_probe_succeeded = True

        while not stop_event.is_set():
            try:
                if store is None:
                    store = cast(
                        PrivilegedOperationExecutionStore,
                        build_privileged_operation_worker_store(
                            database_url=database_url,
                            schema_probe_completed=True,
                            on_schema_probe_succeeded=report_schema_probe_succeeded,
                        ),
                    )
                if not store_initialized:
                    click.echo(
                        json.dumps(
                            {"event": "privileged_operation_worker_store_initialized"},
                            sort_keys=True,
                        )
                    )
                    store_initialized = True
                if not first_poll_attempted:
                    click.echo(
                        json.dumps(
                            {"event": "privileged_operation_worker_first_poll_attempted"},
                            sort_keys=True,
                        )
                    )
                    first_poll_attempted = True
                records = execute_approved_privileged_operations_once(
                    record_store=store,
                    lease_owner=generated_lease_owner,
                    limit=limit,
                )
            except Exception as error:  # noqa: BLE001 - bounded worker retry loop.
                consecutive_errors += 1
                telemetry = {
                    "consecutive_errors": consecutive_errors,
                    "error_type": type(error).__name__,
                }
                if consecutive_errors >= max_consecutive_errors:
                    click.echo(
                        json.dumps(
                            {
                                "event": "privileged_operation_worker_threshold_exit",
                                **telemetry,
                            },
                            sort_keys=True,
                        )
                    )
                    raise click.ClickException(
                        "Privileged-operation workers exited after "
                        f"{consecutive_errors} consecutive polling errors."
                    ) from error
                click.echo(
                    json.dumps(
                        {"event": "privileged_operation_worker_retry", **telemetry},
                        sort_keys=True,
                    )
                )
                stop_event.wait(error_backoff_seconds)
                continue
            consecutive_errors = 0
            click.echo(
                json.dumps(
                    {
                        "event": "privileged_operation_worker_poll_succeeded",
                        "processed": len(records),
                        "statuses": [record.status for record in records],
                    },
                    sort_keys=True,
                )
            )
            stop_event.wait(poll_seconds)
        stopped_cleanly = True
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    if stopped_cleanly:
        click.echo(json.dumps({"event": "privileged_operation_worker_stopped"}, sort_keys=True))


@service_odoo_workers.command("run-once")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by Odoo operation workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS,
    show_default=True,
)
@click.option(
    "--heartbeat-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS,
    show_default=True,
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
def service_odoo_workers_run_once(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Odoo operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    result = run_odoo_stable_operation_worker_once(
        record_store=cast(
            OdooStableOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        control_plane_root_path=control_plane_root or _control_plane_root(),
        lease_owner=generated_lease_owner,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        max_attempts=max_attempts,
    )
    click.echo(json.dumps(result.__dict__, indent=2, sort_keys=True))


@service_odoo_workers.command("reconcile")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
def service_odoo_workers_reconcile(
    state_dir: Path,
    database_url: str,
    max_attempts: int,
) -> None:
    """Recover expired Odoo worker leases without claiming new work."""
    if not database_url.strip():
        raise click.ClickException(
            "Odoo operation worker reconciliation requires --database-url or "
            "LAUNCHPLANE_DATABASE_URL."
        )
    result = reconcile_stale_odoo_stable_operation_records(
        record_store=cast(
            OdooStableOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        max_attempts=max_attempts,
    )
    payload = {
        "status": "ok",
        "reconciled_bootstrap_ids": list(result.reconciled_bootstrap_ids),
        "reconciled_restore_ids": list(result.reconciled_restore_ids),
        "reconciled_retained_import_ids": list(result.reconciled_retained_import_ids),
        "reconciled_replacement_ids": list(result.reconciled_replacement_ids),
        "reconciled_count": len(result.reconciled_bootstrap_ids)
        + len(result.reconciled_restore_ids)
        + len(result.reconciled_retained_import_ids)
        + len(result.reconciled_replacement_ids),
    }
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@service_odoo_workers.command("run")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by Odoo operation workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS,
    show_default=True,
)
@click.option(
    "--heartbeat-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS,
    show_default=True,
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
@click.option(
    "--poll-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_POLL_SECONDS,
    show_default=True,
)
@click.option(
    "--error-backoff-seconds",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_ERROR_BACKOFF_SECONDS,
    show_default=True,
)
@click.option(
    "--max-consecutive-errors",
    type=int,
    default=DEFAULT_ODOO_STABLE_WORKER_MAX_CONSECUTIVE_ERRORS,
    show_default=True,
)
def service_odoo_workers_run(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    poll_seconds: int,
    error_backoff_seconds: int,
    max_consecutive_errors: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Odoo operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    stop_event = Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    try:
        result = run_odoo_stable_operation_worker_loop(
            record_store=cast(
                OdooStableOperationWorkerStore,
                _store(state_dir=state_dir, database_url=database_url),
            ),
            control_plane_root_path=control_plane_root or _control_plane_root(),
            lease_owner=generated_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_attempts=max_attempts,
            poll_seconds=poll_seconds,
            error_backoff_seconds=error_backoff_seconds,
            max_consecutive_errors=max_consecutive_errors,
            stop_event=stop_event,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service_odoo_workers.command("status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--recent-terminal-limit",
    type=int,
    default=10,
    show_default=True,
)
def service_odoo_workers_status(
    state_dir: Path,
    database_url: str,
    recent_terminal_limit: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "Odoo operation worker status requires --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    result = build_odoo_stable_operation_worker_status(
        record_store=cast(
            OdooStableOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        recent_terminal_limit=recent_terminal_limit,
    )
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service.group("verireel-workers")
def service_verireel_workers() -> None:
    """Run Launchplane-owned VeriReel operation workers."""


@service_verireel_workers.command("run-once")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by VeriReel operation workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS,
    show_default=True,
)
@click.option(
    "--heartbeat-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS,
    show_default=True,
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
def service_verireel_workers_run_once(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "VeriReel operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    result = run_verireel_prod_backup_gate_operation_worker_once(
        record_store=cast(
            VeriReelProdBackupGateOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        control_plane_root_path=control_plane_root or _control_plane_root(),
        lease_owner=generated_lease_owner,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        max_attempts=max_attempts,
    )
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service_verireel_workers.command("reconcile")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
def service_verireel_workers_reconcile(
    state_dir: Path,
    database_url: str,
    max_attempts: int,
) -> None:
    """Recover expired VeriReel worker leases without claiming new work."""
    if not database_url.strip():
        raise click.ClickException(
            "VeriReel operation worker reconciliation requires --database-url or "
            "LAUNCHPLANE_DATABASE_URL."
        )
    result = reconcile_stale_verireel_prod_backup_gate_operation_records(
        record_store=cast(
            VeriReelProdBackupGateOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        max_attempts=max_attempts,
    )
    payload = {
        "status": "ok",
        "reconciled_operation_ids": list(result.reconciled_operation_ids),
        "reconciled_count": len(result.reconciled_operation_ids),
    }
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@service_verireel_workers.command("run")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used by VeriReel operation workflows.",
)
@click.option(
    "--lease-owner",
    default="",
    help="Worker lease owner id. Defaults to a generated process-local id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS,
    show_default=True,
)
@click.option(
    "--heartbeat-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS,
    show_default=True,
)
@click.option(
    "--max-attempts",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    show_default=True,
)
@click.option(
    "--poll-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_POLL_SECONDS,
    show_default=True,
)
@click.option(
    "--error-backoff-seconds",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_ERROR_BACKOFF_SECONDS,
    show_default=True,
)
@click.option(
    "--max-consecutive-errors",
    type=int,
    default=DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_CONSECUTIVE_ERRORS,
    show_default=True,
)
def service_verireel_workers_run(
    state_dir: Path,
    database_url: str,
    control_plane_root: Path | None,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    poll_seconds: int,
    error_backoff_seconds: int,
    max_consecutive_errors: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "VeriReel operation workers require --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    generated_lease_owner = (
        f"{socket.gethostname()}:{uuid.uuid4()}" if not lease_owner.strip() else lease_owner
    )
    stop_event = Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    try:
        result = run_verireel_prod_backup_gate_operation_worker_loop(
            record_store=cast(
                VeriReelProdBackupGateOperationWorkerStore,
                _store(state_dir=state_dir, database_url=database_url),
            ),
            control_plane_root_path=control_plane_root or _control_plane_root(),
            lease_owner=generated_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_attempts=max_attempts,
            poll_seconds=poll_seconds,
            error_backoff_seconds=error_backoff_seconds,
            max_consecutive_errors=max_consecutive_errors,
            stop_event=stop_event,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service_verireel_workers.command("status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
@click.option(
    "--recent-terminal-limit",
    type=int,
    default=10,
    show_default=True,
)
def service_verireel_workers_status(
    state_dir: Path,
    database_url: str,
    recent_terminal_limit: int,
) -> None:
    if not database_url.strip():
        raise click.ClickException(
            "VeriReel operation worker status requires --database-url or LAUNCHPLANE_DATABASE_URL."
        )
    result = build_verireel_prod_backup_gate_operation_worker_status(
        record_store=cast(
            VeriReelProdBackupGateOperationWorkerStore,
            _store(state_dir=state_dir, database_url=database_url),
        ),
        recent_terminal_limit=recent_terminal_limit,
    )
    click.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@service.command("render-authz-policy")
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path),
    required=True,
    help="Explicit bootstrap authz policy source. Live policy should be DB-backed.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "b64"]),
    default="json",
    show_default=True,
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to resolve a relative policy file.",
)
def service_render_authz_policy(
    policy_file: Path | None,
    output_format: str,
    control_plane_root: Path | None,
) -> None:
    callbacks = _service_callbacks()
    payload = callbacks.build_bootstrap_policy_payload(
        control_plane_root=control_plane_root or _control_plane_root(),
        policy_file=policy_file,
    )
    if output_format == "b64":
        click.echo(str(payload["policy_b64"]))
        return
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@service.command("sync-bootstrap-policy")
@click.option(
    "--target-type",
    type=click.Choice(["compose", "application"]),
    envvar=_SERVICE_TARGET_TYPE_ENV_KEYS,
    required=True,
    help="Dokploy target type for the live Launchplane service.",
)
@click.option(
    "--target-id",
    envvar=_SERVICE_TARGET_ID_ENV_KEYS,
    required=True,
    help="Dokploy target id for the live Launchplane service.",
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path),
    required=True,
    help="Explicit bootstrap authz policy source. Live policy should be DB-backed.",
)
@click.option("--apply", "apply_changes", is_flag=True, default=False)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials and a relative policy file.",
)
def service_sync_bootstrap_policy(
    target_type: str,
    target_id: str,
    policy_file: Path | None,
    apply_changes: bool,
    control_plane_root: Path | None,
) -> None:
    callbacks = _service_callbacks()
    payload = callbacks.sync_launchplane_bootstrap_policy(
        target_type=target_type,
        target_id=target_id,
        control_plane_root=control_plane_root or _control_plane_root(),
        policy_file=policy_file,
        apply_changes=apply_changes,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@service.command("inspect-dokploy-target")
@click.option(
    "--target-type",
    type=click.Choice(["compose", "application"]),
    envvar=_SERVICE_TARGET_TYPE_ENV_KEYS,
    required=True,
    help="Dokploy target type for the live Launchplane service.",
)
@click.option(
    "--target-id",
    envvar=_SERVICE_TARGET_ID_ENV_KEYS,
    required=True,
    help="Dokploy target id for the live Launchplane service.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials.",
)
def service_inspect_dokploy_target(
    target_type: str,
    target_id: str,
    control_plane_root: Path | None,
) -> None:
    callbacks = _service_callbacks()
    launchplane_root = control_plane_root or _control_plane_root()
    host, token = dokploy_source.read_dokploy_config(control_plane_root=launchplane_root)
    _, preflight_payload = callbacks.inspect_launchplane_service_dokploy_target(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    click.echo(json.dumps(preflight_payload, indent=2, sort_keys=True))
    if preflight_payload["blockers"]:
        raise click.ClickException(
            callbacks.launchplane_service_target_preflight_error_message(
                preflight_payload=preflight_payload
            )
        )


@service.command("inspect-config-boundary")
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to inspect local config authority.",
)
def service_inspect_config_boundary(control_plane_root: Path | None) -> None:
    callbacks = _service_callbacks()
    launchplane_root = control_plane_root or _control_plane_root()
    payload = callbacks.inspect_local_launchplane_config_boundary(
        control_plane_root=launchplane_root
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@service.command("audit-config-authority")
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to scan checked-in config authority.",
)
@click.option(
    "--mode",
    type=click.Choice(["full-audit", "changed-files-gate"]),
    default="full-audit",
    show_default=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown"]),
    default="json",
    show_default=True,
)
@click.option(
    "--include-untracked",
    is_flag=True,
    default=False,
    help="Include untracked files during full-audit discovery.",
)
@click.option(
    "--include-ignored",
    is_flag=True,
    default=False,
    help="Include ignored files during full-audit discovery except heavyweight directories.",
)
@click.option(
    "--path",
    "scan_paths",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Limit the audit to one or more files. Relative paths resolve from the control-plane root.",
)
@click.option(
    "--fail-on-findings",
    is_flag=True,
    default=False,
    help="Exit non-zero when unallowed config-authority findings are present.",
)
@click.option(
    "--gate-profile",
    type=click.Choice(["default", "product-repo"]),
    default="default",
    show_default=True,
    help="Policy profile used when --fail-on-findings is enabled.",
)
def service_audit_config_authority(
    control_plane_root: Path | None,
    mode: str,
    output_format: str,
    include_untracked: bool,
    include_ignored: bool,
    scan_paths: tuple[Path, ...],
    fail_on_findings: bool,
    gate_profile: str,
) -> None:
    callbacks = _service_callbacks()
    launchplane_root = control_plane_root or _control_plane_root()
    payload = callbacks.build_config_authority_audit(
        control_plane_root=launchplane_root,
        mode=mode,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        paths=scan_paths,
    )
    gate = None
    if fail_on_findings:
        gate = callbacks.evaluate_config_authority_gate(payload, profile=gate_profile)
        payload = {**payload, "gate": gate}
    if output_format == "markdown":
        click.echo(callbacks.render_config_authority_markdown(payload))
    else:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    if gate is not None and gate.get("status") == "fail":
        raise click.ClickException(
            "Config authority audit found unallowed checked-in runtime authority."
        )


@service.command("inspect-data-freshness")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", envvar=_DATABASE_URL_ENV_KEYS, default="", show_default=False)
@click.option("--local-inspection", is_flag=True, default=False)
@click.option("--context", "context_name", required=True)
@click.option("--preview-context", "preview_context_name", required=True)
def service_inspect_data_freshness(
    state_dir: Path,
    database_url: str,
    local_inspection: bool,
    context_name: str,
    preview_context_name: str,
) -> None:
    resolved_database_url = database_url.strip()
    if not resolved_database_url and not local_inspection:
        raise click.ClickException(
            "service inspect-data-freshness requires --database-url or "
            "LAUNCHPLANE_DATABASE_URL. Use --local-inspection for explicit local "
            "filesystem inspection."
        )
    record_store = _store(
        state_dir=state_dir,
        database_url=resolved_database_url if resolved_database_url else None,
    )
    try:
        payload = _build_data_freshness_report(
            record_store=record_store,
            context_name=context_name,
            preview_context_name=preview_context_name,
        )
    finally:
        close = getattr(record_store, "close", None)
        if callable(close):
            close()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
    if payload["status"] != "ok":
        raise click.ClickException("Launchplane data freshness report is missing provenance.")


@service.command("export-openapi")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Write canonical Launchplane OpenAPI JSON to this path.",
)
def service_export_openapi(output: Path) -> None:
    written_path = write_canonical_openapi(output)
    click.echo(str(written_path))


@service.command("export-agent-contract")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Write the public-safe Launchplane agent/operator contract to this path.",
)
@click.option(
    "--source-sha",
    default=None,
    help="Optional source commit SHA recorded as non-gating provenance.",
)
def service_export_agent_contract(output: Path, source_sha: str | None) -> None:
    written_path = write_agent_operator_contract(output, source_commit_sha=source_sha)
    click.echo(str(written_path))
