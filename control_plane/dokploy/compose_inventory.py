import base64
from dataclasses import dataclass
import hashlib
import inspect
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
from typing import Any

import yaml

from control_plane.dokploy import native_copy_stream
from control_plane.dokploy import api
from control_plane.dokploy.target_source_setup import (
    COMPOSE_SOURCE_FIELDS,
    require_empty_compose_source,
)
from control_plane.testing_activation_compose import (
    _SUBSTITUTION,
    compose_interpolation_keys,
    interpolate,
)


SOURCE_FILE_BYTES = 64 * 1024
SOURCE_TOTAL_BYTES = 512 * 1024
SOURCE_FILE_COUNT = 16
SOURCE_ENV_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CachedComposeFile:
    document: dict[str, Any]
    sha256: str
    directory_env: bool


@dataclass(frozen=True)
class CachedComposeSource:
    project_absent: bool
    files: dict[str, CachedComposeFile]
    absent_inputs: frozenset[str]


def standard_compose_environment(payload: dict[str, Any]) -> bool:
    import posixpath

    if (
        payload.get("composeType") != "docker-compose"
        or payload.get("createEnvFile") is not True
        or payload.get("randomize") is not False
        or "command" not in payload
    ):
        return False
    command = payload["command"]
    if command in (None, ""):
        return True
    path = payload.get("composePath")
    if (
        not isinstance(command, str)
        or any(character in command for character in "'\";&|`$(){}<>\n\\")
        or payload.get("sourceType") != "raw"
        or not isinstance(path, str)
        or posixpath.dirname(posixpath.normpath(path))
        or re.search(r"(?<![A-Za-z0-9_])COMPOSE_", str(payload.get("env") or "")) is not None
    ):
        return False
    arguments = command.split()
    return (
        len(arguments) in (8, 9)
        and arguments[:4] == ["compose", "-p", payload.get("appName"), "-f"]
        and arguments[4] in ("docker-compose.yml", "./docker-compose.yml")
        and arguments[5] == "up"
        and set(arguments[6:])
        == (
            {"-d", "--remove-orphans"}
            if len(arguments) == 8
            else {"-d", "--remove-orphans", "--build"}
        )
    )


def source_relative_path(value: str, parent: str = "") -> str:
    path = posixpath.normpath(posixpath.join(parent, value))
    if (
        value.startswith("/")
        or path.startswith(("/", "../"))
        or path in (".", "..")
        or len(path) > 512
        or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None
        or PurePosixPath(path).suffix not in {".yml", ".yaml", ".json"}
    ):
        raise ValueError("Unqualified cached source path.")
    return path


