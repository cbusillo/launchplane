from __future__ import annotations

import os
import subprocess
import sys

from control_plane.storage.postgres import PostgresRecordStore

DATABASE_URL_ENV_VARS = ("LAUNCHPLANE_DATABASE_URL",)
PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS = 10
PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS = 30_000
PRIVILEGED_OPERATION_WORKER_STARTUP_TIMEOUT_SECONDS = 90
PRIVILEGED_OPERATION_WORKER_PROBE_SUCCESS_EXIT_CODE = 0
PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE = 2
PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE = 3
PRIVILEGED_OPERATION_WORKER_PROBE_ENVIRONMENT_KEYS = frozenset(
    {
        "DYLD_LIBRARY_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LD_LIBRARY_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
    }
)
PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS = (
    "launchplane_privileged_operations",
    "launchplane_privileged_operations_status_idx",
    "launchplane_privileged_operations_descriptor_idx",
    "launchplane_privileged_operation_events",
    "launchplane_privileged_operation_events_operation_sequence_uidx",
    "launchplane_privileged_operation_events_occurred_idx",
    "launchplane_idempotency_records",
    "launchplane_idempotency_scope_route_key_idx",
    "launchplane_idempotency_state_lease_idx",
    "launchplane_idempotency_active_reconciliation_idx",
    "launchplane_authz_policies",
    "launchplane_authz_policies_revision_uidx",
    "launchplane_authz_policies_active_uidx",
    "launchplane_secrets",
    "launchplane_secrets_scope_name_idx",
    "launchplane_secrets_lookup_idx",
    "launchplane_secret_versions",
    "launchplane_secret_versions_secret_idx",
    "launchplane_secret_bindings",
    "launchplane_secret_bindings_lookup_idx",
    "launchplane_secret_audit_events",
    "launchplane_secret_audit_events_secret_idx",
)


class PrivilegedOperationWorkerSchemaError(RuntimeError):
    pass


class PrivilegedOperationWorkerStartupTimeoutError(RuntimeError):
    pass


class PrivilegedOperationWorkerProbeError(RuntimeError):
    pass


def resolve_database_url(database_url: str | None = None) -> str | None:
    if database_url is not None and database_url.strip():
        return database_url.strip()
    for environment_key in DATABASE_URL_ENV_VARS:
        environment_value = os.environ.get(environment_key, "").strip()
        if environment_value:
            return environment_value
    return None


def _privileged_operation_worker_probe_environment(*, database_url: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in PRIVILEGED_OPERATION_WORKER_PROBE_ENVIRONMENT_KEYS or key.startswith("PG")
    }
    environment[DATABASE_URL_ENV_VARS[0]] = database_url
    return environment


def build_shared_record_store(*, database_url: str | None = None) -> PostgresRecordStore:
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        raise ValueError(
            "Launchplane shared storage requires --database-url or "
            "LAUNCHPLANE_DATABASE_URL. Filesystem state is local-only."
        )
    store = PostgresRecordStore(database_url=resolved_database_url)
    store.verify_schema()
    return store


def build_privileged_operation_worker_store(
    *, database_url: str | None = None
) -> PostgresRecordStore:
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        raise ValueError(
            "Launchplane privileged-operation workers require --database-url or "
            "LAUNCHPLANE_DATABASE_URL."
        )
    probe_environment = _privileged_operation_worker_probe_environment(
        database_url=resolved_database_url
    )
    try:
        completed_probe = subprocess.run(
            [
                sys.executable,
                "-m",
                "control_plane.storage.privileged_operation_worker_probe",
            ],
            env=probe_environment,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=PRIVILEGED_OPERATION_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise PrivilegedOperationWorkerStartupTimeoutError(
            "Launchplane privileged-operation worker store initialization timed out."
        ) from error
    if (
        completed_probe.returncode
        == PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE
    ):
        raise PrivilegedOperationWorkerSchemaError(
            "Launchplane privileged-operation worker schema is not runtime-compatible."
        )
    if completed_probe.returncode != PRIVILEGED_OPERATION_WORKER_PROBE_SUCCESS_EXIT_CODE:
        raise PrivilegedOperationWorkerProbeError(
            "Launchplane privileged-operation worker schema probe failed."
        )
    return PostgresRecordStore(
        database_url=resolved_database_url,
        postgres_connect_timeout_seconds=PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS,
        postgres_statement_timeout_milliseconds=(
            PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS
        ),
    )


def storage_backend_name(record_store: object) -> str:
    backend_name = getattr(record_store, "backend_name", "")
    if isinstance(backend_name, str) and backend_name.strip():
        return backend_name
    return "filesystem"
