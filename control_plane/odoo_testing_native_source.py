"""Same-product container-native reads and private immutable testing-copy artifacts."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast, TypeVar
import uuid

from control_plane.contracts.odoo_testing_source import (
    NATIVE_TESTING_COPY_CHUNK_BYTES,
    NATIVE_TESTING_COPY_CACHE_DIRECTORY,
    NativeOdooSourceAuthority,
    NativeOdooTestingSource,
    NativeTestingCopyArtifact,
    NativeTestingCopyCapture,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import read_native_source, NativeSourceUnavailable
from control_plane.dokploy.native_copy_stream import inspect_native_testing_receiver
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.dokploy.native_copy_stream import NativeUploadRejected
from collections.abc import Callable
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.contracts.canonical_json import canonical_json_sha256

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore
    from control_plane.odoo_testing_restore import FreshOdooTestingGeneration

Result = TypeVar("Result")


def _provider_source_read(read: Callable[[], Result]) -> Result:
    try:
        return read()
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied(
                "The provider refused the registered source read."
            ) from None
        raise NativeSourceUnavailable("The registered source inventory is unavailable.") from None


def native_testing_source_authority(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    testing_context: str,
) -> NativeOdooSourceAuthority:
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("Native copy requires one same-product production source.")
    lane = production[0]
    if not is_exclusive_product_context(
        context=lane.context,
        product=profile.product,
        owners=product_context_owner_map(store.list_product_profile_records()),
    ):
        raise ValueError("The production source context is not exclusively owned.")
    target = store.read_dokploy_target_record(context_name=lane.context, instance_name="prod")
    identity = store.read_dokploy_target_id_record(context_name=lane.context, instance_name="prod")
    if (
        len(
            [
                item
                for item in profile.lanes
                if (item.context, item.instance) == (testing_context, "testing")
            ]
        )
        != 1
    ):
        raise ValueError("Native copy requires its exact owned testing recipient.")
    recipient = store.read_dokploy_target_id_record(
        context_name=testing_context, instance_name="testing"
    )
    if target.target_type != "compose" or identity.target_id == recipient.target_id:
        raise ValueError("Native source and testing require different registered compose targets.")
    values: dict[str, str] = {}
    versions: list[dict[str, str]] = []
    for scope in ("context", "instance"):
        for runtime in store.list_runtime_environment_records(context_name=lane.context):
            if runtime.scope == scope and runtime.instance == (
                "prod" if scope == "instance" else ""
            ):
                values.update({key: str(value) for key, value in runtime.env.items()})
                versions.append({"scope": runtime.scope, "updated_at": runtime.updated_at})
    values.update({key: str(value) for key, value in target.env.items()})
    database = values.get("ODOO_DB_NAME", "")
    filestore = Path(values.get("ODOO_FILESTORE_PATH", "/volumes/data/filestore"))
    if not filestore.is_absolute() or ".." in filestore.parts:
        raise ValueError("The source filestore needs its managed absolute path.")
    if filestore.name != database:
        filestore /= database
    return NativeOdooSourceAuthority(
        product=profile.product,
        context=lane.context,
        target_id=identity.target_id,
        database_name=database,
        filestore_path=str(filestore),
        settings_sha256=canonical_json_sha256(
            {
                "lane": lane.model_dump(mode="json"),
                "runtime_versions": versions,
                "target_updated_at": target.updated_at,
            }
        ),
    )


def recorded_native_testing_capture(
    store: object, generation: FreshOdooTestingGeneration
) -> NativeTestingCopyCapture | None:
    from control_plane.odoo_testing_restore import generation_binding

    if generation.source_mode != "container-export":
        return None
    binding = generation_binding(generation)
    capture = getattr(store, "read_native_testing_copy_capture")(binding)
    if capture is not None and (
        not isinstance(capture, NativeTestingCopyCapture)
        or capture.generation_binding_sha256 != binding
        or capture.product != generation.product
        or capture.context != generation.context
        or capture.source_binding_sha256 != generation.source_binding_sha256
    ):
        raise ValueError("The native copy receipt differs from its generation.")
    return capture


def resolve_native_testing_source(
    store: PostgresRecordStore,
    *,
    root: Path,
    profile: LaunchplaneProductProfileRecord,
    testing_context: str,
    probe: bool = False,
) -> NativeOdooTestingSource:
    authority = native_testing_source_authority(
        store, profile=profile, testing_context=testing_context
    )
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = _provider_source_read(
        lambda: api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=authority.target_id
        )
    )
    server = str(payload.get("serverId") or "")
    project = str(payload.get("appName") or "")
    if server == "local" or not re.fullmatch(r"[a-zA-Z0-9_-]+", server):
        raise ValueError("Native copy refuses unregistered or key-generating local terminals.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", project):
        raise ValueError("The source compose project is unavailable.")
    containers = _provider_source_read(
        lambda: api.dokploy_request(
            host=host,
            token=token,
            path="/api/docker.getContainersByAppNameMatch",
            query={"appName": project, "appType": "docker-compose", "serverId": server},
        )
    )
    if not isinstance(containers, list):
        raise ValueError("The source container inventory is unavailable.")
    selected: dict[str, dict[str, api.JsonValue]] = {}
    for entry in containers:
        if not isinstance(entry, dict) or entry.get("state") != "running":
            continue
        identifier = str(entry.get("containerId") or "")
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise ValueError("The source container identity is invalid.")
        inspection = _provider_source_read(
            lambda: api.dokploy_request(
                host=host,
                token=token,
                path="/api/docker.getConfig",
                query={"containerId": identifier, "serverId": server},
            )
        )
        if not isinstance(inspection, dict):
            raise ValueError("The source inspection is unavailable.")
        configuration = inspection.get("Config")
        if not isinstance(configuration, dict):
            raise ValueError("The source configuration is unavailable.")
        labels = configuration.get("Labels")
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise ValueError("The source container is outside its registered compose.")
        role = str(labels.get("com.docker.compose.service") or "")
        if role in {"database", "web"}:
            if role in selected:
                raise ValueError("The source service is ambiguous.")
            selected[role] = inspection
    if selected.keys() != {"database", "web"}:
        raise ValueError("The source requires one running database and web container.")
    result = NativeOdooTestingSource(
        **authority.model_dump(mode="json"),
        server_id=server,
        compose_project=project,
        database_container=str(selected["database"].get("Id") or ""),
        web_container=str(selected["web"].get("Id") or ""),
        database_image=str(selected["database"].get("Image") or ""),
        web_image=str(selected["web"].get("Image") or ""),
    )
    if probe:
        frames: list[dict[str, object]] = []
        read_native_source(
            host=host,
            token=token,
            server_id=server,
            source=result.model_dump(mode="json"),
            mode="probe",
            receive=frames.append,
            timeout_seconds=90,
        )
        if len(frames) != 1 or frames[0] != {"type": "complete", "probe": True}:
            raise NativeSourceUnavailable("The production source capability is unverified.")
    return result


def _cache_directory(generation_binding: str) -> Path:
    state = os.environ.get("LAUNCHPLANE_STATE_DIR", "")
    if not state or not re.fullmatch(r"[a-f0-9]{64}", generation_binding):
        raise ValueError("Native copy needs the service's explicit artifact state directory.")
    root = Path(state) / NATIVE_TESTING_COPY_CACHE_DIRECTORY / generation_binding
    if not root.is_absolute() or root.resolve() != root.absolute():
        raise ValueError("Native copy cache cannot traverse symlinks.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def verify_native_capture_files(capture: NativeTestingCopyCapture) -> tuple[Path, Path]:
    directory = _cache_directory(capture.generation_binding_sha256) / capture.capture_id
    files = []
    for part in ("database", "filestore"):
        path = directory / part
        expected = getattr(capture, part)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected.size:
            raise ValueError(
                "The original native copy artifact is unavailable; retain its receipt."
            )
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != expected.sha256:
            raise ValueError("The original native copy artifact failed integrity readback.")
        files.append(path)
    return files[0], files[1]


def capture_native_testing_source(
    store: PostgresRecordStore,
    *,
    root: Path,
    generation: FreshOdooTestingGeneration,
    descriptor: NativeOdooTestingSource,
) -> NativeTestingCopyCapture:
    from control_plane.odoo_testing_restore import generation_binding

    binding = generation_binding(generation)
    if (
        generation.source_mode != "container-export"
        or descriptor.binding_sha256 != generation.source_binding_sha256
    ):
        raise ValueError("Native copy differs from its reviewed generation source.")
    existing = store.read_native_testing_copy_capture(binding)
    if existing is not None:
        if existing.source_binding_sha256 != descriptor.binding_sha256:
            raise ValueError("The recorded copy source changed; do not recapture.")
        verify_native_capture_files(existing)
        return existing
    cache = _cache_directory(binding)
    capture_id = uuid.uuid4().hex
    destination = cache / capture_id
    host, token = source.read_dokploy_config(control_plane_root=root)
    with TemporaryDirectory(prefix="capture-", dir=cache) as temporary:
        directory = Path(temporary)
        handles = {part: (directory / part).open("xb") for part in ("database", "filestore")}
        digests = {part: hashlib.sha256() for part in handles}
        sizes = {part: 0 for part in handles}
        completion: dict[str, object] = {}

        def receive(frame: dict[str, object]) -> None:
            if frame.get("type") == "complete":
                completion.update(frame)
                return
            if frame.get("type") != "data" or frame.get("part") not in handles:
                raise NativeSourceUnavailable("The source returned an unexpected artifact frame.")
            part = str(frame["part"])
            data = base64.b64decode(str(frame.get("data") or ""), validate=True)
            sizes[part] += len(data)
            if sizes[part] > 128 * 1024 * 1024:
                raise NativeSourceUnavailable("The source artifact exceeds the transport bound.")
            digests[part].update(data)
            handles[part].write(data)

        try:
            read_native_source(
                host=host,
                token=token,
                server_id=descriptor.server_id,
                source=descriptor.model_dump(mode="json"),
                mode="capture",
                receive=receive,
            )
            artifacts = {
                part: NativeTestingCopyArtifact(sha256=digests[part].hexdigest(), size=sizes[part])
                for part in handles
            }
            if any(completion.get(part) != value.model_dump() for part, value in artifacts.items()):
                raise NativeSourceUnavailable(
                    "The source completion witness differs from its bytes."
                )
            for part, handle in handles.items():
                os.fchmod(handle.fileno(), 0o600)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
            capture = NativeTestingCopyCapture(
                product=generation.product,
                context=generation.context,
                generation_binding_sha256=binding,
                source_binding_sha256=descriptor.binding_sha256,
                capture_id=capture_id,
                database=artifacts["database"],
                filestore=artifacts["filestore"],
                captured_at=utc_now_timestamp(),
                source_observation=descriptor,
            )
            # The DB receipt is immutable. A crash before its write leaves an
            # unreferenced private cache, never permission to restore those bytes.
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(directory, destination)
            descriptor_fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor_fd)
            finally:
                os.close(descriptor_fd)
            # A failed commit reply may still have committed. Keep private bytes
            # even on an exception; deleting them can destroy the recorded copy.
            # Unreferenced private caches have a documented custody cleanup path.
            store.write_native_testing_copy_capture(capture)
            return capture
        finally:
            for handle in handles.values():
                handle.close()


def prepare_native_testing_delivery(
    store: PostgresRecordStore,
    *,
    root: Path,
    generation: FreshOdooTestingGeneration,
    descriptor: NativeOdooTestingSource,
    host: str,
    token: str,
    server_id: str,
    spec: dict[str, object],
    before_mutation: Callable[[str], None] | None,
) -> NativeTestingCopyCapture:
    """Qualify before reading source; resume only the original capture bytes."""
    from control_plane.odoo_testing_restore import generation_binding

    capture = store.read_native_testing_copy_capture(generation_binding(generation))
    if capture is not None and capture.source_binding_sha256 != generation.source_binding_sha256:
        raise ValueError("The original copy receipt no longer matches its generation.")

    def inspect(
        current: NativeTestingCopyCapture | None,
    ) -> tuple[str, bool, dict[tuple[str, int], dict[str, object]], dict[str, object]]:
        frames: list[dict[str, object]] = []
        inspect_native_testing_receiver(
            host=host,
            token=token,
            server_id=server_id,
            spec={**spec, "native_capture": current.model_dump(mode="json") if current else None},
            receive=frames.append,
        )
        complete = [item for item in frames if item.get("type") == "complete"]
        if (
            len(complete) != 1
            or not re.fullmatch(r"[a-f0-9]{64}", str(complete[0].get("runner") or ""))
            or not isinstance(complete[0].get("resume"), bool)
        ):
            raise ValueError("The isolated testing receiver is unverified.")
        chunks: dict[tuple[str, int], dict[str, object]] = {}
        for item in frames:
            if item.get("type") == "complete":
                continue
            if (
                item.get("type") != "chunk"
                or item.get("part") not in {"database", "filestore"}
                or type(item.get("index")) is not int
            ):
                raise ValueError("The testing artifact readback is invalid.")
            key = (str(item["part"]), cast(int, item["index"]))
            if key in chunks:
                raise ValueError("The testing artifact readback is ambiguous.")
            chunks[key] = item
        artifacts = complete[0].get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ValueError("The native artifact readback is invalid.")
        return str(complete[0]["runner"]), bool(complete[0]["resume"]), chunks, artifacts

    runner, resume, observed, artifacts = inspect(capture)
    if resume:
        if capture is None:
            raise ValueError("The restored database has no immutable source capture receipt.")
        return capture
    if capture is not None and artifacts == {
        part: getattr(capture, part).model_dump() for part in ("database", "filestore")
    }:
        return capture
    if capture is None:
        capture = capture_native_testing_source(
            store, root=root, generation=generation, descriptor=descriptor
        )
        runner, resume, observed, _ = inspect(capture)
        if resume:
            raise ValueError("The testing database appeared during source capture.")
    files = verify_native_capture_files(capture)
    expected: dict[tuple[str, int], dict[str, object]] = {}
    delivery_started = False
    for part, path in zip(("database", "filestore"), files, strict=True):
        with path.open("rb") as handle:
            index = 0
            while content := handle.read(NATIVE_TESTING_COPY_CHUNK_BYTES):
                key = (part, index)
                value = {
                    "type": "chunk",
                    "part": part,
                    "index": index,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                expected[key] = value
                upload_required = True
                if key in observed:
                    if observed[key] == value:
                        upload_required = False
                    else:
                        size = observed[key].get("size")
                        if (
                            not isinstance(size, int)
                            or isinstance(size, bool)
                            or not 0 <= size < len(content)
                            or observed[key].get("sha256")
                            != hashlib.sha256(content[:size]).hexdigest()
                        ):
                            raise ValueError(
                                "An existing source chunk differs; retain it for recovery."
                            )
                        # A failed provider cp may leave an exact prefix of our
                        # original immutable chunk. Only that proved partial is
                        # repaired with the same original bytes.
                if upload_required:
                    if before_mutation is not None and not delivery_started:
                        before_mutation("native_testing_copy_chunk")
                        delivery_started = True
                    for attempt in range(3):
                        try:
                            api.upload_native_testing_chunk(
                                host=host,
                                token=token,
                                server_id=server_id,
                                container_id=runner,
                                filename=f"lp-native-{capture.capture_id}-{part}-{index}",
                                content=content,
                            )
                            break
                        except NativeUploadRejected as error:
                            if not error.retryable or attempt == 2:
                                raise
                            time.sleep(attempt + 1)
                index += 1
    final_runner, resume, readback, _ = inspect(capture)
    if final_runner != runner or resume or readback != expected:
        raise ValueError("The testing artifact bytes failed actual receiver readback.")
    return capture


def remove_completed_native_capture_cache(capture: NativeTestingCopyCapture) -> None:
    """Dispose raw private bytes after restore proof, retaining the DB receipt."""
    root = _cache_directory(capture.generation_binding_sha256)
    directory = root / capture.capture_id
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError("The private capture cache has unexpected custody.")
    if directory.exists():
        shutil.rmtree(directory)
