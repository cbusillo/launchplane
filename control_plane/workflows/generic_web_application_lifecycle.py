"""Declared application maintenance and schedules under the deploy effect fence."""

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Protocol, runtime_checkable, cast
from uuid import uuid4

import click

from control_plane.contracts.generic_web_extensions import (
    GenericWebApplicationSchedule,
    GenericWebLaneExtensions,
    GenericWebMaintenanceCommand,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.post_deploy import DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    application_snapshot_for_deploy,
    recorded_application_lifecycle,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)


def schedule_payload(
    definition: GenericWebMaintenanceCommand,
    application_id: str,
    *,
    enabled: bool,
) -> api.JsonObject:
    return {
        "name": definition.name,
        "cronExpression": definition.cron_expression
        if isinstance(definition, GenericWebApplicationSchedule)
        else DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": definition.command,
        "applicationId": application_id,
        "enabled": enabled,
        "timezone": "UTC",
    }


class GenericWebApplicationLifecycle(Protocol):
    snapshot: ApplicationLifecycleSnapshot
    effect_attempted: bool

    def capture(self) -> None: ...
    def prepare(self) -> None: ...
    def maintain(self) -> dict[str, str]: ...
    def complete(self) -> dict[str, str]: ...
    def restore(self) -> None: ...
    def hold(self) -> None: ...


@dataclass
class RecordedApplicationLifecycle:
    """A product verification hook needs a receipt, without provider schedule access."""

    snapshot: ApplicationLifecycleSnapshot
    effect_attempted: bool = False

    def capture(self) -> None:
        pass

    def prepare(self) -> None:
        pass

    def maintain(self) -> dict[str, str]:
        return {}

    def complete(self) -> dict[str, str]:
        return {}

    def restore(self) -> None:
        pass

    def hold(self) -> None:
        pass


