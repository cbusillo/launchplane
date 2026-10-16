"""Persist typed startup intent and recreate only fenced Compose consumers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import inspect
import json
from pathlib import PurePosixPath
import re
from typing import Any

from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.dokploy import api
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.dokploy.slot_website_settings import StartupSettingsReadback
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY


def merge_startup_environment(
    raw: str, settings: dict[str, Any], payload_key: str, indexing_key: str
) -> str:
    """Change one payload; retain unrelated settings and raw env lines intact."""
    lines = raw.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*(?:export\s+)?" + re.escape(payload_key) + r"\s*=", line)
    ]
    if len(matches) > 1:
        raise ValueError("startup payload is duplicated")
    payload: dict[str, Any] = {}
    if matches:
        value = lines[matches[0]].split("=", 1)[1].strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("startup payload must be a single portable value")
            value = value[1:-1]
        payload = json.loads(base64.b64decode(value, validate=True)) if value else {}
    if not isinstance(payload, dict) or not isinstance(payload.get("config_parameters", []), list):
        raise ValueError("startup override payload is invalid")
    parameters = payload.get("config_parameters", [])
    if any(
        not isinstance(item, dict) or not isinstance(item.get("key"), str) for item in parameters
    ):
        raise ValueError("startup parameters are invalid")
    payload["instance"] = "prod"
    payload["website_settings"] = settings
    payload["config_parameters"] = [item for item in parameters if item["key"] != indexing_key] + [
        {
            "key": indexing_key,
            "value": {
                "source": "literal",
                "value": "1" if settings["public_indexing_enabled"] else "0",
            },
        }
    ]
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    replacement = payload_key + "=" + encoded + "\n"
    if matches:
        lines[matches[0]] = replacement
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(replacement)
    return "".join(lines)


def startup_file_program() -> str:
    # Use the same merger for provider records and the host's durable env file.
    # The host needs only its existing Python and Compose, not this package.
    return (
        "from __future__ import annotations\nimport base64,json,re,os,stat,tempfile,sys\nfrom pathlib import Path\n"
        + inspect.getsource(merge_startup_environment)
        + """
request=json.load(sys.stdin)
path=Path(request['path'])
if not path.is_absolute() or path.is_symlink() or path.resolve()!=path.absolute():
    raise ValueError('startup env requires an existing regular authoritative file')
metadata=path.stat()
if not stat.S_ISREG(metadata.st_mode):
    raise ValueError('startup env is not a regular file')
raw=path.read_text()
desired=merge_startup_environment(raw,request['settings'],request['payload_key'],request['indexing_key'])
if request.get('apply'):
    descriptor,temporary=tempfile.mkstemp(dir=path.parent,prefix='.launchplane-startup-')
    try:
        with os.fdopen(descriptor,'w') as handle:
            os.fchmod(handle.fileno(),stat.S_IMODE(metadata.st_mode))
            os.fchown(handle.fileno(),metadata.st_uid,metadata.st_gid)
            handle.write(desired);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    if path.read_text()!=desired: raise ValueError('startup env readback differs')
