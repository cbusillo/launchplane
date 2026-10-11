"""Atomic single-file publication, verified against Traefik's applied API."""

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from pydantic import Field, model_validator
import yaml

from control_plane.contracts.prepared_public_site import FrozenRecord


class TraefikRouteTarget(FrozenRecord):
    route_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    entrypoints: tuple[str, ...] = Field(min_length=1)
    route_file: Path
    api_url: str
    tls: bool = True
    certificate_resolver: str = ""
    middlewares: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_file(self) -> "TraefikRouteTarget":
        if not self.route_file.is_absolute() or self.route_file.suffix not in {".yaml", ".yml"}:
            raise ValueError("file-provider route requires an explicit absolute YAML path")
        if not self.route_file.parent.is_dir():
            raise ValueError("configured file-provider directory is unavailable")
        if self.route_file.is_symlink():
            raise ValueError("route file cannot be a symlink")
        return self


def route_payload(target: TraefikRouteTarget, backend: str) -> dict[str, Any]:
    parts = urlsplit(backend)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("backend requires an explicit HTTP endpoint")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("backend must be an origin")
    router: dict[str, Any] = {
        "rule": f"Host(`{target.host}`)",
        "service": target.route_id,
        "entryPoints": target.entrypoints,
        "middlewares": target.middlewares,
    }
    if target.tls:
        router["tls"] = (
            {"certResolver": target.certificate_resolver} if target.certificate_resolver else {}
        )
    return {
        "http": {
            "routers": {target.route_id: router},
            "services": {
                target.route_id: {
                    "loadBalancer": {
                        "passHostHeader": True,
                        "servers": [{"url": backend}],
                    }
                }
            },
        }
    }


class TraefikFileRoute:
    """The caller holds the exclusive lane lock throughout publication/readback.

    Mount the *directory*, not this single file, into the file-provider watcher.
    A rename alone is never evidence that Traefik applied a route.
    """

    def __init__(self, target: TraefikRouteTarget, *, apply_timeout_seconds: float = 30) -> None:
        if apply_timeout_seconds <= 0:
            raise ValueError("route application requires a positive deadline")
        self.target = target
        self.apply_timeout_seconds = apply_timeout_seconds

    def _api(self, suffix: str) -> Any:
        with build_opener(ProxyHandler({})).open(
            self.target.api_url.rstrip("/") + suffix, timeout=10
        ) as response:
            return json.loads(response.read(2_000_000))

    def observed_target(self) -> str:
        raw = self._api("/api/rawdata")
        routers = [{**router, "name": name} for name, router in raw.get("routers", {}).items()]
        host_rule = f"Host(`{self.target.host}`)"
        # Traefik v3 rawdata is unpaginated; it also includes disabled and
        # provider-qualified routers which must not silently become authority.
        matches = []
        for router in routers:
            rule = router.get("rule", "")
            if not set(router.get("entryPoints", [])) & set(self.target.entrypoints):
                continue
            host_arguments = re.findall(r"Host\(([^)]*)\)", rule)
            hosts = [
                host
                for arguments in host_arguments
                for host in re.findall(r'[`"]([^`"]+)[`"]', arguments)
            ]
            regexes = re.findall(r'HostRegexp\(\s*[`"]([^`"]+)[`"]\s*\)', rule)
            try:
                regex_matches = any(
                    re.search(pattern, self.target.host, re.IGNORECASE) for pattern in regexes
                )
            except re.error as error:
                raise ValueError("host expression cannot establish lane exclusivity") from error
            if "HostRegexp(" in rule and not regexes:
                raise ValueError("host expression cannot establish lane exclusivity")
            if (
                self.target.host.casefold() in {host.casefold() for host in hosts}
                or regex_matches
                or not (hosts or regexes)
            ):
                matches.append(router)
        if len(matches) != 1:
            raise ValueError("missing or duplicate authoritative public router")
        router = matches[0]
        if (
            router.get("name") != self.target.route_id + "@file"
            or router.get("rule") != host_rule
            or router.get("service") not in {self.target.route_id, self.target.route_id + "@file"}
            or router.get("status") != "enabled"
            or set(router.get("entryPoints", [])) != set(self.target.entrypoints)
            or tuple(router.get("middlewares", [])) != self.target.middlewares
            or ("tls" in router) != self.target.tls
            or (
                self.target.tls
                and router.get("tls", {}).get("certResolver", "")
                != self.target.certificate_resolver
            )
        ):
            raise ValueError("applied router does not match lane authority")
        service = self._api("/api/http/services/" + self.target.route_id + "@file")
        if service.get("status") != "enabled":
            raise ValueError("applied service is unavailable")
        servers = service.get("loadBalancer", {}).get("servers", [])
        if len(servers) != 1 or not servers[0].get("url"):
            raise ValueError("applied route must have exactly one backend")
        return str(servers[0]["url"])

    def publish(self, backend: str, *, expected: tuple[str, ...]) -> str:
        observed = self.observed_target()
        if observed == backend:
            return observed
        if observed not in expected:
            raise ValueError("route authority changed; refusing a stale traffic switch")
        # A watched file belongs to this route alone. Never replace a shared
        # dynamic configuration and silently retire its other routers/services.
        owned = yaml.safe_load(self.target.route_file.read_text())
        intended = json.loads(json.dumps(route_payload(self.target, backend)))
        previous = json.loads(json.dumps(route_payload(self.target, observed)))
        if owned not in (previous, intended):
            raise ValueError("route file is not the exact single-route owned configuration")
        if owned == intended:
            return self._wait_applied(backend, expected)
        payload = json.dumps(intended, sort_keys=True)
        temporary: str | None = None
        try:
            # No supported file-provider extension while the file is incomplete.
            with NamedTemporaryFile(
                mode="w",
                prefix=".cutover-",
                suffix=".pending",
                dir=self.target.route_file.parent,
                delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.target.route_file)
            temporary = None
            descriptor = os.open(self.target.route_file.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return self._wait_applied(backend, expected)

    def _wait_applied(self, backend: str, expected: tuple[str, ...]) -> str:
        # Asynchronous provider application may not be observable yet. The
        # controller records switching and reconciles instead of claiming pass.
        deadline = time.monotonic() + self.apply_timeout_seconds
        while True:
            observed = self.observed_target()
            if observed == backend:
                return backend
            if observed not in expected:
                raise ValueError("foreign route appeared during publication readback")
            if time.monotonic() >= deadline:
                raise TimeoutError("published route has no verified applied outcome")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def evidence_id(self) -> str:
        return hashlib.sha256(
            (self.observed_target() + datetime.now(UTC).isoformat()).encode()
        ).hexdigest()
