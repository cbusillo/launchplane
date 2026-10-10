"""File-backed isolation/rehearsal storage, never shared production authority."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from control_plane.contracts.prepared_public_site import PreparedPublicSite, PublicPauseRecord
from control_plane.prepared_public_site import verify_snapshot


def _atomic_json(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def save_prepared_site(path: Path, site: PreparedPublicSite) -> None:
    verify_snapshot(site)
    if path.exists():
        if load_prepared_site(path) != site:
            raise ValueError("cannot replace a retained prepared public copy")
        return
    _atomic_json(path, site.model_dump_json())


def load_prepared_site(path: Path) -> PreparedPublicSite:
    site = PreparedPublicSite.model_validate_json(path.read_text())
    verify_snapshot(site)
    return site


class FilePublicPauseStore:
    def __init__(self, state_directory: Path) -> None:
        self.state_directory = state_directory

    def _path(self, pause_id: str) -> Path:
        if len(pause_id) != 32 or any(char not in "0123456789abcdef" for char in pause_id):
            raise ValueError("invalid pause identity")
        return self.state_directory / (pause_id + ".json")

    def save(self, record: PublicPauseRecord) -> None:
        _atomic_json(self._path(record.pause_id), record.model_dump_json())

    def load(self, pause_id: str) -> PublicPauseRecord:
        return PublicPauseRecord.model_validate_json(self._path(pause_id).read_text())
