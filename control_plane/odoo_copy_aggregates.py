from __future__ import annotations

from datetime import datetime, timezone
import inspect
from typing import Annotated, BinaryIO, Literal
import re

from pydantic import BaseModel, ConfigDict, Field

from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane.clean_testing_snapshot import preview_snapshot
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceAuthorizationDenied
from control_plane.launchplane_mutations import control_plane_root
from control_plane.odoo_runtime_reads import OdooRuntimeReadError
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import PostgresRecordStore


ROUTE = "/v1/products/{product}/environments/{environment}/aggregate-counts"
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$", max_length=63)]
Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
MAX_ROW_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_GROUP_BYTES = 128 * 1024 * 1024
MAX_ROWS = 2_000_000


class AggregateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: Identifier
    key: Identifier
    partition: Identifier | None = None
    active: Identifier = "active"


class AggregateCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_rows: Count
    duplicate_groups: Count
    duplicate_rows: Count
    missing_key_rows: Count


class AggregateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    generation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_captured_at: str
    source_mode: Literal["ssh", "container-export"]
    source_binding_sha256: str
    operation_id: str
    observed_at: str
    sanitization_verified: Literal[True] = True
    resources_isolated: Literal[True] = True
    read_only: Literal[True] = True
    counts: AggregateCounts


def decode_copy_field(value: bytes) -> str | None:
    if value == b"\\N":
        return None
    output = bytearray()
    escapes = {ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("v"): 11}
    position = 0
    while position < len(value):
        character = value[position]
        position += 1
        if character != 92:
            output.append(character)
            continue
        if position == len(value):
            raise ValueError("Invalid COPY escape")
        character = value[position]
        position += 1
        if 48 <= character <= 55:
            digits = bytes([character])
            while position < len(value) and len(digits) < 3 and 48 <= value[position] <= 55:
                digits += bytes([value[position]])
                position += 1
            output.append(int(digits, 8))
        elif character == ord("x"):
            digits = b""
            while (
                position < len(value)
                and len(digits) < 2
                and value[position] in b"0123456789abcdefABCDEF"
            ):
                digits += bytes([value[position]])
                position += 1
            if not digits:
                raise ValueError("Invalid COPY hex escape")
            output.append(int(digits, 16))
        else:
            output.append(escapes.get(character, character))
    return output.decode("utf-8")


def count_archive_rows(stream: BinaryIO, query: dict[str, str | None]) -> dict[str, int]:
    import csv

    header = re.compile(
        rb'^COPY public\.(?:"'
        + str(query["table"]).encode()
        + rb'"|'
        + str(query["table"]).encode()
        + rb") \((.+)\) FROM stdin;\n$"
    )
    columns: list[str] | None = None
    completed = False
    total_bytes = rows = active_rows = missing = group_bytes = 0
    groups: dict[tuple[str, str | None], int] = {}
    while line := stream.readline(MAX_ROW_BYTES + 1):
        total_bytes += len(line)
        if (
            len(line) > MAX_ROW_BYTES
            or total_bytes > MAX_EXTRACTED_BYTES
            or not line.endswith(b"\n")
        ):
            raise ValueError("Archive extraction exceeds its bound")
        if columns is None:
            if line.startswith(b"COPY "):
                match = header.fullmatch(line)
                if match is None:
                    raise ValueError("Archive table selection changed")
                columns = next(csv.reader([match[1].decode("utf-8")], skipinitialspace=True))
                if len(columns) != len(set(columns)) or any(
                    name is not None and name not in columns
                    for name in (query[field] for field in ("key", "partition", "active"))
                ):
                    raise ValueError("Archive columns unavailable")
            continue
        if line == b"\\.\n":
            if completed:
                raise ValueError("Duplicate archive terminator")
            completed = True
            continue
        if completed:
            if line.startswith(b"COPY "):
                raise ValueError("Multiple archive tables")
            continue
        fields = line[:-1].split(b"\t")
        if len(fields) != len(columns):
            raise ValueError("Malformed archive row")
        rows += 1
        if rows > MAX_ROWS:
            raise ValueError("Archive row bound exceeded")
        active = decode_copy_field(fields[columns.index(str(query["active"]))])
        if active not in {"t", "f", None}:
            raise ValueError("Archive active column is not boolean")
        active_rows += active == "t"
        key = (decode_copy_field(fields[columns.index(str(query["key"]))]) or "").strip(" ").lower()
        if not key:
            missing += 1
            continue
        partition = (
            decode_copy_field(fields[columns.index(str(query["partition"]))])
            if query["partition"] is not None
            else None
        )
        group = (key, partition)
        if group not in groups:
            group_bytes += len(key.encode()) + len((partition or "").encode()) + 256
            if group_bytes > MAX_GROUP_BYTES:
                raise ValueError("Archive grouping bound exceeded")
        groups[group] = groups.get(group, 0) + 1
    if columns is None or not completed:
        raise ValueError("Incomplete archive table")
    duplicates = [count for count in groups.values() if count > 1]
    return dict(
        active_rows=active_rows,
        duplicate_groups=len(duplicates),
        duplicate_rows=sum(duplicates),
        missing_key_rows=missing,
    )


