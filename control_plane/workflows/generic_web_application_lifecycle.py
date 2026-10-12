"""Declared application maintenance and schedules under the deploy effect fence."""

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import click

from control_plane.contracts.generic_web_extensions import (
    GenericWebApplicationSchedule,
    GenericWebLaneExtensions,
    GenericWebMaintenanceCommand,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.post_deploy import DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION


def schedule_payload(
    definition: GenericWebMaintenanceCommand,
    application_id: str,
    *,
    enabled: bool,
) -> api.JsonObject:
    return {
        "name": definition.name,
        "cronExpression": (
            definition.cron_expression
            if isinstance(definition, GenericWebApplicationSchedule)
            else DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION
        ),
        "scheduleType": "application",
        "shellType": "sh",
        "command": definition.command,
        "applicationId": application_id,
        "enabled": enabled,
        "timezone": "UTC",
    }


class GenericWebApplicationLifecycle(Protocol):
    def prepare(self) -> None: ...

    def maintain(self) -> dict[str, str]: ...

    def complete(self) -> dict[str, str]: ...

    def restore(self) -> None: ...


@runtime_checkable
class GenericWebApplicationLifecycleProvider(Protocol):
    def application_lifecycle(
        self,
        *,
        control_plane_root: Path,
        extensions: GenericWebLaneExtensions,
        target_type: str,
        target_id: str,
        checkpoint: Callable[[str], None],
        effect_started: Callable[[], None],
    ) -> GenericWebApplicationLifecycle: ...


@dataclass
class DokployApplicationLifecycle:
    host: str
    token: str = field(repr=False)
    application_id: str
    extensions: GenericWebLaneExtensions
    checkpoint: Callable[[str], None]
    effect_started: Callable[[], None]
    originals: dict[str, api.JsonObject | None] = field(default_factory=dict)

    def _mutate(self, phase: str) -> None:
        self.checkpoint(phase)
        self.effect_started()

    def prepare(self) -> None:
        for definition in self.extensions.schedules:
            observed = api.read_dokploy_application_schedule(
                host=self.host,
                token=self.token,
                application_id=self.application_id,
                schedule_name=definition.name,
            )
            if observed is None:
                self.originals[definition.name] = None
                continue
            if observed.get("applicationId") != self.application_id or not isinstance(
                observed.get("enabled"), bool
            ):
                raise click.ClickException(
                    "Application schedule has no exact target/state readback."
                )
            payload = {
                key: observed.get(key) for key in api._DOKPLOY_APPLICATION_SCHEDULE_READBACK_FIELDS
            }
            self.originals[definition.name] = payload
            if observed["enabled"]:
                self._mutate("application_schedule_quiesce")
                api.upsert_dokploy_application_schedule(
                    host=self.host,
                    token=self.token,
                    application_id=self.application_id,
                    schedule_payload={**payload, "enabled": False},
                )

    def maintain(self) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for definition in self.extensions.maintenance:
            self._run_command(definition)
            evidence[f"maintenance:{definition.name}"] = "pass"
        return evidence

    def complete(self) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for definition in self.extensions.schedules:
            self._mutate("application_schedule_prepare")
            disabled = schedule_payload(definition, self.application_id, enabled=False)
            schedule = api.upsert_dokploy_application_schedule(
                host=self.host,
                token=self.token,
                application_id=self.application_id,
                schedule_payload=disabled,
            )
            if definition.enabled:
                self._mutate("application_schedule_canary")
                api.run_dokploy_schedule(
                    host=self.host,
                    token=self.token,
                    schedule_id=api.schedule_key(schedule),
                    timeout_seconds=definition.timeout_seconds,
                )
                self._mutate("application_schedule_activate")
                api.upsert_dokploy_application_schedule(
                    host=self.host,
                    token=self.token,
                    application_id=self.application_id,
                    schedule_payload={**disabled, "enabled": True},
                )
            evidence[f"schedule:{definition.name}"] = "pass"
        return evidence

    def _run_command(self, definition: GenericWebMaintenanceCommand) -> None:
        self._mutate("application_maintenance_prepare")
        payload = schedule_payload(definition, self.application_id, enabled=False)
        observed = api.read_dokploy_application_schedule(
            host=self.host,
            token=self.token,
            application_id=self.application_id,
            schedule_name=definition.name,
        )
        if observed is not None and observed.get("enabled") is not False:
            raise click.ClickException("Application maintenance cannot replace an active schedule.")
        schedule = api.upsert_dokploy_application_schedule(
            host=self.host,
            token=self.token,
            application_id=self.application_id,
            schedule_payload=payload,
        )
        self._mutate("application_maintenance_execute")
        api.run_dokploy_schedule(
            host=self.host,
            token=self.token,
            schedule_id=api.schedule_key(schedule),
            timeout_seconds=definition.timeout_seconds,
        )

    def restore(self) -> None:
        errors: list[str] = []
        for definition in self.extensions.schedules:
            if definition.name not in self.originals:
                continue
            self._mutate("application_schedule_restore")
            try:
                original = self.originals[definition.name]
                if original is not None:
                    api.upsert_dokploy_application_schedule(
                        host=self.host,
                        token=self.token,
                        application_id=self.application_id,
                        schedule_payload=original,
                    )
                else:
                    observed = api.read_dokploy_application_schedule(
                        host=self.host,
                        token=self.token,
                        application_id=self.application_id,
                        schedule_name=definition.name,
                    )
                    if observed is not None:
                        api.delete_dokploy_application_schedule(
                            host=self.host,
                            token=self.token,
                            application_id=self.application_id,
                            schedule_payload={
                                key: observed.get(key)
                                for key in api._DOKPLOY_APPLICATION_SCHEDULE_READBACK_FIELDS
                            },
                        )
            except click.ClickException as error:
                errors.append(str(error))
        if errors:
            raise click.ClickException(
                "Application schedule restoration failed: " + "; ".join(errors)
            )


def application_lifecycle_for_lane(
    *,
    control_plane_root: Path,
    extensions: GenericWebLaneExtensions,
    provider: object,
    target_type: str,
    target_id: str,
    checkpoint: Callable[[str], None],
    effect_started: Callable[[], None],
) -> GenericWebApplicationLifecycle | None:
    if not extensions.configured:
        return None
    if not isinstance(provider, GenericWebApplicationLifecycleProvider):
        raise click.ClickException(
            "Deploy provider does not support the declared application lifecycle."
        )
    return provider.application_lifecycle(
        control_plane_root=control_plane_root,
        extensions=extensions,
        target_type=target_type,
        target_id=target_id,
        checkpoint=checkpoint,
        effect_started=effect_started,
    )


def dokploy_application_lifecycle(
    *,
    control_plane_root: Path,
    extensions: GenericWebLaneExtensions,
    target_type: str,
    target_id: str,
    checkpoint: Callable[[str], None],
    effect_started: Callable[[], None],
) -> GenericWebApplicationLifecycle:
    if target_type != "application" or not target_id:
        raise click.ClickException("Declared application lifecycle requires a Dokploy application.")
    host, token = source.read_dokploy_config(control_plane_root=control_plane_root)
    return DokployApplicationLifecycle(
        host, token, target_id, extensions, checkpoint, effect_started
    )
