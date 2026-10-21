"""Artifact-bound lifecycle receipts, including the state to restore on rollback."""

from typing import Literal, Protocol, cast
from datetime import datetime, timezone
import hashlib

import click
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions


APPLICATION_LIFECYCLE_EVIDENCE_KEY = "generic_web_application_lifecycle"


class ApplicationLifecycleBaselineChanged(ValueError):
    """An unstarted original no longer matches its captured provider baseline."""


class ApplicationLifecycleSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    product: str
    context: str
    instance: str
    provider_id: str
    target_id: str
    artifact_id: str
    source_git_ref: str
    previous_artifact_id: str = ""
    previous_source_git_ref: str = ""
    extensions: GenericWebLaneExtensions
    originals: dict[str, dict[str, JsonValue] | None] = Field(default_factory=dict)
    managed_names: tuple[str, ...] = ()
    mode: Literal["deploy", "rollback"] = "deploy"
    reset: bool = False
    state: Literal["prepared", "not_started", "held", "qualified", "restored", "restore_failed"] = (
        "prepared"
    )
    previous_owned_names: tuple[str, ...] = ()
    current_originals: dict[str, dict[str, JsonValue] | None] = Field(default_factory=dict)
    restore_current_on_pre_image_failure: bool = False
    resume_from_hold: bool = False
    restore_declared: bool = False

    def evidence(self) -> dict[str, str]:
        return {APPLICATION_LIFECYCLE_EVIDENCE_KEY: self.model_dump_json()}

    @property
    def owned_names(self) -> tuple[str, ...]:
        if self.state in {"not_started", "restored", "restore_failed"}:
            return self.previous_owned_names
        if self.mode == "rollback":
            return tuple(name for name, payload in self.originals.items() if payload is not None)
        return tuple(item.name for item in self.extensions.schedules)


class _ApplicationLifecycleStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...


def recorded_application_lifecycle(record: DeploymentRecord) -> ApplicationLifecycleSnapshot | None:
    text = record.post_deploy_update.evidence.get(APPLICATION_LIFECYCLE_EVIDENCE_KEY)
    if not text:
        return None
    try:
        snapshot = ApplicationLifecycleSnapshot.model_validate_json(text)
    except ValueError as error:
        raise click.ClickException("Stored application lifecycle receipt is invalid.") from error
    if record.artifact_identity is None or (
        snapshot.context,
        snapshot.instance,
        snapshot.artifact_id,
        snapshot.source_git_ref,
    ) != (
        record.context,
        record.instance,
        record.artifact_identity.artifact_id,
        record.source_git_ref,
    ):
        raise click.ClickException(
            "Stored application lifecycle receipt has a different deployment binding."
        )
    return snapshot