print(json.dumps({'verified':True}))
"""
    )


@dataclass(frozen=True)
class _Project:
    slot: OdooSlot
    argv: tuple[str, ...]
    env_file: str
    services: tuple[tuple[SlotWriterContainer, str], ...]


class DokploySlotStartup:
    """Bound to the same writer object used by native maintenance and handoff.

    Construction has no effects. Preflight only reads provider/Compose evidence.
    Configure runs inside the release guard after every writer has drained.
    It never deploys a target or starts a container, dependency, or registry.
    """

    def __init__(
        self, *, host: str, token: str, writers: ContainerSlotWriters, execute: ContainerCommand
    ):
        self.host, self.token, self.writers, self.execute = host, token, writers, execute
        self._pending_projects: dict[str, _Project] = {}

    def _refresh_project(self, project: _Project) -> None:
        # A lost up/ps reply is uncertainty, not evidence that the old IDs still
        # exist. Keep the observed Compose coordinates until readback succeeds.
        for writer, service in project.services:
            containers = self.execute(
                project.argv + ("ps", "--all", "--quiet", service), None
            ).split()
            if len(containers) != 1:
                raise ValueError("recreated writer inventory is not unique")
            self.writers.targets = tuple(
                replace(target, container=containers[0])
                if (target.slot_id, target.writer) == (writer.slot_id, writer.writer)
                else target
                for target in self.writers.targets
            )
        self._pending_projects.pop(project.slot.slot_id, None)

    def _projects(self, plan: OdooCutoverPlan, *, fenced: bool) -> tuple[_Project, ...]:
        if self.writers.plan != plan:
            raise ValueError("startup writer inventory belongs to another release")
        for project in tuple(self._pending_projects.values()):
            self._refresh_project(project)
        projects = []
        for slot in (plan.old, plan.new):
            target = api.fetch_dokploy_target_payload(
                host=self.host, token=self.token, target_type="compose", target_id=slot.compose_id
            )
            app = str(target.get("appName") or "")
            if not app or target.get("domains"):
                raise ValueError("startup requires the admitted private Compose projects")
            services = []
            project_argv: tuple[str, ...] | None = None
            env_file = ""
            for writer in self.writers.targets:
                if writer.slot_id != slot.slot_id:
                    continue
                observed = json.loads(self.execute(("docker", "inspect", writer.container), None))
                if not isinstance(observed, list) or len(observed) != 1:
                    raise ValueError("startup container inventory is ambiguous")
                config, state = observed[0]["Config"], observed[0]["State"]
                if fenced and (
                    config["Image"] != slot.image
                    or state.get("Running")
                    or state.get("Restarting")
                    or self.writers.active_jobs(writer) != 0
                ):
                    raise ValueError("startup requires exact immutable, stopped writers")
                labels = config.get("Labels", {})
                directory = str(labels.get("com.docker.compose.project.working_dir") or "")
                files = str(labels.get("com.docker.compose.project.config_files") or "").split(",")
                service = str(labels.get("com.docker.compose.service") or "")
                if (
                    labels.get("com.docker.compose.project") != app
                    or not service
                    or not PurePosixPath(directory).is_absolute()
                    or any(not PurePosixPath(path).is_absolute() for path in files)
                ):
                    raise ValueError(
                        "startup requires observed Compose project/file/service authority"
                    )
                argv: tuple[str, ...] = (
                    "docker",
                    "compose",
                    "--project-name",
                    app,
                    "--project-directory",
                    directory,
                )
                for path in files:
                    argv += ("--file", path)
                if project_argv is not None and project_argv != argv:
                    raise ValueError("writer Compose authorities disagree")
                project_argv = argv
                env_file = str(PurePosixPath(directory) / ".env")
                services.append((writer, service))
            if project_argv is None or len(services) != len(plan.writers):
                raise ValueError("startup requires every writer in both projects")
            # No interpolation/resolution: preflight must prove the env-file input,
            # rather than accept a container's stale effective settings.
            raw = json.loads(
                self.execute(
                    project_argv
                    + ("config", "--format", "json", "--no-env-resolution", "--no-interpolate"),
                    None,
                )
            )
            for _, service in services:
                definition = raw["services"][service]
                # The containers we are about to replace are no proof of what
                # Compose would create today. Refuse mutable/interpolated images
                # and later env files before touching either retained slot.
                if definition.get("image") != slot.image:
                    raise ValueError("Compose writer image differs from the immutable release")
                paths = definition.get("env_file", [])
                if (
                    not isinstance(paths, list)
                    or not paths
                    or str(paths[-1].get("path", "") if isinstance(paths[-1], dict) else paths[-1])
                    not in {".env", env_file}
                    or ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY in definition.get("environment", {})
                ):
                    raise ValueError(
                        "writer startup must consume the durable env-file payload without an override"
                    )
            projects.append(_Project(slot, project_argv, env_file, tuple(services)))
        return tuple(projects)

    def _file(self, project: _Project, intent: OdooWebsiteSettingsIntent, *, apply: bool) -> None:
        response = json.loads(
            self.execute(
                ("python3", "-c", startup_file_program()),
                json.dumps(
                    {
                        "path": project.env_file,
                        "settings": intent.settings.model_dump(mode="json"),
                        "payload_key": ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
                        "indexing_key": WEBSITE_INDEXING_PARAMETER,
                        "apply": apply,
                    }
                ),
            )
        )
        if response != {"verified": True}:
            raise ValueError("durable startup file readback is unverified")

    def preflight(self, plan: OdooCutoverPlan) -> None:
        projects = self._projects(plan, fenced=False)
        for intent in (plan.website_settings, plan.rollback_website_settings):
            if intent is None:
                continue
            for project in projects:
                self._file(project, intent, apply=False)
                target = api.fetch_dokploy_target_payload(
                    host=self.host,
                    token=self.token,
                    target_type="compose",
                    target_id=project.slot.compose_id,
                )
                merge_startup_environment(
                    str(target.get("env") or ""),
                    intent.settings.model_dump(mode="json"),
                    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
                    WEBSITE_INDEXING_PARAMETER,
                )

    def __call__(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> StartupSettingsReadback:
        if slot not in (plan.old, plan.new) or intent != (
            plan.website_settings if slot == plan.new else plan.rollback_website_settings
        ):
            raise ValueError("startup snapshot differs from the decision-bound slot")
        projects = self._projects(plan, fenced=True)
        # Read/validate both paths before the first write, including rollback.
        self.preflight(plan)
        for project in projects:
            current = api.fetch_dokploy_target_payload(
                host=self.host,
                token=self.token,
                target_type="compose",
                target_id=project.slot.compose_id,
            )
            desired = merge_startup_environment(
                str(current.get("env") or ""),
                intent.settings.model_dump(mode="json"),
                ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
                WEBSITE_INDEXING_PARAMETER,
            )
            api.update_dokploy_target_env(
                host=self.host,
                token=self.token,
                target_type="compose",
                target_id=project.slot.compose_id,
                target_payload=current,
                env_text=desired,
            )
            readback = api.fetch_dokploy_target_payload(
                host=self.host,
                token=self.token,
                target_type="compose",
                target_id=project.slot.compose_id,
            )
            if str(readback.get("env") or "") != desired:
                raise ValueError("provider startup payload readback differs")
            self._file(project, intent, apply=True)
            services = tuple(service for _, service in project.services)
            self._pending_projects[project.slot.slot_id] = project
            try:
                self.execute(
                    project.argv
                    + (
                        "up",
                        "--no-start",
                        "--no-deps",
                        "--force-recreate",
                        "--no-build",
                        "--pull",
                        "never",
                        *services,
                    ),
                    None,
                )
            finally:
                self._refresh_project(project)
        refreshed = self.writers.targets
        result = StartupSettingsReadback(intent.settings.digest, intent.settings.digest, refreshed)
        from control_plane.dokploy.slot_website_settings import _verify_startup_containers

        _verify_startup_containers(plan, intent, result, self.execute)
        return result
