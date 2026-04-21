from __future__ import annotations

import os
from pathlib import Path

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore

DATABASE_URL_ENV_VARS = ("LAUNCHPLANE_DATABASE_URL",)


def resolve_database_url(database_url: str | None = None) -> str | None:
    if database_url is not None and database_url.strip():
        return database_url.strip()
    for environment_key in DATABASE_URL_ENV_VARS:
        environment_value = os.environ.get(environment_key, "").strip()
        if environment_value:
            return environment_value
    return None


def build_record_store(*, state_dir: Path, database_url: str | None = None) -> FilesystemRecordStore | PostgresRecordStore:
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        return FilesystemRecordStore(state_dir=state_dir)
    store = PostgresRecordStore(database_url=resolved_database_url)
    store.ensure_schema()
    return store


def storage_backend_name(record_store: object) -> str:
    backend_name = getattr(record_store, "backend_name", "")
    if isinstance(backend_name, str) and backend_name.strip():
        return backend_name
    return "filesystem"
