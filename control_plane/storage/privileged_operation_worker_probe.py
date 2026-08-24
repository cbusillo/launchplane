from __future__ import annotations

from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError

from control_plane.storage.factory import (
    PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS,
    PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE,
    PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE,
    PRIVILEGED_OPERATION_WORKER_PROBE_SUCCESS_EXIT_CODE,
    PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS,
    PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS,
    resolve_database_url,
)
from control_plane.storage.postgres import PostgresRecordStore


def run_privileged_operation_worker_schema_probe() -> int:
    database_url = resolve_database_url()
    if database_url is None:
        return PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE
    store: PostgresRecordStore | None = None
    try:
        store = PostgresRecordStore(
            database_url=database_url,
            postgres_connect_timeout_seconds=PRIVILEGED_OPERATION_WORKER_CONNECT_TIMEOUT_SECONDS,
            postgres_statement_timeout_milliseconds=(
                PRIVILEGED_OPERATION_WORKER_STATEMENT_TIMEOUT_MILLISECONDS
            ),
        )
        try:
            store.verify_runtime_schema_compatibility(
                required_relations=PRIVILEGED_OPERATION_WORKER_REQUIRED_RELATIONS
            )
        except RuntimeError:
            return PRIVILEGED_OPERATION_WORKER_PROBE_SCHEMA_INCOMPATIBLE_EXIT_CODE
        except (OSError, SQLAlchemyError, ValueError):
            return PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE
    except (ImportError, OSError, SQLAlchemyError, ValueError):
        return PRIVILEGED_OPERATION_WORKER_PROBE_FAILED_EXIT_CODE
    finally:
        if store is not None:
            with suppress(OSError, RuntimeError, SQLAlchemyError):
                store.close()
    return PRIVILEGED_OPERATION_WORKER_PROBE_SUCCESS_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(run_privileged_operation_worker_schema_probe())