def application_snapshot_for_deploy(
    *,
    store: object,
    product: str,
    context: str,
    instance: str,
    provider_id: str,
    target_id: str,
    artifact_id: str,
    source_git_ref: str,
    deployment_record_id: str,
    extensions: GenericWebLaneExtensions,
    rollback: bool,
    proved_pre_image_retry: bool = False,
    proved_no_effect_retry: bool = False,
    established_testing_predecessor: bool = False,
    rollback_source_deployment_record_id: str = "",
    rollback_target_deployment_record_id: str = "",
    previous_deployment_record_id: str = "",
) -> ApplicationLifecycleSnapshot | None:
    reader = cast(_ApplicationLifecycleStore, store)

    def read_snapshot(record_id: str) -> ApplicationLifecycleSnapshot | None:
        if not record_id:
            return None
        try:
            record = reader.read_deployment_record(record_id)
        except FileNotFoundError:
            return None
        value = recorded_application_lifecycle(record)
        if value is not None and (
            value.product,
            value.context,
            value.instance,
            value.provider_id,
            value.target_id,
        ) != (product, context, instance, provider_id, target_id):
            raise click.ClickException(
                "Application lifecycle receipt belongs to a different target."
            )
        return value

    same_attempt = read_snapshot(deployment_record_id)
    excluded_predecessor_states = {"not_started"}
    if established_testing_predecessor and not rollback and same_attempt is None:
        # Another prepared declaration does not establish this operation's
        # ownership. Preserve that original for exact-key recovery. Its live
        # native reservation, if any, still fences this new operation.
        excluded_predecessor_states.add("prepared")
    previous_artifact = previous_source = ""
    prior: ApplicationLifecycleSnapshot | None = None
    if callable(getattr(store, "read_environment_inventory", None)):
        try:
            inventory = reader.read_environment_inventory(
                context_name=context, instance_name=instance
            )
        except FileNotFoundError:
            pass
        else:
            previous_record = None
            if inventory.deployment_record_id:
                try:
                    previous_record = reader.read_deployment_record(inventory.deployment_record_id)
                except FileNotFoundError:
                    pass
            from control_plane.testing_activation import completed_application_predecessor

            independent = not rollback and completed_application_predecessor(
                store,
                product=product,
                context=context,
                target_id=target_id,
                inventory=inventory,
                deployment=previous_record,
            )
            if not independent and inventory.artifact_identity is not None:
                previous_artifact = inventory.artifact_identity.artifact_id
                previous_source = inventory.source_git_ref
            if not independent:
                prior = read_snapshot(inventory.deployment_record_id)
                if previous_record is not None and previous_record.artifact_identity is not None:
                    previous_artifact = previous_record.artifact_identity.artifact_id
                    previous_source = previous_record.source_git_ref
    if previous_deployment_record_id and prior is None:
        prior = read_snapshot(previous_deployment_record_id)
        if prior is not None:
            previous_artifact, previous_source = prior.artifact_id, prior.source_git_ref
    if prior is not None and prior.state in excluded_predecessor_states:
        prior = None
    receipts: list[ApplicationLifecycleSnapshot] = []
    if callable(getattr(store, "list_deployment_records", None)):
        # A paused attempt may not have deployed an image or written inventory.
        # Receipts carry capture time independently of provider deployment times.
        for record in cast(_ApplicationLifecycleRecordLister, store).list_deployment_records(
            context_name=context,
            instance_name=instance,
        ):
            candidate = recorded_application_lifecycle(record)
            if (
                candidate is not None
                and candidate.state not in excluded_predecessor_states
                and (
                    candidate.product,
                    candidate.provider_id,
                    candidate.target_id,
                )
                == (product, provider_id, target_id)
            ):
                receipts.append(candidate)
        latest = max(receipts, key=lambda item: item.captured_at, default=None)
        if (
            latest is not None
            and latest.state != "qualified"
            and (prior is None or latest.captured_at >= prior.captured_at)
        ):
            prior = latest
    if same_attempt is not None:
        if same_attempt.extensions != extensions and not rollback:
            raise click.ClickException(
                "Application lifecycle declarations changed for this reserved attempt; use a new operation after exact recovery."
            )
        if (same_attempt.artifact_id, same_attempt.source_git_ref) != (artifact_id, source_git_ref):
            raise click.ClickException("Application lifecycle attempt identity changed.")
        prior = same_attempt
        if proved_no_effect_retry:
            return same_attempt.model_copy(deep=True, update={"state": "prepared"})
        if proved_pre_image_retry:
            return same_attempt.model_copy(deep=True)

    source = read_snapshot(rollback_source_deployment_record_id) or prior
    if source is not None and source.state == "not_started":
        source = prior
    if rollback:
        target = read_snapshot(rollback_target_deployment_record_id)
        if source is None and target is None and not receipts:
            # Declaring future extensions does not make historical image-only
            # rollbacks acquire new maintenance or scheduling behavior.
            return None
        originals: dict[str, dict[str, JsonValue] | None] = {}
        restore_declared = False
        target_extensions = GenericWebLaneExtensions()
        baseline = next(
            (
                item
                for item in sorted(
                    [item for item in (source, *receipts) if item is not None],
                    key=lambda item: item.captured_at,
                    reverse=True,
                )
                if (item.previous_artifact_id, item.previous_source_git_ref)
                == (artifact_id, source_git_ref)
            ),
            None,
        )
        if baseline is not None:
            originals = dict(
                baseline.current_originals if baseline.mode == "rollback" else baseline.originals
            )
        elif (
            source is not None
            and source.mode == "rollback"
            and (source.artifact_id, source.source_git_ref) == (artifact_id, source_git_ref)
        ):
            originals = dict(source.originals)
        elif (
            target is not None
            and target.state == "qualified"
            and (target.artifact_id, target.source_git_ref) == (artifact_id, source_git_ref)
        ):
            if target.mode == "rollback":
                originals = dict(target.originals)
            else:
                target_extensions = target.extensions
                restore_declared = True
        else:
            raise click.ClickException(
                "Rollback has no artifact-bound application lifecycle receipt."
            )
        restore_current = source is None or source.state in {
            "qualified",
            "restored",
            "restore_failed",
        }
        current_originals: dict[str, dict[str, JsonValue] | None] = {}
        if source is not None and source.state in {"restored", "restore_failed"}:
            current_originals = dict(
                source.current_originals if source.mode == "rollback" else source.originals
            )
        if (
            source is not None
            and source.mode == "rollback"
            and (source.artifact_id, source.source_git_ref) == (artifact_id, source_git_ref)
        ):
            previous_artifact, previous_source = (
                source.previous_artifact_id,
                source.previous_source_git_ref,
            )
            if source.state != "qualified":
                current_originals = dict(source.current_originals)
        names = set(originals)
        if source is not None:
            names.update(source.owned_names)
            names.update(source.managed_names)
        names.update(item.name for item in target_extensions.schedules)
        for name in names:
            originals.setdefault(name, None)
        return ApplicationLifecycleSnapshot(
            product=product,
            context=context,
            instance=instance,
            provider_id=provider_id,
            target_id=target_id,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            previous_artifact_id=previous_artifact,
            previous_source_git_ref=previous_source,
            extensions=target_extensions,
            previous_owned_names=source.owned_names if source else (),
            current_originals=current_originals,
            restore_current_on_pre_image_failure=restore_current,
            originals=originals,
            managed_names=tuple(sorted(names)),
            mode="rollback",
            restore_declared=restore_declared,
        )

    held = prior is not None and prior.state in {"prepared", "held"}
    if (
        proved_pre_image_retry
        and same_attempt is not None
        and prior is same_attempt
        and prior.state in {"prepared", "held"}
    ):
        held = same_attempt.resume_from_hold or same_attempt.state == "held"
    carry_baseline = prior is not None and prior.state in {"prepared", "held", "restore_failed"}
    previous_names = (
        prior.managed_names
        if carry_baseline and prior is not None
        else (prior.owned_names if prior is not None else ())
    )
    if not extensions.configured and not previous_names:
        return None
    originals = (
        dict(prior.current_originals if prior.mode == "rollback" else prior.originals)
        if carry_baseline and prior is not None
        else {}
    )
    if carry_baseline and prior is not None:
        previous_artifact, previous_source = (
            prior.previous_artifact_id,
            prior.previous_source_git_ref,
        )
    names = set(previous_names) | {item.name for item in extensions.schedules}
    return ApplicationLifecycleSnapshot(
        product=product,
        context=context,
        instance=instance,
        provider_id=provider_id,
        target_id=target_id,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        previous_artifact_id=previous_artifact,
        previous_source_git_ref=previous_source,
        extensions=extensions,
        originals=originals,
        managed_names=tuple(sorted(names)),
        resume_from_hold=held,
        previous_owned_names=(prior.previous_owned_names if carry_baseline else prior.owned_names)
        if prior
        else (),
    )