ARCHIVE_COUNT_LIBRARY = (
    "from typing import BinaryIO\n"
    + f"MAX_ROW_BYTES={MAX_ROW_BYTES}\nMAX_EXTRACTED_BYTES={MAX_EXTRACTED_BYTES}\nMAX_GROUP_BYTES={MAX_GROUP_BYTES}\nMAX_ROWS={MAX_ROWS}\n"
    + inspect.getsource(decode_copy_field)
    + inspect.getsource(count_archive_rows)
)

SNAPSHOT_AGGREGATE_PROGRAM = (
    CLEAN_TESTING_SNAPSHOT_LIBRARY
    + ARCHIVE_COUNT_LIBRARY
    + r"""
import base64,signal,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
process=None
stage='snapshot_unavailable'
def expired(*arguments):
    raise TimeoutError()
signal.signal(signal.SIGALRM,expired)
signal.alarm(90)
try:
    snapshot=spec['snapshot']
    path,receipt=clean_snapshot_receipt(snapshot['snapshot_id'])
    if any(receipt.get(key)!=snapshot[key] for key in ('database_sha256','filestore_sha256')):
        raise ValueError()
    descriptor=os.open(path/'database.dump',os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor,'rb') as archive:
        if not stat.S_ISREG(os.fstat(archive.fileno()).st_mode) or os.fstat(archive.fileno()).st_size>snapshot['storage_budget']['database_bytes']:
            raise ValueError()
        if hashlib.file_digest(archive,'sha256').hexdigest()!=snapshot['database_sha256']:
            raise ValueError()
        identity=os.fstat(archive.fileno())
        bindings=subprocess.check_output(['docker','ps','-aq'],timeout=10).decode().split()
        if len(bindings)>1024 or any(not re.fullmatch(r'[0-9a-f]{12,64}',item) for item in bindings):
            raise ValueError()
        for identifier in bindings:
            raw=subprocess.check_output(['docker','inspect','--format','{{json .Mounts}}',identifier],timeout=5)
            if len(raw)>65536:
                raise ValueError()
            mounts=json.loads(raw)
            if not isinstance(mounts,list):
                raise ValueError()
            for mount in mounts:
                if isinstance(mount,dict) and mount.get('Type')=='tmpfs':
                    continue
                if not isinstance(mount,dict) or not isinstance(mount.get('Source'),str) or not mount['Source']:
                    raise ValueError()
                source_path=Path(mount['Source']).resolve()
                if path.is_relative_to(source_path) or source_path.is_relative_to(path):
                    raise ValueError()
        archive.seek(0)
        stage='archive_tool_unavailable'
        identifiers=subprocess.check_output(['docker','ps','-q','--filter','label=com.docker.compose.project='+spec['project'],'--filter','label=com.docker.compose.service=database'],timeout=10).decode().split()
        if len(identifiers)!=1 or not re.fullmatch(r'[0-9a-f]{12,64}',identifiers[0]):
            raise ValueError()
        stage='archive_unreadable'
        process=subprocess.Popen(['docker','exec','-i',identifiers[0],'env','-i','PATH=/usr/local/bin:/usr/bin:/bin',
            'pg_restore','--data-only','--no-owner','--no-privileges','--file=-','--schema=public','--table='+spec['query']['table']],
            stdin=archive,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        counts=count_archive_rows(process.stdout,spec['query'])
        if process.wait(timeout=10)!=0:
            raise ValueError()
        after=os.fstat(archive.fileno())
        if any(getattr(identity,key)!=getattr(after,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
            raise ValueError()
    result={'type':'complete','counts':counts}
except Exception:
    result={'type':'complete','unavailable':stage}
finally:
    if process is not None:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    signal.alarm(0)
print(prefix+json.dumps(result),flush=True)
"""
)