@runtime_checkable
class GenericWebApplicationLifecycleProvider(Protocol):
    def application_lifecycle(
        self,
        *,
        control_plane_root: Path,
        snapshot: ApplicationLifecycleSnapshot,
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
    snapshot: ApplicationLifecycleSnapshot
    checkpoint: Callable[[str], None]
    effect_started: Callable[[], None]
    effect_attempted: bool = False
    observed: dict[str, api.JsonObject | None] = field(default_factory=dict)

    def _mutate(self, phase: str) -> None:
        self.checkpoint(phase)
        self.effect_started()
        self.effect_attempted = True

    def _read(self, name: str) -> api.JsonObject | None:
        observed = api.read_dokploy_application_schedule(
            host=self.host,
            token=self.token,
            application_id=self.application_id,
            schedule_name=name,
        )
        if observed is None:
            return None
        if observed.get("applicationId") != self.application_id or not isinstance(
            observed.get("enabled"), bool
        ):
            raise click.ClickException("Application schedule has no exact target/state readback.")
        return {key: observed.get(key) for key in api._DOKPLOY_APPLICATION_SCHEDULE_READBACK_FIELDS}

    def _upsert(self, payload: api.JsonObject, phase: str) -> api.JsonObject:
        self._mutate(phase)
        return api.upsert_dokploy_application_schedule(
            host=self.host,
            token=self.token,
            application_id=self.application_id,
            schedule_payload=payload,
        )

    def _delete(self, name: str) -> None:
        observed = self._read(name)
        if observed is not None:
            self._mutate("application_schedule_remove")
            api.delete_dokploy_application_schedule(
                host=self.host,
                token=self.token,
                application_id=self.application_id,
                schedule_payload=observed,
            )

    def capture(self) -> None:
        # Capture every baseline before the first provider mutation. The caller
        # durably writes this artifact-bound receipt before prepare().
        for name in self.snapshot.managed_names:
            observed = self._read(name)
            self.observed[name] = observed
            if self.snapshot.mode == "rollback" and name not in self.snapshot.current_originals:
                self.snapshot.current_originals[name] = observed
            if self.snapshot.mode == "deploy" and name not in self.snapshot.originals:
                self.snapshot.originals[name] = observed
        if self.snapshot.restore_declared:
            for definition in self.snapshot.extensions.schedules:
                self.snapshot.originals[definition.name] = schedule_payload(
                    definition,
                    self.application_id,
                    enabled=definition.enabled,
                )

    def prepare(self) -> None:
        for payload in self.observed.values():
            if payload is not None and payload["enabled"]:
                self._upsert({**payload, "enabled": False}, "application_schedule_quiesce")

    def maintain(self) -> dict[str, str]:
        if self.snapshot.mode == "rollback":
            return {}
        evidence: dict[str, str] = {}
        for definition in self.snapshot.extensions.maintenance:
            if definition.on_reset != self.snapshot.reset:
                continue
            self._run_command(definition)
            evidence[f"maintenance:{definition.name}"] = "pass"
        return evidence

    def complete(self) -> dict[str, str]:
        if self.snapshot.mode == "rollback":
            self._restore(canary=True)
            return {"application_rollback": "qualified"}
        evidence: dict[str, str] = {}
        declared = {item.name for item in self.snapshot.extensions.schedules}
        for name in self.snapshot.managed_names:
            if name not in declared:
                self._delete(name)
        for definition in self.snapshot.extensions.schedules:
            disabled = schedule_payload(definition, self.application_id, enabled=False)
            schedule = self._upsert(disabled, "application_schedule_prepare")
            if definition.enabled:
                self._run_schedule(schedule, definition.timeout_seconds)
                self._upsert({**disabled, "enabled": True}, "application_schedule_activate")
            evidence[f"schedule:{definition.name}"] = "pass"
        return evidence

    def _run_schedule(self, schedule: api.JsonObject, timeout_seconds: int) -> None:
        self._mutate("application_schedule_canary")
        api.run_dokploy_schedule(
            host=self.host,
            token=self.token,
            schedule_id=api.schedule_key(schedule),
            timeout_seconds=timeout_seconds,
        )

    def _run_command(self, definition: GenericWebMaintenanceCommand) -> None:
        observed = self._read(definition.name)
        if observed is not None and observed != schedule_payload(
            definition, self.application_id, enabled=False
        ):
            raise click.ClickException(
                "Application maintenance cannot replace a different or active schedule; declare a distinct command name."
            )
        schedule = self._upsert(
            schedule_payload(definition, self.application_id, enabled=False),
            "application_maintenance_prepare",
        )
        self._mutate("application_maintenance_execute")
        api.run_dokploy_schedule(
            host=self.host,
            token=self.token,
            schedule_id=api.schedule_key(schedule),
            timeout_seconds=definition.timeout_seconds,
        )

    def restore(self) -> None:
        self._restore(canary=False, current=self.snapshot.mode == "rollback")

    def _restore(self, *, canary: bool, current: bool = False) -> None:
        originals = self.snapshot.current_originals if current else self.snapshot.originals
        for name, original in originals.items():
            if original is None:
                self._delete(name)
                continue
            payload = original
            schedule = self._upsert(
                {**payload, "enabled": False} if canary else payload, "application_schedule_restore"
            )
            if canary and payload["enabled"]:
                definition = next(
                    (item for item in self.snapshot.extensions.schedules if item.name == name), None
                )
                self._run_schedule(schedule, definition.timeout_seconds if definition else 300)
                self._upsert(payload, "application_schedule_activate")

    def hold(self) -> None:
        # A new image or an inherited interrupted attempt is not qualified.
        # Never re-enable its old cron jobs until a proved rollback canary.
        for name in self.snapshot.managed_names:
            observed = self._read(name)
            if observed is not None and observed["enabled"]:
                self._upsert({**observed, "enabled": False}, "application_schedule_hold")


def application_lifecycle_for_lane(
    *,
    control_plane_root: Path,
    snapshot: ApplicationLifecycleSnapshot | None,
    provider: object,
    target_type: str,
    target_id: str,
    checkpoint: Callable[[str], None],
    effect_started: Callable[[], None],
) -> GenericWebApplicationLifecycle | None:
    if snapshot is None:
        return None
    if not snapshot.managed_names and not snapshot.extensions.maintenance:
        return RecordedApplicationLifecycle(snapshot)
    if not isinstance(provider, GenericWebApplicationLifecycleProvider):
        raise click.ClickException(
            "Deploy provider does not support the declared application lifecycle."
        )
    return provider.application_lifecycle(
        control_plane_root=control_plane_root,
        snapshot=snapshot,
        target_type=target_type,
        target_id=target_id,
        checkpoint=checkpoint,
        effect_started=effect_started,
    )


def dokploy_application_lifecycle(
    *,
    control_plane_root: Path,
    snapshot: ApplicationLifecycleSnapshot,
    target_type: str,
    target_id: str,
    checkpoint: Callable[[str], None],
    effect_started: Callable[[], None],
) -> GenericWebApplicationLifecycle:
    if target_type != "application" or not target_id:
        raise click.ClickException("Declared application lifecycle requires a Dokploy application.")
    host, token = source.read_dokploy_config(control_plane_root=control_plane_root)
    return DokployApplicationLifecycle(host, token, target_id, snapshot, checkpoint, effect_started)


class _PreviewLifecycleStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def write_deployment_record(self, record: DeploymentRecord) -> object: ...
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...


@dataclass
class RecordedPreviewApplicationLifecycle(DokployApplicationLifecycle):
    store: _PreviewLifecycleStore | None = None
    record_id: str = ""

    def _save(self) -> None:
        if self.store is None:
            return
        snapshot = self.snapshot
        qualified = snapshot.state == "qualified"
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id=self.record_id,
                artifact_identity=ArtifactIdentityReference(artifact_id=snapshot.artifact_id),
                context=snapshot.context,
                instance=snapshot.instance,
                source_git_ref=snapshot.source_git_ref,
                delegated_executor="control-plane.preview-application-lifecycle",
                deploy=DeploymentEvidence(
                    target_name=self.application_id,
                    target_type="application",
                    deploy_mode="preview-lifecycle",
                    status="pass" if qualified else "pending",
                    started_at=snapshot.captured_at,
                ),
                post_deploy_update=PostDeployUpdateEvidence(
                    attempted=qualified or self.effect_attempted,
                    status="pass" if qualified else "fail" if self.effect_attempted else "skipped",
                    evidence=snapshot.evidence(),
                ),
            )
        )

    def capture(self) -> None:
        super().capture()
        # Persist ownership and the full baseline before pausing any job.
        self._save()

    def complete(self) -> dict[str, str]:
        evidence = super().complete()
        self.snapshot.state = "qualified"
        self._save()
        return evidence

    def hold(self) -> None:
        super().hold()
        self.snapshot.state = "held"
        self._save()

    def restore(self) -> None:
        super().restore()
        self.snapshot.state = "restored"
        self._save()


