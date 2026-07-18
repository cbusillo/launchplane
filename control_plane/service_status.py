from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import cast

import click

from control_plane.workflows.launchplane_self_deploy import LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY
from control_plane.storage.schema_invariants import RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS
from control_plane.storage.schema_migration import SCHEMA_MIGRATION_TARGET_REVISION
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerStore,
    build_odoo_stable_operation_worker_status,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    VeriReelProdBackupGateOperationWorkerStore,
    build_verireel_prod_backup_gate_operation_worker_status,
)


def launchplane_policy_sha256_from_env() -> str:
    policy_toml = os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip()
    if policy_toml:
        return hashlib.sha256(policy_toml.encode("utf-8")).hexdigest()

    policy_b64 = os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip()
    if policy_b64:
        try:
            policy_bytes = base64.b64decode(policy_b64, validate=True)
        except Exception:
            return ""
        return hashlib.sha256(policy_bytes).hexdigest()

    policy_file = os.environ.get("LAUNCHPLANE_POLICY_FILE", "").strip()
    if not policy_file:
        return ""
    try:
        return hashlib.sha256(Path(policy_file).read_bytes()).hexdigest()
    except OSError:
        return ""


def launchplane_runtime_payload(
    *,
    storage_backend: str,
    database_schema_revision: str,
    authz_policy_schema_version: int,
    authz_policy_sha256_value: str,
    authz_policy_source: str,
) -> dict[str, object]:
    return {
        "authz_policy_sha256": authz_policy_sha256_value,
        "authz_policy_schema_version": authz_policy_schema_version,
        "authz_policy_source": authz_policy_source,
        "bootstrap_authz_policy_sha256": launchplane_policy_sha256_from_env(),
        "docker_image_reference": os.environ.get(LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "").strip(),
        "compatible_database_schema_revisions": RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS,
        "database_schema_revision": database_schema_revision,
        "schema_migration_target_revision": SCHEMA_MIGRATION_TARGET_REVISION,
        "service_audience": os.environ.get("LAUNCHPLANE_SERVICE_AUDIENCE", "").strip(),
        "storage_backend": storage_backend,
    }


def require_odoo_stable_operation_worker_store(
    record_store: object,
) -> OdooStableOperationWorkerStore:
    required_methods = (
        "list_odoo_stable_bootstrap_operation_records",
        "claim_next_odoo_stable_bootstrap_operation_record",
        "heartbeat_odoo_stable_bootstrap_operation_record",
        "complete_odoo_stable_bootstrap_operation_record",
        "recover_expired_odoo_stable_bootstrap_operation_records",
        "list_odoo_stable_target_replacement_operation_records",
        "claim_next_odoo_stable_target_replacement_operation_record",
        "heartbeat_odoo_stable_target_replacement_operation_record",
        "complete_odoo_stable_target_replacement_operation_record",
        "recover_expired_odoo_stable_target_replacement_operation_records",
    )
    if all(callable(getattr(record_store, method_name, None)) for method_name in required_methods):
        return cast(OdooStableOperationWorkerStore, record_store)
    raise click.ClickException(
        "Odoo stable operation worker status requires Launchplane operation-record storage."
    )


def odoo_stable_operation_worker_status_payload(
    *,
    record_store: object,
    recent_terminal_limit: int,
) -> dict[str, object]:
    worker_status = build_odoo_stable_operation_worker_status(
        record_store=require_odoo_stable_operation_worker_store(record_store),
        recent_terminal_limit=recent_terminal_limit,
    )
    return asdict(worker_status)


def require_verireel_prod_backup_gate_operation_worker_store(
    record_store: object,
) -> VeriReelProdBackupGateOperationWorkerStore:
    required_methods = (
        "list_verireel_prod_backup_gate_operation_records",
        "claim_next_verireel_prod_backup_gate_operation_record",
        "heartbeat_verireel_prod_backup_gate_operation_record",
        "mark_verireel_prod_backup_gate_operation_phase",
        "complete_verireel_prod_backup_gate_operation_record",
        "complete_verireel_prod_backup_gate_operation_with_backup_gate_record",
        "recover_expired_verireel_prod_backup_gate_operation_records",
        "write_backup_gate_record",
    )
    if all(callable(getattr(record_store, method_name, None)) for method_name in required_methods):
        return cast(VeriReelProdBackupGateOperationWorkerStore, record_store)
    raise click.ClickException(
        "VeriReel prod backup gate worker status requires Launchplane operation-record storage."
    )


def verireel_prod_backup_gate_operation_worker_status_payload(
    *,
    record_store: object,
    recent_terminal_limit: int,
) -> dict[str, object]:
    worker_status = build_verireel_prod_backup_gate_operation_worker_status(
        record_store=require_verireel_prod_backup_gate_operation_worker_store(record_store),
        recent_terminal_limit=recent_terminal_limit,
    )
    return asdict(worker_status)


def query_int_value(
    raw_value: str,
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if not raw_value.strip():
        value = default
    else:
        value = int(raw_value.strip())
    if value is None:
        return None
    if minimum is not None and value < minimum:
        raise ValueError(f"Query parameter {key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Query parameter {key} must be at most {maximum}")
    return value
