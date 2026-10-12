"""Lossless bounded transport for scrubbed rows, independent of DB authority."""

from collections.abc import Iterator
import base64
import gzip
from io import BytesIO
import json
from typing import Any
import zlib

from control_plane.contracts.stored_record_snapshot import (
    CompressedStoredRecordSnapshot,
    SNAPSHOT_MAX_BYTES,
    SNAPSHOT_MAX_EXPANDED_BYTES,
    SNAPSHOT_MAX_ROWS,
    StoredRecordSnapshot,
)


class SnapshotRowEncoder:
    def __init__(self) -> None:
        self._compressor = zlib.compressobj(level=1, wbits=31)
        self._encoded = bytearray()
        self.expanded_bytes = 0
        self.table_counts: dict[str, int] = {}

    def _append(self, chunk: bytes) -> None:
        if 4 * ((len(self._encoded) + len(chunk) + 2) // 3) > SNAPSHOT_MAX_BYTES:
            raise ValueError("snapshot_byte_limit")
        self._encoded.extend(chunk)

    def add(self, table: str, row: dict[str, Any]) -> None:
        encoded = (
            json.dumps(
                {"table": table, "row": row}, separators=(",", ":"), ensure_ascii=False
            ).encode()
            + b"\n"
        )
        if len(encoded) > SNAPSHOT_MAX_BYTES:
            raise ValueError("snapshot_decoded_row_byte_limit")
        self.expanded_bytes += len(encoded)
        if self.expanded_bytes > SNAPSHOT_MAX_EXPANDED_BYTES:
            raise ValueError("snapshot_expanded_byte_limit")
        self._append(self._compressor.compress(encoded))
        self.table_counts[table] += 1

    def finish(self, *, captured_at: str, revision: str) -> CompressedStoredRecordSnapshot:
        self._append(self._compressor.flush())
        snapshot = CompressedStoredRecordSnapshot(
            captured_at=captured_at,
            database_schema_revision=revision,
            table_counts=self.table_counts,
            expanded_bytes=self.expanded_bytes,
            rows_gzip_base64=base64.b64encode(self._encoded).decode("ascii"),
        )
        if len(snapshot.model_dump_json().encode()) > SNAPSHOT_MAX_BYTES:
            raise ValueError("snapshot_byte_limit")
        return snapshot


def iter_snapshot_rows(
    snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot,
) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(snapshot, StoredRecordSnapshot):
        for table, rows in snapshot.tables.items():
            for row in rows:
                yield table, row
        return
    if len(snapshot.model_dump_json().encode()) > SNAPSHOT_MAX_BYTES:
        raise ValueError("snapshot_byte_limit")
    counts = {table: 0 for table in snapshot.table_counts}
    if any(count < 0 for count in snapshot.table_counts.values()):
        raise ValueError("snapshot_invalid_counts")
    if sum(snapshot.table_counts.values()) > SNAPSHOT_MAX_ROWS:
        raise ValueError("snapshot_row_limit")
    compressed = base64.b64decode(snapshot.rows_gzip_base64, validate=True)
    expanded_bytes = 0
    row_count = 0
    with gzip.GzipFile(fileobj=BytesIO(compressed)) as stream:
        while encoded := stream.readline(SNAPSHOT_MAX_BYTES + 1):
            expanded_bytes += len(encoded)
            row_count += 1
            if len(encoded) > SNAPSHOT_MAX_BYTES:
                raise ValueError("snapshot_decoded_row_byte_limit")
            if expanded_bytes > SNAPSHOT_MAX_EXPANDED_BYTES:
                raise ValueError("snapshot_expanded_byte_limit")
            if row_count > SNAPSHOT_MAX_ROWS:
                raise ValueError("snapshot_row_limit")
            if not encoded.endswith(b"\n"):
                raise ValueError("snapshot_incomplete_row")
            item = json.loads(encoded)
            if not isinstance(item, dict) or set(item) != {"table", "row"}:
                raise ValueError("snapshot_invalid_row")
            table, row = item["table"], item["row"]
            if not isinstance(table, str) or table not in counts or not isinstance(row, dict):
                raise ValueError("snapshot_invalid_row")
            counts[table] += 1
            yield table, row
    if counts != snapshot.table_counts or expanded_bytes != snapshot.expanded_bytes:
        raise ValueError("snapshot_incomplete_records")
