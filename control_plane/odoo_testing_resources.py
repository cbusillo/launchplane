"""Provision new local Docker volumes without adopting or changing existing data."""

import base64
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256


TESTING_RESOURCES_INCOMPLETE_MARKER = "testing_resources_incomplete"


class OdooTestingResources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")

    @property
    def volumes(self) -> dict[str, str]:
        return {
            "ODOO_DATA_VOLUME": f"{self.namespace}-data",
            "ODOO_DB_VOLUME": f"{self.namespace}-db",
            "ODOO_LOG_VOLUME": f"{self.namespace}-logs",
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    def script(self, mode: Literal["prepare", "readback"], *, compose_project: str = "") -> str:
        if mode not in {"prepare", "readback"}:
            raise ValueError("Unsupported replacement resource mode.")
        spec = {
            **self.model_dump(mode="json"),
            "volumes": self.volumes,
            "mode": mode,
            "compose_project": compose_project or self.namespace,
        }
        encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
        return (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_TESTING_RESOURCES'\n"
            + ODOO_TESTING_RESOURCE_PROGRAM
            + "\nLP_TESTING_RESOURCES\n"
        )


class GenericWebTestingResources(OdooTestingResources):
    """New empty storage only; no application or production settings are installed."""

    resource_kind: Literal["generic-web"] = "generic-web"

    @property
    def volumes(self) -> dict[str, str]:
        return {
            "database": f"{self.namespace}-db",
            "config": f"{self.namespace}-config",
        }


class OdooRehearsalResources(BaseModel):
    """The same empty storage protocol, without a canonical lane identity."""

    model_config = ConfigDict(extra="forbid")
    product: str = Field(min_length=1)
    context: str = Field(pattern=r"^lp-rehearsal-[a-f0-9]{32}$")
    instance: Literal["rehearsal"] = "rehearsal"
    operation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^lp-rehearsal-[a-f0-9]{32}$")
    resource_kind: Literal["odoo-upgrade-rehearsal"] = "odoo-upgrade-rehearsal"

    @property
    def volumes(self) -> dict[str, str]:
        return {
            "ODOO_DATA_VOLUME": self.namespace + "-data",
            "ODOO_DB_VOLUME": self.namespace + "-db",
            "ODOO_LOG_VOLUME": self.namespace + "-logs",
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    def script(self, mode: Literal["prepare", "readback"]) -> str:
        spec = {
            **self.model_dump(mode="json"),
            "volumes": self.volumes,
            "mode": mode,
            "compose_project": self.namespace,
        }
        encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
        return (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_TESTING_RESOURCES'\n"
            + ODOO_TESTING_RESOURCE_PROGRAM
            + "\nLP_TESTING_RESOURCES\n"
        )


def resource_readback_passes(evidence: dict[str, str], resources: OdooTestingResources) -> bool:
    return (
        evidence.get("testing_resources_ok") == "true"
        and evidence.get("testing_resources_binding_sha256") == resources.binding_sha256
    )


def validate_resource_namespace(namespace: str) -> None:
    if not re.fullmatch(r"lp-testing-[a-f0-9]{32}", namespace):
        raise ValueError("Replacement requires its service-allocated resource namespace.")


# The program runs on the selected Docker host, outside every application
# container. It accepts no paths, credentials, driver options or existing names.
# Failed or interrupted preparation retains its volumes for same-spec readback;
# it never deletes a volume to make a retry pass.
ODOO_TESTING_RESOURCE_PROGRAM = (
    f"INCOMPLETE_MARKER = {TESTING_RESOURCES_INCOMPLETE_MARKER!r}\n"
    + r"""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def docker(*arguments):
    result = subprocess.run(
        ["docker", *arguments], check=False, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("Docker resource inspection or preparation failed")
    return result.stdout


def inspect_volume(name):
    found = json.loads(docker("volume", "inspect", name))
    if not isinstance(found, list) or len(found) != 1:
        raise RuntimeError("Volume inspection is incomplete")
    return found[0]


def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    namespace = spec["namespace"]
    rehearsal = spec.get("resource_kind") == "odoo-upgrade-rehearsal"
    if (
        spec["instance"] != ("rehearsal" if rehearsal else "testing")
        or spec["mode"] not in {"prepare", "readback"}
        or not re.fullmatch(("lp-rehearsal-" if rehearsal else "lp-testing-") + r"[a-f0-9]{32}", namespace)
    ):
        raise RuntimeError("Invalid testing resource scope")
    volumes = {
        "ODOO_DATA_VOLUME": namespace + "-data",
        "ODOO_DB_VOLUME": namespace + "-db",
        "ODOO_LOG_VOLUME": namespace + "-logs",
    }
    if spec.get("resource_kind") == "generic-web":
        volumes = {
            "database": namespace + "-db",
            "config": namespace + "-config",
        }
    elif "resource_kind" in spec and not rehearsal:
        raise RuntimeError("Unsupported testing resource kind")
    if spec["volumes"] != volumes:
        raise RuntimeError("Invalid testing resource names")
    project = spec.get("compose_project", namespace)
    if not re.fullmatch(re.escape(namespace) + r"(?:-[a-z0-9]{6})?", project):
        raise RuntimeError("Invalid testing compose project")
    binding = {key: value for key, value in spec.items() if key not in {"mode", "volumes", "compose_project"}}
    digest = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    paths = []
    identities = set()
    missing = False
    for role, name in volumes.items():
        labels = {
            "io.launchplane.testing-resources": digest,
            "io.launchplane.testing-resource-role": role,
        }
        if name not in existing:
            if spec["mode"] != "prepare":
                missing = True
                continue
            arguments = ["volume", "create", "--driver", "local"]
            for key, value in labels.items():
                arguments.extend(["--label", key + "=" + value])
            arguments.append(name)
            docker(*arguments)
        volume = inspect_volume(name)
        if (
            volume.get("Name") != name
            or volume.get("Driver") != "local"
            or volume.get("Options") not in (None, {})
            or not isinstance(volume.get("Labels"), dict)
            or any(volume["Labels"].get(key) != value for key, value in labels.items())
        ):
            raise RuntimeError("Existing volume cannot be adopted as a new testing resource")
        path = Path(volume["Mountpoint"])
        if not path.is_absolute():
            raise RuntimeError("Unsupported volume backing directory")
        path = path.resolve(strict=True)
        if (
            path.name != "_data"
            or path.parent.name != name
            or not path.is_dir()
        ):
            raise RuntimeError("Unsupported volume backing directory")
        identity = (path.stat().st_dev, path.stat().st_ino)
        if identity in identities or any(path.iterdir()):
            raise RuntimeError("Replacement storage is aliased or contains data")
        identities.add(identity)
        paths.append(path)
    container_ids = docker("ps", "-aq").splitlines()
    # A stopped container can still own needed data or restart later. Inspect all
    # containers, rather than treating a lack of running containers as unused.
    for identifier in container_ids:
        inspected = json.loads(docker("inspect", identifier))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise RuntimeError("Container mount inspection is incomplete")
        configuration = inspected[0].get("Config")
        labels = configuration.get("Labels") if isinstance(configuration, dict) else None
        if isinstance(labels, dict) and labels.get("com.docker.compose.project") == project:
            raise RuntimeError("The new compose already has a container")
        mounts = inspected[0].get("Mounts")
        if not isinstance(mounts, list):
            raise RuntimeError("Container mount inventory is unavailable")
        for mount in mounts:
            if mount.get("Type") == "tmpfs":
                continue
            if mount.get("Name") in volumes.values():
                raise RuntimeError("Replacement storage is already used by a container")
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                raise RuntimeError("Container backing storage is unavailable")
            source = Path(source).resolve()
            if mount.get("Type") == "bind" and mount.get("RW") is False and not any(
                source == path or path in source.parents for path in paths
            ):
                # Only read-only parents are administrative host inspection.
                # Exact paths or children are consumers of this resource.
                continue
            if any(source == path or source in path.parents or path in source.parents for path in paths):
                raise RuntimeError("Replacement backing storage overlaps an existing mount")
    print("testing_resources_binding_sha256=" + digest)
    if missing:
        print(INCOMPLETE_MARKER + "=true")
        print("testing_resources_ok=false")
        sys.exit(1)
    print("testing_resources_ok=true")


try:
    main()
except Exception:
    # Docker output and paths can contain private state; never forward exceptions.
    print("testing_resources_ok=false")
    sys.exit(1)
"""
)