def preview_application_lifecycle(
    *,
    host: str,
    token: str,
    application_id: str,
    product: str,
    context: str,
    instance: str,
    artifact_id: str,
    source_git_ref: str,
    extensions: GenericWebLaneExtensions,
    record_store: object | None = None,
) -> DokployApplicationLifecycle:
    can_record = all(
        callable(getattr(record_store, name, None))
        for name in ("read_deployment_record", "write_deployment_record", "list_deployment_records")
    )
    if extensions.configured and not can_record:
        raise click.ClickException(
            "Preview application lifecycle requires deployment receipt storage."
        )
    store = cast(_PreviewLifecycleStore, record_store) if can_record else None
    record_id = "deployment-preview-lifecycle-" + uuid4().hex
    snapshot = None
    if store is not None:
        previous: list[tuple[ApplicationLifecycleSnapshot, str]] = []
        for record in store.list_deployment_records(context_name=context, instance_name=instance):
            receipt = recorded_application_lifecycle(record)
            if receipt is not None and (
                receipt.product,
                receipt.target_id,
                receipt.provider_id,
            ) == (product, application_id, "dokploy"):
                previous.append((receipt, record.record_id))
        previous_id = max(previous, key=lambda item: item[0].captured_at)[1] if previous else ""
        snapshot = application_snapshot_for_deploy(
            store=store,
            product=product,
            context=context,
            instance=instance,
            provider_id="dokploy",
            target_id=application_id,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            deployment_record_id=record_id,
            extensions=extensions,
            rollback=False,
            previous_deployment_record_id=previous_id,
        )
    if snapshot is None:
        snapshot = ApplicationLifecycleSnapshot(
            product=product,
            context=context,
            instance=instance,
            provider_id="dokploy",
            target_id=application_id,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            extensions=extensions,
            managed_names=tuple(item.name for item in extensions.schedules),
        )
    return RecordedPreviewApplicationLifecycle(
        host,
        token,
        application_id,
        snapshot,
        lambda _phase: None,
        lambda: None,
        store=store,
        record_id=record_id,
    )
