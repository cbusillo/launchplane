from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
import fcntl
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast

from control_plane.contracts.odoo_testing_source import (
    MigratedOdooTestingSource,
    NativeTestingCopyCapture,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.odoo_testing_native_source import _cache_directory, verify_native_capture_files
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

UploadPart = Literal["database", "filestore"]


class UploadDigest(Protocol):
    def update(self, data: bytes) -> None: ...


def migrated_candidate(
    store: PostgresRecordStore, product: str, operation_id: str, *, allow_complete: bool = False
) -> TestingCandidateAllocation:
    operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
    allocation = store.read_testing_candidate(
        testing_candidate_namespace(product, operation.context, operation_id)
    )
    if (
        operation.product != product
        or not operation.is_native_testing_rebuild
        or operation.request.migrated_source is None
        or allocation is None
        or allocation.application is None
    ):
        raise ValueError("The original migrated testing candidate is unavailable")
    app = allocation.application
    if (
        app.plan.source != "migrated"
        or app.plan.generation is None
        or not allow_complete
        and (app.phase in {"prepared", "abandoned"} or app.restore_workflow_started is True)
        or app.plan.native_source != operation.request.migrated_source
    ):
        raise ValueError("Migrated input no longer belongs to an unstarted testing restore")
    return allocation


def upload_directory(allocation: TestingCandidateAllocation, *, create: bool = True) -> Path:
    assert allocation.application is not None and allocation.application.plan.generation is not None
    source = allocation.application.plan.generation.migrated_source
    if source is None:
        raise ValueError("The candidate has no migrated source")
    binding = source.binding_sha256
    path = _cache_directory(binding, create=create) / binding[:32]
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("The original migrated source directory changed")
    return path


def file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Migrated input must be a regular file")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def upload_state(allocation: TestingCandidateAllocation) -> dict[str, dict[str, object]]:
    directory = upload_directory(allocation, create=False)
    result: dict[str, dict[str, object]] = {}
    for part in ("database", "filestore"):
        complete = directory / part
        path = complete if complete.exists() else directory / (part + ".partial")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Migrated upload state contains an unsafe file")
        result[part] = {
            "complete": complete.exists(),
            "size": path.stat().st_size if path.exists() else 0,
        }
    return result


@dataclass
class MigratedUpload:
    handle: BinaryIO
    remaining: int
    digest: UploadDigest
    reserve_bytes: int = 0

    def write(self, data: bytes) -> None:
        if len(data) > self.remaining:
            raise ValueError("Migrated input exceeds its admitted artifact size")
        space = os.fstatvfs(self.handle.fileno())
        if space.f_bavail * space.f_frsize < len(data) + self.reserve_bytes:
            raise ValueError("The upload must preserve the existing testing storage reserve")
        self.handle.write(data)
        self.digest.update(data)
        self.remaining -= len(data)


@contextmanager
def receive_migrated_part(
    allocation: TestingCandidateAllocation, part: UploadPart, offset: int, prefix_sha256: str
) -> Iterator[MigratedUpload]:
    assert allocation.application is not None
    source = allocation.application.plan.native_source
    if not isinstance(source, MigratedOdooTestingSource):
        raise ValueError("The candidate has no migrated output intent")
    expected = getattr(source, part)
    directory = upload_directory(allocation)
    lock = os.open(
        directory / "upload.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("Upload lock is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        complete = directory / part
        if complete.exists() or complete.is_symlink():
            if (
                offset != expected.size
                or prefix_sha256 != expected.sha256
                or file_digest(complete) != expected.sha256
            ):
                raise ValueError("The completed artifact differs from the original upload")
            with complete.open("rb") as handle:
                yield MigratedUpload(handle, 0, hashlib.sha256())
            return
        partial = directory / (part + ".partial")
        descriptor = os.open(partial, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Partial upload is not a regular file")
            digest = hashlib.sha256()
            while content := handle.read(1024 * 1024):
                digest.update(content)
            if (
                handle.tell() != offset
                or offset > expected.size
                or digest.hexdigest() != prefix_sha256
            ):
                raise ValueError("The retained partial differs from the original upload prefix")
            generation = allocation.application.plan.generation
            if generation is None or generation.drill_storage_budget is None:
                raise ValueError("The original testing storage headroom is unavailable")
            state = upload_state(allocation)
            required = sum(
                max(0, getattr(source, key).size - cast(int, item["size"]))
                for key, item in state.items()
            )
            if (
                shutil.disk_usage(directory).free
                < required + generation.drill_storage_budget.reserve_bytes
            ):
                raise ValueError(
                    "Private copy volume cannot hold the remaining pair and testing reserve"
                )
            upload = MigratedUpload(
                handle,
                expected.size - offset,
                digest,
                generation.drill_storage_budget.reserve_bytes,
            )
            yield upload
            handle.flush()
            os.fsync(handle.fileno())
            if upload.remaining or digest.hexdigest() != expected.sha256:
                raise ValueError(
                    "Migrated artifact is incomplete or differs from its admitted digest"
                )
        os.link(partial, complete, follow_symlinks=False)
        partial.unlink()
        sync = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(sync)
        finally:
            os.close(sync)
    finally:
        os.close(lock)


def finish_migrated_upload(
    store: PostgresRecordStore, allocation: TestingCandidateAllocation
) -> NativeTestingCopyCapture | None:
    assert allocation.application is not None and allocation.application.plan.generation is not None
    generation = allocation.application.plan.generation
    source = generation.migrated_source
    if source is None:
        raise ValueError("The generation has no migrated output")
    binding = generation_binding(generation)
    existing = store.read_native_testing_copy_capture(binding)
    if existing is not None:
        verify_native_capture_files(existing)
        return existing
    directory = upload_directory(allocation)
    for part in ("database", "filestore"):
        path = directory / part
        if not path.exists():
            return None
        expected = getattr(source, part)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected.size:
            raise ValueError("The saved migrated artifact changed before receipt commit")
    saved = directory / ("capture-" + binding + ".json")
    if saved.is_symlink():
        raise ValueError("The retained migrated receipt is unsafe")
    if saved.exists():
        capture = NativeTestingCopyCapture.model_validate_json(saved.read_text())
    else:
        capture = NativeTestingCopyCapture(
            product=generation.product,
            context=generation.context,
            generation_binding_sha256=binding,
            source_binding_sha256=source.binding_sha256,
            capture_id=source.binding_sha256[:32],
            database=source.database,
            filestore=source.filestore,
            captured_at=utc_now_timestamp(),
            source_observation=source,
        )
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=directory, prefix=".capture-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(capture.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, saved, follow_symlinks=False)
            except FileExistsError:
                capture = NativeTestingCopyCapture.model_validate_json(saved.read_text())
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if (
        capture.source_observation != source
        or capture.database != source.database
        or capture.filestore != source.filestore
        or capture.generation_binding_sha256 != binding
    ):
        raise ValueError("The retained receipt differs from the original migrated pair")
    store.write_native_testing_copy_capture(capture, candidate_namespace=allocation.namespace)
    return capture
