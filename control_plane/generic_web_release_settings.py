import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from typing import Protocol, cast

from control_plane import runtime_environments as environments
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.generic_web_release_settings import (
    GenericWebReleaseSettings,
    GenericWebReleaseBaseline,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane import secrets
from control_plane.contracts.release_review import ReleaseVersion
from control_plane.contracts.runtime_identity import (
    COMPOSE_IMAGE_REFERENCE_ENV_KEYS,
    runtime_identity_env,
)
from control_plane.contracts.deployment_record import DEPLOYMENT_OPERATION_EVIDENCE_KEY

RELEASE_SETTINGS_KEY = "release_runtime_settings"
ROLLBACK_SETTINGS_KEY = "release_runtime_settings_baseline"


def requires_initial_settings_request(
    production: ReleaseVersion, candidate: ReleaseVersion
) -> bool:
    return (
        production.runtime_settings is None
        and candidate.runtime_settings is not None
        and production.artifact_id == candidate.artifact_id
        and production.source_commit == candidate.source_commit
    )


def require_nonsecret_release_keys(store: object, context: str, keys: set[str]) -> None:
    if not callable(getattr(store, "list_secret_bindings", None)):
        return
    bindings = secrets.resolve_effective_secret_bindings_from_store(
        record_store=cast(environments.SiteSecretDeliveryStore, store),
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context,
        instance_name="prod",
        scopes=environments.site_secret_scopes("prod"),
    )
    if keys & bindings.keys():
        raise ValueError("Release settings overlap managed secret bindings.")


def recorded_release_settings(record: DeploymentRecord) -> GenericWebReleaseSettings | None:
    payload = record.runtime_source.get(RELEASE_SETTINGS_KEY)
    return GenericWebReleaseSettings.model_validate_json(payload) if payload is not None else None


def desired_release_settings(
    store: object, profile: LaunchplaneProductProfileRecord
) -> GenericWebReleaseSettings | None:
    lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
    if lane is None or profile.driver_id == "odoo" or lane.generic_web_cutover is not None:
        return None
    reader = getattr(store, "list_runtime_environment_records", None)
    if not callable(reader):
        return None
    records = tuple(
        record
        for record in cast(Callable[[], tuple[RuntimeEnvironmentRecord, ...]], reader)()
        if record.scope != "global"
    )
    definition = environments.build_runtime_environment_definition_from_records(records)
    target_env: dict[str, str] = {}
    target_reader = getattr(store, "read_dokploy_target_record", None)
    if callable(target_reader):
        try:
            target_env = dict(
                cast(Callable[..., DokployTargetRecord], target_reader)(
                    context_name=lane.context, instance_name=lane.instance
                ).env
            )
        except FileNotFoundError:
            pass
    retired = environments.retired_provider_keys_from_store(
        record_store=cast(environments.RuntimeEnvironmentRecordStore, store),
        context_name=lane.context,
        instance_name=lane.instance,
    )
    values = environments.resolve_app_values_from_definition(
        definition=definition,
        context_name=lane.context,
        instance_name=lane.instance,
        target_env=target_env,
        retired_keys=retired,
    )
    if callable(getattr(store, "list_secret_bindings", None)):
        secret_keys = environments.site_delivered_secret_bindings_from_store(
            record_store=cast(environments.SiteSecretDeliveryStore, store),
            context_name=lane.context,
            instance_name=lane.instance,
        )
        values = {key: value for key, value in values.items() if key not in secret_keys}
    read_inventory = getattr(store, "read_environment_inventory", None)
    read_deployment = getattr(store, "read_deployment_record", None)
    if callable(read_inventory) and callable(read_deployment):
        try:
            inventory = cast(Callable[..., EnvironmentInventory], read_inventory)(
                context_name=lane.context, instance_name=lane.instance
            )
            previous = recorded_release_settings(
                cast(Callable[[str], DeploymentRecord], read_deployment)(
                    inventory.deployment_record_id
                )
            )
            reserved_keys = set(COMPOSE_IMAGE_REFERENCE_ENV_KEYS)
            if inventory.runtime_identity is not None:
                reserved_keys.update(runtime_identity_env(inventory.runtime_identity))
            values = {key: value for key, value in values.items() if key not in reserved_keys}
            retired -= reserved_keys

        except FileNotFoundError:
            previous = None
        if previous is not None:
            retired = set(
                set(retired) | ((set(previous.values) | set(previous.absent_keys)) - values.keys())
            )
    if not values and not retired:
        return None
    require_nonsecret_release_keys(store, lane.context, set(values) | set(retired))
    return GenericWebReleaseSettings(values=values, absent_keys=tuple(sorted(retired)))


class ReleaseSettingsReader(Protocol):
    def read_release_settings(
        self, *, control_plane_root: Path, target: object, keys: frozenset[str]
    ) -> GenericWebReleaseSettings: ...


def recorded_release_baseline(record: DeploymentRecord) -> GenericWebReleaseBaseline | None:
    payload = record.runtime_source.get(ROLLBACK_SETTINGS_KEY)
    return GenericWebReleaseBaseline.model_validate_json(payload) if payload is not None else None


def merge_release_settings(
    previous: GenericWebReleaseSettings, replacement: GenericWebReleaseSettings
) -> GenericWebReleaseSettings:
    values = {
        key: value for key, value in previous.values.items() if key not in replacement.absent_keys
    }
    values.update(replacement.values)
    return GenericWebReleaseSettings(
        values=values,
        absent_keys=tuple(
            sorted((set(previous.absent_keys) | set(replacement.absent_keys)) - values.keys())
        ),
    )


def rollback_release_baseline(
    store: object,
    source: DeploymentRecord,
    target_id: str,
) -> GenericWebReleaseBaseline | None:
    visited: set[str] = set()
    settings = GenericWebReleaseSettings()
    while source.record_id not in visited:
        visited.add(source.record_id)
        baseline = recorded_release_baseline(source)
        if baseline is None:
            break
        settings = merge_release_settings(settings, baseline.settings)
        if baseline.deployment_record_id == target_id:
            return GenericWebReleaseBaseline(deployment_record_id=target_id, settings=settings)
        predecessor = getattr(store, "read_deployment_record")(baseline.deployment_record_id)
        if predecessor.context != source.context or predecessor.instance != source.instance:
            raise ValueError("Release baseline belongs to another lane.")
        source = predecessor
    return None


def prepare_release_settings(
    *,
    store: object,
    provider: object,
    control_plane_root: Path,
    target: object,
    context: str,
    settings: GenericWebReleaseSettings | None,
    evidence: dict[str, str],
    rollback_target: str = "",
    rollback_source: str = "",
) -> GenericWebReleaseSettings | None:
    from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget

    resolved = cast(GenericWebResolvedDeployTarget, target)
    read_record = getattr(store, "read_deployment_record")
    try:
        inventory = getattr(store, "read_environment_inventory")(
            context_name=context, instance_name="prod"
        )
    except FileNotFoundError:
        if settings is None and not rollback_target:
            return None
        raise
    previous = cast(
        DeploymentRecord, read_record(rollback_source or inventory.deployment_record_id)
    )
    retained_payload = evidence.get(ROLLBACK_SETTINGS_KEY)
    baseline = (
        GenericWebReleaseBaseline.model_validate_json(retained_payload)
        if retained_payload
        else None
    )
    if rollback_target:
        reviewed = recorded_release_settings(read_record(rollback_target))
        baseline = baseline or rollback_release_baseline(store, previous, rollback_target)
        if baseline is not None and baseline.deployment_record_id != rollback_target:
            raise ValueError("Rollback baseline differs from its pinned target.")
        settings = baseline.settings if baseline else reviewed
        current = recorded_release_settings(previous)
        if current is not None:
            current_keys = set(current.values) | set(current.absent_keys)
            retained_keys = set(settings.values) | set(settings.absent_keys) if settings else set()
            if not current_keys <= retained_keys:
                raise ValueError("Rollback lacks retained settings for the current release keys.")
        if reviewed is None:
            evidence.pop(RELEASE_SETTINGS_KEY, None)
        else:
            evidence[RELEASE_SETTINGS_KEY] = reviewed.model_dump_json()
    else:
        reviewed = recorded_release_settings(previous)
        settings = settings if settings is not None else reviewed
        if settings is None:
            return None
        encoded = settings.model_dump_json()
        original = evidence.get(RELEASE_SETTINGS_KEY)
        if (
            original is not None
            and GenericWebReleaseSettings.model_validate_json(original) != settings
        ):
            raise ValueError("Deployment retry must retain its original release settings.")
        evidence[RELEASE_SETTINGS_KEY] = encoded
        if baseline is None:
            keys = set(settings.values) | set(settings.absent_keys)
            if reviewed is not None:
                keys |= set(reviewed.values) | set(reviewed.absent_keys)
            require_nonsecret_release_keys(store, context, keys)
            if not callable(getattr(provider, "read_release_settings", None)):
                raise ValueError(
                    "The deploy provider cannot capture release settings for rollback."
                )
            captured = cast(ReleaseSettingsReader, provider).read_release_settings(
                control_plane_root=control_plane_root,
                target=resolved,
                keys=frozenset(keys),
            )
            if set(captured.values) | set(captured.absent_keys) != keys:
                raise ValueError("Provider did not capture every previous release setting.")
            baseline = GenericWebReleaseBaseline(
                deployment_record_id=previous.record_id,
                settings=merge_release_settings(captured, reviewed) if reviewed else captured,
            )
    if settings is not None:
        require_nonsecret_release_keys(
            store, context, set(settings.values) | set(settings.absent_keys)
        )
    if baseline is not None:
        evidence[ROLLBACK_SETTINGS_KEY] = baseline.model_dump_json()
    resolved.release_settings = settings
    return settings


def verify_recorded_release_settings(
    *,
    store: object,
    provider: object,
    control_plane_root: Path,
    target: object,
    record: DeploymentRecord,
) -> str:
    settings = recorded_release_settings(record)
    if record.runtime_source.get(DEPLOYMENT_OPERATION_EVIDENCE_KEY) == "rollback":
        baseline = recorded_release_baseline(record)
        settings = baseline.settings if baseline is not None else settings
    if settings is None:
        return ""
    keys = set(settings.values) | set(settings.absent_keys)
    require_nonsecret_release_keys(store, record.context, keys)
    if not callable(getattr(provider, "read_release_settings", None)):
        raise ValueError("Release settings readback is unavailable.")
    actual = cast(ReleaseSettingsReader, provider).read_release_settings(
        control_plane_root=control_plane_root,
        target=target,
        keys=frozenset(keys),
    )
    if actual != settings:
        raise ValueError("Provider settings differ from the recorded release.")
    return hashlib.sha256(json.dumps(actual.model_dump(), sort_keys=True).encode()).hexdigest()