def require_named_storage(document: dict[str, Any]) -> None:
    declared = set(document.get("volumes", {}))
    if document.get("configs") or document.get("secrets"):
        raise ValueError("File-backed configuration requires separate retained custody.")
    for service in document["services"].values():
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                parts = mount.split(":")
                if len(parts) not in {2, 3} or parts[0] not in declared:
                    raise ValueError("Bind or anonymous storage is unsupported.")
            elif (
                not isinstance(mount, dict)
                or mount.get("type", "volume") != "volume"
                or mount.get("source") not in declared
            ):
                raise ValueError("Only exact named storage can be qualified.")


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("A canonical directory is required.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    for component in path.parts[1:]:
        try:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
        finally:
            os.close(descriptor)
        descriptor = child
    return descriptor


def _read_regular(path: Path, bound: int) -> bytes:
    directory = _open_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > bound:
            raise ValueError("A bounded regular source is required.")
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            opened = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("The source changed while opening.")
            content = bytearray()
            while chunk := os.read(descriptor, min(65536, bound + 1 - len(content))):
                content.extend(chunk)
                if len(content) > bound:
                    raise ValueError("The source exceeds its bound.")
            after = os.fstat(descriptor)
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            for observed in (before, current):
                if (
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_size,
                    observed.st_mtime_ns,
                ) != (after.st_dev, after.st_ino, len(content), after.st_mtime_ns):
                    raise ValueError("The source changed while reading.")
            return bytes(content)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def read_cached_files(base: Path, request: dict[str, Any]) -> dict[str, Any]:
    app = request["app_name"]
    files = request["files"]
    inputs = request.get("input_names", [])
    if (
        not isinstance(app, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", app) is None
        or not isinstance(files, list)
        or len(files) > SOURCE_FILE_COUNT
        or len(files) != len(set(files))
        or not isinstance(inputs, list)
        or len(inputs) > SOURCE_FILE_COUNT
        or any(
            not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None
            for key in inputs
        )
    ):
        raise ValueError("Unqualified cached source request.")
    directory = _open_directory(base)
    try:
        try:
            project = os.open(app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return {"project_absent": True, "files": {}, "absent_inputs": []}
        os.close(project)
    finally:
        os.close(directory)
    code = base / app / "code"
    rows = {}
    size = 0
    for relative in files:
        if not isinstance(relative, str) or source_relative_path(relative) != relative:
            raise ValueError("Unqualified cached source path.")
        content = _read_regular(code / relative, SOURCE_FILE_BYTES)
        size += len(content)
        if size > SOURCE_TOTAL_BYTES:
            raise ValueError("Cached sources exceed their bound.")
        parent = _open_directory((code / relative).parent)
        try:
            try:
                os.stat(".env", dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                environment = False
            else:
                environment = True
        finally:
            os.close(parent)
        rows[relative] = {
            "content": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "directory_env": environment,
        }
    absent = []
    if inputs:
        environment_path = request.get("env_file")
        if (
            not isinstance(environment_path, str)
            or PurePosixPath(environment_path).is_absolute()
            or ".." in PurePosixPath(environment_path).parts
            or PurePosixPath(environment_path).name != ".env"
        ):
            raise ValueError("Unqualified generated environment path.")
        environment_bytes = _read_regular(code / environment_path, SOURCE_ENV_BYTES)
        absent = [key for key in inputs if key.encode() not in environment_bytes]
    if any(
        hashlib.sha256(_read_regular(code / relative, SOURCE_FILE_BYTES)).hexdigest()
        != row["sha256"]
        for relative, row in rows.items()
    ):
        raise ValueError("Cached sources changed during observation.")
    return {"project_absent": False, "files": rows, "absent_inputs": absent}


CACHED_COMPOSE_PROGRAM = (
    "import base64,hashlib,json,os,posixpath,re,stat,sys\n"
    "from pathlib import Path,PurePosixPath\nfrom typing import Any\n"
    + "\n".join(
        f"{key}={value}"
        for key, value in (
            ("SOURCE_FILE_BYTES", SOURCE_FILE_BYTES),
            ("SOURCE_TOTAL_BYTES", SOURCE_TOTAL_BYTES),
            ("SOURCE_FILE_COUNT", SOURCE_FILE_COUNT),
            ("SOURCE_ENV_BYTES", SOURCE_ENV_BYTES),
        )
    )
    + "\n"
    + inspect.getsource(source_relative_path)
    + inspect.getsource(_open_directory)
    + inspect.getsource(_read_regular)
    + inspect.getsource(read_cached_files)
    + "\ntry:\n"
    "    result=read_cached_files(Path('/etc/dokploy/compose'),json.loads(base64.b64decode(sys.argv[1],validate=True)))\n"
    "except (OSError,ValueError,TypeError,KeyError):\n"
    "    result={'unavailable':True}\n"
    "print(sys.argv[2]+json.dumps({'type':'complete','result':result}))\n"
)


def read_cached_compose_source(
    *, host: str, token: str, server_id: str, request: dict[str, Any]
) -> CachedComposeSource:
    frames: list[dict[str, object]] = []
    native_copy_stream.require_registered_remote_server(server_id)
    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=server_id,
        program=CACHED_COMPOSE_PROGRAM,
        payload=request,
        receive=frames.append,
        timeout_seconds=20,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Cached source observation is incomplete.")
    result = frames[0].get("result")
    if not isinstance(result, dict) or result.get("unavailable"):
        raise ValueError("Cached source observation is unavailable.")
    absent = result.get("project_absent")
    rows = result.get("files")
    inputs = result.get("absent_inputs")
    if (
        type(absent) is not bool
        or not isinstance(rows, dict)
        or set(rows) != (set() if absent else set(request["files"]))
        or not isinstance(inputs, list)
        or any(not isinstance(key, str) for key in inputs)
        or not set(inputs) <= set(request.get("input_names", []))
    ):
        raise ValueError("Cached source observation differs from its request.")
    files = {}
    size = 0
    for relative, row in rows.items():
        content = base64.b64decode(row["content"], validate=True)
        size += len(content)
        if (
            len(content) > SOURCE_FILE_BYTES
            or size > SOURCE_TOTAL_BYTES
            or hashlib.sha256(content).hexdigest() != row["sha256"]
            or type(row["directory_env"]) is not bool
        ):
            raise ValueError("Cached source bytes are unqualified.")
        document = yaml.safe_load(content)
        if not isinstance(document, dict):
            raise ValueError("Cached Compose document is unavailable.")
        files[relative] = CachedComposeFile(document, row["sha256"], row["directory_env"])
    return CachedComposeSource(absent, files, frozenset(inputs))


def _empty_project_documents(
    *, host: str, token: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    if (
        not set(COMPOSE_SOURCE_FIELDS) <= payload.keys()
        or "sourceType" not in payload
        or "env" not in payload
        or not isinstance(payload["env"], str | type(None))
        or payload.get("mounts") != []
        or payload.get("command") not in (None, "")
        or "command" not in payload
        or payload.get("backups") != []
        or payload.get("domains") != []
        or not isinstance(payload.get("deployments"), list)
        or any(
            not isinstance(row, dict) or row.get("status") not in {"done", "error", "cancelled"}
            for row in payload["deployments"]
        )
    ):
        raise ValueError("The empty source is not independently qualified.")
    require_empty_compose_source(payload, payload["composeId"])
    if api.list_dokploy_schedules(
        host=host, token=token, target_id=payload["composeId"], schedule_type="compose"
    ):
        raise ValueError("The empty source has scheduled work.")
    observed = read_cached_compose_source(
        host=host,
        token=token,
        server_id=payload["serverId"],
        request={"app_name": payload["appName"], "files": []},
    )
    if not observed.project_absent:
        raise ValueError("The unconfigured source retains cached project custody.")
    return ()


def _default_volume_inputs(document: dict[str, Any], raw_env: str) -> set[str]:
    keys: set[str] = set()
    for definition in document.get("volumes", {}).values():
        name = (definition or {}).get("name")
        if not isinstance(name, str) or "$" not in name:
            continue
        selected = compose_interpolation_keys(name)
        if any(match[2] not in {"-", ":-"} for match in _SUBSTITUTION.finditer(name)) or any(
            key in {"PATH", "HOME", "APP_NAME"}
            or key.startswith(("COMPOSE_", "DOCKER_"))
            or key in raw_env
            for key in selected
        ):
            raise ValueError("Cached interpolation lacks qualified default scope.")
        keys.update(selected)
    return keys


def _included_documents(
    *, host: str, token: str, payload: dict[str, Any], document: dict[str, Any], exact: bool
) -> tuple[dict[str, Any], ...]:
    if (
        payload.get("sourceType") != "git"
        or not standard_compose_environment(payload)
        or payload.get("isolatedDeployment") is not False
        or payload.get("mounts") != []
        or payload.get("domains") != []
        or document.get("services")
        or document.get("volumes")
        or document.get("configs")
        or document.get("secrets")
    ):
        raise ValueError("Cached include scope is unsupported.")
    includes = document.get("include")
    if (
        not isinstance(includes, list)
        or not includes
        or any(not isinstance(path, str) for path in includes)
    ):
        raise ValueError("Only local include wrappers are qualified.")
    root = source_relative_path(payload["composePath"])
    paths = [root, *(source_relative_path(path, posixpath.dirname(root)) for path in includes)]
    request = {"app_name": payload["appName"], "files": paths}
    arguments = {"host": host, "token": token, "server_id": payload["serverId"]}
    observed = read_cached_compose_source(**arguments, request=request)
    if observed.project_absent or observed.files[root].document.get("include") != includes:
        raise ValueError("The cached source differs from the converted include.")
    from control_plane.production_recipient import readable_provider_compose

    documents = []
    keys: set[str] = set()
    for path in paths[1:]:
        file = observed.files[path]
        child = readable_provider_compose(file.document)
        if (
            child.get("include")
            or file.directory_env
            and posixpath.dirname(path) != posixpath.dirname(root)
            or any(
                service.get("env_file")
                or isinstance(service.get("extends"), dict)
                and service["extends"].get("file")
                for service in child["services"].values()
            )
        ):
            raise ValueError("The included source has unsupported file scopes.")
        require_named_storage(child)
        keys.update(_default_volume_inputs(child, str(payload.get("env") or "")))
        documents.append(child)
    if keys:
        if not exact or not isinstance(payload.get("env"), str):
            raise ValueError("Default absence requires complete exact environment evidence.")
        proof = read_cached_compose_source(
            **arguments,
            request={
                **request,
                "input_names": sorted(keys),
                "env_file": posixpath.join(posixpath.dirname(root), ".env"),
            },
        )
        if (
            proof.project_absent
            or proof.absent_inputs != keys
            or {path: file.sha256 for path, file in proof.files.items()}
            != {path: file.sha256 for path, file in observed.files.items()}
        ):
            raise ValueError("Cached default absence changed or remains unqualified.")
        documents = [
            {
                **child,
                "volumes": {
                    alias: {**definition, "name": interpolate(definition["name"], {})}
                    if isinstance(definition, dict) and isinstance(definition.get("name"), str)
                    else definition
                    for alias, definition in child.get("volumes", {}).items()
                },
            }
            for child in documents
        ]
    return tuple(documents)


def read_volume_documents(
    *, host: str, token: str, payload: dict[str, Any], raw: str, server_id: str, exact: bool
) -> tuple[dict[str, Any], ...]:
    document = yaml.safe_load(raw)
    from control_plane.production_recipient import readable_provider_compose

    if isinstance(document, dict) and not document.get("include"):
        return (readable_provider_compose(document),)
    if not server_id or payload.get("serverId") != server_id:
        raise ValueError("Cached source placement is unqualified.")
    if document is None:
        return _empty_project_documents(host=host, token=token, payload=payload)
    if not isinstance(document, dict):
        raise ValueError("Converted source is unreadable.")
    return _included_documents(
        host=host, token=token, payload=payload, document=document, exact=exact
    )
