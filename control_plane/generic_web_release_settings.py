from collections.abc import Callable
from pathlib import Path
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from typing import Protocol, cast

from control_plane import runtime_environments as environments
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.generic_web_release_settings import GenericWebReleaseSettings
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane import secrets

RELEASE_SETTINGS_KEY = "release_runtime_settings"
ROLLBACK_SETTINGS_KEY = "release_runtime_settings_baseline"


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


def recorded_release_settings(
    record: DeploymentRecord, *, rollback: bool = False
) -> GenericWebReleaseSettings | None:
    payload = (
        record.runtime_source.get(ROLLBACK_SETTINGS_KEY) if rollback else None
    ) or record.runtime_source.get(RELEASE_SETTINGS_KEY)
    return GenericWebReleaseSettings.model_validate_json(payload) if payload is not None else None


def desired_release_settings(
    store: object, profile: LaunchplaneProductProfileRecord
) -> GenericWebReleaseSettings | None:
    lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
    if lane is None or profile.driver_id == "odoo":
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


def prepare_release_settings(
    *,
    store: object,
    provider: object,
    control_plane_root: Path,
    target: object,
    context: str,
    settings: GenericWebReleaseSettings | None,
    rollback_target: str = "",
) -> GenericWebReleaseSettings | None:
    from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget

    resolved = cast(GenericWebResolvedDeployTarget, target)
    read_record = getattr(store, "read_deployment_record")
    if rollback_target:
        settings = recorded_release_settings(read_record(rollback_target), rollback=True)
        inventory = getattr(store, "read_environment_inventory")(
            context_name=context, instance_name="prod"
        )
        current = recorded_release_settings(
            read_record(inventory.deployment_record_id), rollback=True
        )
        if current is not None:
            current_keys = set(current.values) | set(current.absent_keys)
            retained_keys = set(settings.values) | set(settings.absent_keys) if settings else set()
            if not current_keys <= retained_keys:
                raise ValueError("Rollback lacks retained settings for the current release keys.")
    if settings is None:
        if resolved.release_settings is not None:
            raise ValueError("Recovered release settings no longer match the release.")
        return None
    if resolved.release_settings is not None and resolved.release_settings != settings:
        raise ValueError("Recovered release settings differ from the release.")
    keys = set(settings.values) | set(settings.absent_keys)
    require_nonsecret_release_keys(store, context, keys)
    if not callable(getattr(provider, "read_release_settings", None)):
        raise ValueError("The deploy provider cannot capture release settings for rollback.")
    if not rollback_target:
        inventory = getattr(store, "read_environment_inventory")(
            context_name=context, instance_name="prod"
        )
        previous = cast(DeploymentRecord, read_record(inventory.deployment_record_id))
        baseline = recorded_release_settings(previous, rollback=True)
        captured_keys = set(baseline.values) | set(baseline.absent_keys) if baseline else set()
        missing = keys - captured_keys
        if baseline is None or missing:
            captured = cast(ReleaseSettingsReader, provider).read_release_settings(
                control_plane_root=control_plane_root, target=resolved, keys=frozenset(missing)
            )
            if set(captured.values) | set(captured.absent_keys) != missing:
                raise ValueError("Provider did not capture every previous release setting.")
            baseline = GenericWebReleaseSettings(
                values={**(baseline.values if baseline else {}), **captured.values},
                absent_keys=tuple(
                    sorted(
                        set(baseline.absent_keys if baseline else ()) | set(captured.absent_keys)
                    )
                ),
            )
            getattr(store, "write_deployment_record")(
                previous.model_copy(
                    update={
                        "runtime_source": {
                            **previous.runtime_source,
                            ROLLBACK_SETTINGS_KEY: baseline.model_dump_json(),
                        },
                    }
                )
            )
    resolved.release_settings = settings
    return settings