class _ApplicationLifecycleRecordLister(Protocol):
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...


def generic_web_provider_deployment_record_id(
    provider_operation_key: str, *, context: str, instance: str
) -> str:
    digest = hashlib.sha256(provider_operation_key.encode()).hexdigest()[:24]
    return f"deployment-provider-operation-{digest}-{context}-{instance}"


def has_recorded_application_lifecycle(
    store: object,
    *,
    context: str,
    instance: str,
    deployment_record_id: str = "",
    artifact_id: str = "",
    source_git_ref: str = "",
) -> bool:
    """Read one bound operation (or current inventory), never the lane's history."""
    reader = cast(_ApplicationLifecycleStore, store)
    try:
        if not deployment_record_id:
            if not callable(getattr(store, "read_environment_inventory", None)):
                return False
            inventory = reader.read_environment_inventory(
                context_name=context, instance_name=instance
            )
            deployment_record_id = inventory.deployment_record_id
        if not deployment_record_id:
            return False
        record = reader.read_deployment_record(deployment_record_id)
    except FileNotFoundError:
        return False
    if (record.context, record.instance) != (context, instance):
        raise click.ClickException("Application lifecycle operation belongs to another lane.")
    if artifact_id and (
        record.artifact_identity is None or record.artifact_identity.artifact_id != artifact_id
    ):
        return False
    if source_git_ref and record.source_git_ref != source_git_ref:
        return False
    return recorded_application_lifecycle(record) is not None
