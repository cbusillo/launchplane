from dataclasses import dataclass
from time import monotonic
from typing import Literal

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_config_requirement_applies_to_lane,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.contracts.testing_external_resources import (
    TestingExternalResourceAudit,
    TestingExternalResourceEntry,
)
from control_plane.discord_external_resources import observe_discord_bot
from control_plane.product_secret_copy import require_product_lanes
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.testing_local_credentials import (
    TestingCredentialStore,
    _require_unambiguous_lane,
)

EXTERNAL_RESOURCE_ENTRY_LIMIT = 16
EXTERNAL_RESOURCE_READ_SECONDS = 90.0


class ExternalResourceInputsChanged(ValueError):
    pass


@dataclass(frozen=True)
class CapturedExternalResource:
    product: str
    context: str
    instance: Literal["testing", "prod"]
    binding_key: str
    binding: SecretBinding
    record: SecretRecord
    version: SecretVersion


@dataclass(frozen=True)
class ExternalResourceInputSnapshot:
    profiles: tuple[LaunchplaneProductProfileRecord, ...]
    deliveries: tuple[tuple[str, str, str, tuple[SecretBinding, ...]], ...]
    custody: tuple[tuple[SecretBinding, SecretRecord, SecretVersion], ...]
    entries: tuple[CapturedExternalResource, ...]


def capture_external_resource_inputs(
    store: TestingCredentialStore,
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
    *,
    inventory: bool,
) -> ExternalResourceInputSnapshot:
    current_profiles = {
        profile.product: profile for profile in store.list_product_profile_records()
    }
    if inventory and current_profiles != {profile.product: profile for profile in profiles}:
        raise ExternalResourceInputsChanged()
    deliveries = []
    custody = []
    entries = []
    for profile in profiles:
        if current_profiles.get(profile.product) != profile:
            raise ExternalResourceInputsChanged()
        for lane in profile.lanes:
            if lane.instance not in {"testing", "prod"}:
                continue
            instance: Literal["testing", "prod"] = (
                "testing" if lane.instance == "testing" else "prod"
            )
            require_product_lanes(
                store, product=profile.product, routes=((lane.context, lane.instance),)
            )
            _require_unambiguous_lane(store, lane.context, lane.instance)
            bindings = site_delivered_secret_bindings_from_store(
                record_store=store, context_name=lane.context, instance_name=lane.instance
            )
            deliveries.append(
                (
                    profile.product,
                    lane.context,
                    lane.instance,
                    tuple(bindings[key].model_copy(deep=True) for key in sorted(bindings)),
                )
            )
            compared = secrets.resolve_effective_secret_bindings_from_store(
                record_store=store,
                integration="runtime_environment",
                context_name=lane.context,
                instance_name=lane.instance,
                scopes=frozenset({"context", "context_instance"}),
            )
            for key in sorted(compared):
                comparison_binding, record = compared[key]
                version = store.read_secret_version(record.current_version_id)
                custody.append(
                    (
                        comparison_binding.model_copy(deep=True),
                        record.model_copy(deep=True),
                        version.model_copy(deep=True),
                    )
                )
            declarations = [
                requirement
                for requirement in profile.expected_config.managed_secret_bindings
                if requirement.external_provider is not None
                and product_config_requirement_applies_to_lane(
                    requirement_context=requirement.context,
                    requirement_instance=requirement.instance,
                    lane=lane,
                )
            ]
            for key in sorted({row.binding_key for row in declarations}):
                if any(
                    row.testing_local_credential
                    and row.binding_key == key
                    and product_config_requirement_applies_to_lane(
                        requirement_context=row.context,
                        requirement_instance=row.instance,
                        lane=lane,
                    )
                    for row in profile.expected_config.managed_secret_bindings
                ):
                    raise ValueError("The external resource declaration conflicts with generation.")
                binding = bindings.get(key)
                if binding is None:
                    raise ValueError("The external resource binding is unavailable.")
                record = store.read_secret_record(binding.secret_id)
                version = store.read_secret_version(record.current_version_id)
                if (
                    record.status != "configured"
                    or binding.status != "configured"
                    or record.integration != "runtime_environment"
                    or binding.integration != record.integration
                    or record.scope not in {"context", "context_instance"}
                    or record.context != lane.context
                    or record.instance not in {"", lane.instance}
                    or (binding.context, binding.instance) != (record.context, record.instance)
                    or version.secret_id != record.secret_id
                    or version.version_id != record.current_version_id
                ):
                    raise ValueError("The external resource binding is unavailable.")
                entries.append(
                    CapturedExternalResource(
                        profile.product,
                        lane.context,
                        instance,
                        key,
                        binding.model_copy(deep=True),
                        record.model_copy(deep=True),
                        version.model_copy(deep=True),
                    )
                )
                if len(entries) > EXTERNAL_RESOURCE_ENTRY_LIMIT:
                    raise ValueError("The external resource audit exceeds its bounded scope.")
    return ExternalResourceInputSnapshot(
        tuple(profile.model_copy(deep=True) for profile in profiles),
        tuple(deliveries),
        tuple(custody),
        tuple(entries),
    )


def audit_external_resources(
    store: TestingCredentialStore,
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
    *,
    captured: ExternalResourceInputSnapshot | None,
    inventory: bool = False,
) -> TestingExternalResourceAudit:
    deadline = monotonic() + EXTERNAL_RESOURCE_READ_SECONDS
    try:
        if captured is None:
            raise ValueError("The external resource input snapshot is unavailable.")

        def check_current() -> None:
            try:
                current = capture_external_resource_inputs(store, profiles, inventory=inventory)
            except Exception:
                raise ExternalResourceInputsChanged() from None
            if current != captured:
                raise ExternalResourceInputsChanged()

        results = []
        for entry in captured.entries:
            check_current()
            value = secrets._decrypt_secret_value(entry.version.ciphertext, entry.version.key_id)
            if not value.strip():
                raise ValueError("The external resource binding is unavailable.")
            observation = observe_discord_bot(value, deadline=deadline, check_current=check_current)
            results.append(
                TestingExternalResourceEntry(
                    product=entry.product,
                    context=entry.context,
                    instance=entry.instance,
                    binding_key=entry.binding_key,
                    version_id=entry.version.version_id,
                    observation=observation,
                )
            )
        check_current()
        return TestingExternalResourceAudit(
            status=(
                "not_configured"
                if not results
                else "observed"
                if all(
                    entry.observation.identity_status
                    == entry.observation.guild_status
                    == "observed"
                    for entry in results
                )
                else "unavailable"
            ),
            entries=tuple(results),
        )
    except ExternalResourceInputsChanged:
        return TestingExternalResourceAudit(status="unavailable", failure_code="inputs_changed")
    except Exception:
        return TestingExternalResourceAudit(
            status="unavailable", failure_code="configuration_unavailable"
        )