def read_aggregates(
    *, store: PostgresRecordStore, product: str, query: AggregateQuery
) -> AggregateEvidence:
    profile = store.read_product_profile_record(product)
    if not profile.is_active or profile.driver_id != "odoo":
        raise ValueError("Aggregate reads require an active Odoo copy source.")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    snapshot = preview_snapshot(store, profile)
    generation = store.read_fresh_odoo_testing_generation(product, lane.context)
    if (
        generation is None
        or not generation.completed_at
        or generation_binding(generation) != snapshot.snapshot_id
    ):
        raise ValueError("The clean snapshot lacks its completed production-copy provenance.")
    target = store.read_dokploy_target_record(context_name=lane.context, instance_name="testing")
    target_id = store.read_dokploy_target_id_record(
        context_name=lane.context, instance_name="testing"
    )
    if target.target_type != "compose" or any(
        other.target_id == target_id.target_id
        and (other.context, other.instance) != (lane.context, "testing")
        for other in store.list_dokploy_target_id_records()
    ):
        raise ValueError("The archive utility target is not exclusive testing.")
    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root(), database_url=store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    if provider.get("serverId") != snapshot.server_id or provider.get("name") != target.target_name:
        raise ValueError("The archive utility target differs from snapshot custody.")
    frames: list[dict[str, object]] = []
    try:
        _read_remote(
            host=host,
            token=token,
            server_id=snapshot.server_id,
            program=SNAPSHOT_AGGREGATE_PROGRAM,
            payload={
                "snapshot": snapshot.model_dump(mode="json"),
                "query": query.model_dump(mode="json"),
                "project": str(provider.get("appName") or ""),
            },
            receive=frames.append,
            timeout_seconds=120,
        )
    except NativeSourceAuthorizationDenied:
        raise OdooRuntimeReadError(
            "authorization_denied", "The provider refused the snapshot aggregate read.", 403
        ) from None
    if len(frames) != 1 or set(frames[0]) != {"type", "counts"} or frames[0]["type"] != "complete":
        reason = frames[0].get("unavailable") if len(frames) == 1 else None
        code = (
            reason
            if reason in {"snapshot_unavailable", "archive_tool_unavailable", "archive_unreadable"}
            else "aggregate_unavailable"
        )
        raise OdooRuntimeReadError(
            str(code), "The qualified snapshot aggregate read did not complete.", 503
        )
    return AggregateEvidence(
        product=product,
        context=lane.context,
        generation_sha256=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_database_sha256=snapshot.database_sha256,
        snapshot_captured_at=snapshot.captured_at,
        source_mode=generation.source_mode,
        source_binding_sha256=generation.source_binding_sha256,
        operation_id=generation.operation_id,
        observed_at=datetime.now(timezone.utc).isoformat(),
        counts=AggregateCounts.model_validate(frames[0]["counts"]),
    )
