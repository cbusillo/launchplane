"""Persist typed startup intent and recreate only fenced Compose consumers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from collections.abc import Callable
import inspect
import json
import re
from typing import Any

from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.dokploy import api
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane.dokploy.slot_startup_authority import (
    ComposeProjectAuthority,
    ComposeServiceAuthority,
    StartupComposeAuthority,
    plan_digest,
    read_authority,
)
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
    authority: ComposeProjectAuthority
    services: tuple[tuple[SlotWriterContainer, str], ...]

    @property
    def argv(self) -> tuple[str, ...]:
        return self.authority.argv

    @property
    def env_file(self) -> str:
        return self.authority.env_file


class DokploySlotStartup:
    """Startup and explicit recovery under the existing release guard/pause.

    The host supplies persistent authority storage before using this adapter.
    Preflight only reads. Recovery never starts a container or repeats native
    maintenance; the host must freshly attest its original guard and pause.
    """

    def __init__(
        self,
        *,
        host: str,
        token: str,
        writers: ContainerSlotWriters,
        execute: ContainerCommand,
        authority_directory: str,
    ):
        self.host, self.token, self.writers, self.execute = host, token, writers, execute
        self.authority_directory = authority_directory

    def _inspect(self, project: _Project, service: str, container: str) -> None:
        observed = json.loads(self.execute(("docker", "inspect", container), None))
        if not isinstance(observed, list) or len(observed) != 1:
            raise ValueError("startup container inventory is ambiguous")
        config, state = observed[0]["Config"], observed[0]["State"]
        labels = config.get("Labels", {})
        authority = project.authority
        if (
            config["Image"] != project.slot.image
            or labels.get("com.docker.compose.project") != authority.project
            or labels.get("com.docker.compose.service") != service
            or labels.get("com.docker.compose.project.working_dir") != authority.directory
            or labels.get("com.docker.compose.project.config_files") != ",".join(authority.files)
            or labels.get("com.docker.compose.container-number") != "1"
            or str(labels.get("com.docker.compose.oneoff")).lower() != "false"
        ):
            raise ValueError("writer differs from durable Compose authority")
        if state.get("Running") or state.get("Restarting") or state.get("Paused"):
            raise ValueError("startup recovery requires stopped writers")

    def _containers(self, project: _Project, service: str) -> tuple[str, ...]:
        # Docker's full label inventory includes intermediate/one-off containers;
        # Compose ps alone may hide precisely the duplicate we must refuse.
        containers = tuple(
            self.execute(
                (
                    "docker",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=" + project.authority.project,
                    "--filter",
                    "label=com.docker.compose.service=" + service,
                ),
                None,
            ).split()
        )
        if len(set(containers)) != len(containers):
            raise ValueError("duplicate Docker inventory identities")
        for container in containers:
            self._inspect(project, service, container)
        return containers

    def _refresh_project(self, project: _Project) -> None:
        replacements = {}
        for writer, service in project.services:
            containers = self._containers(project, service)
            if len(containers) != 1:
                raise ValueError(
                    "recreated writer inventory is not unique; guarded recover required"
                )
            target = replace(writer, container=containers[0])
            if self.writers.active_jobs(target) != 0:
                raise ValueError("recreated writer has active database jobs")
            replacements[writer.writer] = target
        self.writers.targets = tuple(
            replacements[target.writer] if target.slot_id == project.slot.slot_id else target
            for target in self.writers.targets
        )

    def _projects(self, plan: OdooCutoverPlan, *, fenced: bool) -> tuple[_Project, ...]:
        if self.writers.plan != plan:
            raise ValueError("startup writer inventory belongs to another release")
        saved = read_authority(plan, self.authority_directory, self.execute)
        if saved and {project.slot_id for project in saved.projects} != {
            plan.old.slot_id,
            plan.new.slot_id,
        }:
            raise ValueError("durable Compose authority omits a release slot")
        projects = []
        for slot in (plan.old, plan.new):
            target = api.fetch_dokploy_target_payload(
                host=self.host, token=self.token, target_type="compose", target_id=slot.compose_id
            )
            app = str(target.get("appName") or "")
            if not app or target.get("domains"):
                raise ValueError("startup requires the admitted private Compose projects")
            writers = tuple(
                writer for writer in self.writers.targets if writer.slot_id == slot.slot_id
            )
            if saved:
                authority = next(
                    project for project in saved.projects if project.slot_id == slot.slot_id
                )
            else:
                authorities = []
                services = []
                for writer in writers:
                    observed = json.loads(
                        self.execute(("docker", "inspect", writer.container), None)
                    )
                    if not isinstance(observed, list) or len(observed) != 1:
                        raise ValueError("startup container inventory is ambiguous")
                    labels = observed[0]["Config"].get("Labels", {})
                    authorities.append(
                        (
                            labels.get("com.docker.compose.project"),
                            str(labels.get("com.docker.compose.project.working_dir") or ""),
                            tuple(
                                str(
                                    labels.get("com.docker.compose.project.config_files") or ""
                                ).split(",")
                            ),
                        )
                    )
                    services.append(
                        ComposeServiceAuthority(
                            writer_id=writer.writer.writer_id,
                            service=str(labels.get("com.docker.compose.service") or ""),
                        )
                    )
                if not authorities or len(set(authorities)) != 1 or authorities[0][0] != app:
                    raise ValueError("writer Compose authorities disagree")
                authority = ComposeProjectAuthority(
                    slot_id=slot.slot_id,
                    project=app,
                    directory=authorities[0][1],
                    files=authorities[0][2],
                    services=tuple(services),
                )
            if (
                authority.project != app
                or {service.writer_id for service in authority.services}
                != {writer.writer_id for writer in plan.writers}
                or len(authority.services) != len(plan.writers)
                or len({service.service for service in authority.services}) != len(plan.writers)
            ):
                raise ValueError("durable Compose authority differs from admitted writers/project")
            services_by_writer = {
                service.writer_id: service.service for service in authority.services
            }
            project = _Project(
                slot,
                authority,
                tuple((writer, services_by_writer[writer.writer.writer_id]) for writer in writers),
            )
            self._validate_config(project)
            if fenced:
                self._refresh_project(project)
            projects.append(project)
        return tuple(projects)

    def _validate_config(self, project: _Project) -> None:
        raw = json.loads(
            self.execute(
                project.argv
                + ("config", "--format", "json", "--no-env-resolution", "--no-interpolate"),
                None,
            )
        )
        for _, service in project.services:
            definition = raw["services"][service]
            if definition.get("image") != project.slot.image:
                raise ValueError("Compose writer image differs from the immutable release")
            paths = definition.get("env_file", [])
            if (
                not isinstance(paths, list)
                or not paths
                or str(paths[-1].get("path", "") if isinstance(paths[-1], dict) else paths[-1])
                not in {".env", project.env_file}
                or ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY in definition.get("environment", {})
            ):
                raise ValueError(
                    "writer startup must consume the durable env-file payload without an override"
                )

    def recover(
        self,
        plan: OdooCutoverPlan,
        slot: OdooSlot,
        intent: OdooWebsiteSettingsIntent,
        *,
        verify_guard: Callable[[OdooCutoverPlan], None],
        active_jobs: Callable[[OdooSlot, WriterTarget], int],
    ) -> StartupSettingsReadback:
        """One bounded convergence pass; a lost reply requires fresh observation.

        The installed host verifies the exclusive original operation AND its
        still-published write pause. Its database observer uses logical writer
        identity, including when no Docker container exists. No stale-ID probe
        can prove absence, drain, or an absent native maintenance outcome.
        """
        verify_guard(plan)
        if read_authority(plan, self.authority_directory, self.execute) is None:
            raise ValueError(
                "guarded recovery requires previously admitted durable Compose authority"
            )
        projects = self._projects(plan, fenced=False)
        inventory = []
        for project in projects:
            for writer, service in project.services:
                containers = self._containers(project, service)
                if active_jobs(project.slot, writer.writer) != 0:
                    raise ValueError("logical writer database jobs are not drained")
                inventory.append((project, service, containers))
        self._validate_intent(plan, slot, intent)
        self.preflight(plan)
        verify_guard(plan)
        # Remove only fully identified, stopped duplicate remnants, without
        # force or volumes. An unobserved/foreign/running remnant holds all work.
        for project, service, containers in inventory:
            if len(containers) > 1:
                for container in containers:
                    self._inspect(project, service, container)
                    self.execute(("docker", "container", "rm", container), None)
        result = self._configure(plan, intent, projects)
        verify_guard(plan)
        if any(
            active_jobs(project.slot, writer.writer) != 0
            for project in projects
            for writer, _ in project.services
        ):
            raise ValueError("logical writer database drain changed during recovery")
        return result

    def _validate_intent(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> None:
        if slot not in (plan.old, plan.new) or intent != (
            plan.website_settings if slot == plan.new else plan.rollback_website_settings
        ):
            raise ValueError("startup snapshot differs from the decision-bound slot")

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
        self._validate_intent(plan, slot, intent)
        projects = self._projects(plan, fenced=True)
        self.preflight(plan)
        return self._configure(plan, intent, projects)

    def _configure(
        self,
        plan: OdooCutoverPlan,
        intent: OdooWebsiteSettingsIntent,
        projects: tuple[_Project, ...],
    ) -> StartupSettingsReadback:
        authority = StartupComposeAuthority(
            plan_sha256=plan_digest(plan), projects=(projects[0].authority, projects[1].authority)
        )
        if read_authority(plan, self.authority_directory, self.execute, authority) != authority:
            raise ValueError("durable Compose authority is unverified")
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
