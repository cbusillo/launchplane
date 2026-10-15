"""Read a product's exact Compose source and constrain its new testing recipient."""

import base64
import json
import posixpath
import re
from collections.abc import Callable
from urllib.parse import quote
from typing import Any

import yaml


def read_compose_source(
    *, repository: str, commit: str, path: str, read: Callable[[str], object]
) -> str:
    """Flatten local includes from one immutable tree; never execute product code."""
    visited: set[str] = set()
    prefix = "/repos/" + "/".join(quote(part, safe="") for part in repository.split("/"))

    def load(filename: str) -> dict[str, Any]:
        filename = posixpath.normpath(filename)
        if (
            filename.startswith(("/", "../"))
            or filename in visited
            or len(visited) >= 16
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", filename)
        ):
            raise ValueError("Compose source has an unsupported include.")
        visited.add(filename)
        response = read(f"{prefix}/contents/{quote(filename, safe='/')}?ref={commit}")
        if not isinstance(response, dict) or response.get("type") != "file":
            raise ValueError("Compose source is not a regular repository file.")
        raw = base64.b64decode(str(response.get("content", "")), validate=False)
        if len(raw) > 256 * 1024 or response.get("encoding") != "base64":
            raise ValueError("Compose source exceeds its bound.")
        document = yaml.safe_load(raw)
        if not isinstance(document, dict):
            raise ValueError("Compose source is unavailable.")
        includes = document.pop("include", [])
        document.pop("name", None)
        document.pop("version", None)
        document = {key: value for key, value in document.items() if not key.startswith("x-")}
        if not isinstance(includes, list):
            raise ValueError("Only local Compose includes are supported.")
        for include in includes:
            if not isinstance(include, str):
                raise ValueError("Only local Compose includes are supported.")
            included = load(posixpath.join(posixpath.dirname(filename), include))
            for key, values in included.items():
                if not isinstance(values, dict) or not isinstance(document.get(key, {}), dict):
                    raise ValueError("Compose include merge is ambiguous.")
                if set(values).intersection(document.get(key, {})):
                    raise ValueError("Compose includes cannot override one another.")
                document.setdefault(key, {}).update(values)
        return document

    return json.dumps(load(path), sort_keys=True, separators=(",", ":"))


_SUBSTITUTION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])(.*?))?\}")


def interpolate(value: Any, environment: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: interpolate(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, environment) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        key, operator, default = match.groups()
        present = key in environment and (
            bool(environment[key]) or not (operator or "").startswith(":")
        )
        if present:
            return environment[key]
        if operator in {"-", ":-"}:
            return default or ""
        if operator in {"?", ":?"}:
            raise ValueError("A required Compose input is unavailable.")
        return ""

    if "$" in _SUBSTITUTION.sub("", value):
        raise ValueError("Unsupported Compose interpolation.")
    return _SUBSTITUTION.sub(replace, value)


def qualify_compose(
    document: object, *, volumes: set[str], ports: set[int], credential_values: set[str]
) -> None:
    """The actual render can only write its two qualified volumes and private network."""
    if not isinstance(document, dict) or set(document) - {
        "name",
        "services",
        "volumes",
        "networks",
    }:
        raise ValueError("Unsupported Compose resource family.")
    services = document.get("services")
    declared = document.get("volumes", {})
    if not isinstance(services, dict) or not services or not isinstance(declared, dict):
        raise ValueError("Compose services and volumes must be explicit.")
    names = {
        key: value.get("name", "") for key, value in declared.items() if isinstance(value, dict)
    }
    if set(names.values()) != volumes or len(names) != len(volumes):
        raise ValueError("Actual Compose storage differs from the qualified allocation.")
    for value in declared.values():
        if not isinstance(value, dict) or set(value) - {"name", "external", "labels"}:
            raise ValueError("Compose storage driver or backing options are unsupported.")
    networks = document.get("networks", {})
    if not isinstance(networks, dict) or any(
        value and (not isinstance(value, dict) or set(value) - {"internal", "labels"})
        for value in networks.values()
    ):
        raise ValueError("Testing cannot join an external or explicitly named network.")
    used_volumes: set[str] = set()
    published: set[int] = set()
    for service in services.values():
        if not isinstance(service, dict) or set(service) - {
            "image",
            "environment",
            "ports",
            "healthcheck",
            "depends_on",
            "restart",
            "volumes",
            "command",
            "entrypoint",
            "working_dir",
            "user",
            "init",
            "stop_grace_period",
            "networks",
            "read_only",
            "tmpfs",
            "labels",
            "logging",
            "deploy",
            "pull_policy",
        }:
            raise ValueError("Testing Compose has an unsupported host capability.")
        if service.get("deploy") or service.get("logging"):
            raise ValueError("Testing cannot use external deploy or logging resources.")
        if not service.get("image") or service.get("labels"):
            raise ValueError("Testing requires image services without external routing labels.")
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                parts = mount.split(":")
                if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] not in {"ro", "rw"}):
                    raise ValueError("Unsupported testing mount.")
                source, destination = parts[:2]
            elif isinstance(mount, dict) and mount.get("type") == "volume":
                source, destination = str(mount.get("source", "")), str(mount.get("target", ""))
                if set(mount) - {"type", "source", "target", "read_only", "volume"} or mount.get(
                    "volume", {}
                ).get("subpath"):
                    raise ValueError("Unsupported testing volume mount.")
            else:
                raise ValueError("Testing cannot mount host paths or anonymous volumes.")
            if (
                source not in names
                or not isinstance(destination, str)
                or not destination.startswith("/")
            ):
                raise ValueError("Testing mount is outside its allocation.")
            used_volumes.add(names[source])
        for port in service.get("ports", []):
            if isinstance(port, (str, int)):
                parts = str(port).split(":")
                if len(parts) != 2 or not all(part.isdecimal() for part in parts):
                    raise ValueError("Testing ports must have explicit numeric host bindings.")
                host_port = int(parts[0])
            elif isinstance(port, dict) and port.get("protocol", "tcp") == "tcp":
                host_port = int(port.get("published", 0))
            else:
                raise ValueError("Unsupported testing port binding.")
            if host_port not in ports or host_port in published:
                raise ValueError("Testing ports differ from the independent lane settings.")
            published.add(host_port)
        environment = service.get("environment", {})
        if isinstance(environment, list):
            environment = dict(entry.split("=", 1) for entry in environment)
        if not isinstance(environment, dict):
            raise ValueError("Testing environment is unavailable.")
        for key, value in environment.items():
            if (
                re.search(r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|(?:^|_)KEY)$", key)
                and value not in credential_values
            ):
                raise ValueError("A carried credential lacks a safe managed lane binding.")
            if re.search(r"(?:_HOST|_URL)$", key) and value not in {
                *services,
                "localhost",
                "127.0.0.1",
                "0.0.0.0",
                "",
            }:
                raise ValueError("Testing has an unqualified external writable endpoint.")
    if used_volumes != volumes or published != ports:
        raise ValueError("Testing must consume every qualified volume and independent port.")
