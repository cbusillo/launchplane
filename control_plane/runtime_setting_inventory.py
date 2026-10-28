from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding


class SettingConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["runtime_record", "lane", "preview_profile", "target_overlay", "secret_binding"]
    product: str = ""
    context: str = ""
    instance: str = ""
    scope: str = ""
    selected_keys: tuple[str, ...] | None = None


class SettingConsumerInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumers: tuple[SettingConsumer, ...]
    total_consumers: int
    truncated: bool
    runtime_record_count: int
    product_profile_count: int
    target_record_count: int
    secret_binding_count: int
    runtime_consumption_verified: Literal[False] = False
    authority_records: tuple[SettingAuthorityConsumer, ...] = ()
    authority_record_count: int = 0
    authority_records_truncated: bool = False
    unqualified_populations: tuple[str, ...] = (
        "preview_generations",
        "preview_template_effective_values",
        "retained_operation_authority",
        "baked_images",
        "provider_environment",
        "managed_secret_resolution",
        "external_cli_consumers",
    )


class SettingAuthorityConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["reconcile_request", "production_recipient"]
    product: str
    context: str = ""
    instance: str
    state: str


def setting_consumer_inventory(
    *,
    keys: tuple[str, ...],
    records: Iterable[RuntimeEnvironmentRecord],
    profiles: Iterable[LaunchplaneProductProfileRecord],
    targets: Iterable[DokployTargetRecord],
    bindings: Iterable[SecretBinding],
    authority_records: Iterable[SettingAuthorityConsumer] = (),
    limit: int = 500,
) -> SettingConsumerInventory:
    if limit < 1:
        raise ValueError("A positive inventory limit is required.")
    selected = set(keys)
    consumers: list[SettingConsumer] = []
    record_count = profile_count = target_count = binding_count = 0
    for record in records:
        record_count += 1
        consumers.append(
            SettingConsumer(
                kind="runtime_record",
                context=record.context,
                instance=record.instance,
                scope=record.scope,
                selected_keys=tuple(sorted(selected.intersection(record.env))),
            )
        )
    for profile in profiles:
        profile_count += 1
        consumers.extend(
            SettingConsumer(
                kind="lane", product=profile.product, context=lane.context, instance=lane.instance
            )
            for lane in profile.lanes
        )
        if profile.preview.context:
            consumers.append(
                SettingConsumer(
                    kind="preview_profile",
                    product=profile.product,
                    context=profile.preview.context,
                    instance=profile.preview.template_instance,
                    selected_keys=tuple(
                        sorted(selected.intersection(profile.preview.override_env))
                    ),
                )
            )
    for target in targets:
        target_count += 1
        consumers.append(
            SettingConsumer(
                kind="target_overlay",
                context=target.context,
                instance=target.instance,
                selected_keys=tuple(sorted(selected.intersection(target.env))),
            )
        )
    for binding in bindings:
        binding_count += 1
        if binding.integration == "runtime_environment" and binding.binding_key in selected:
            consumers.append(
                SettingConsumer(
                    kind="secret_binding",
                    context=binding.context,
                    instance=binding.instance,
                    selected_keys=(binding.binding_key,),
                )
            )
    consumers.sort(
        key=lambda item: (item.kind, item.product, item.context, item.instance, item.scope)
    )
    authorities = sorted(
        authority_records,
        key=lambda item: (item.kind, item.product, item.context, item.instance, item.state),
    )
    return SettingConsumerInventory(
        consumers=tuple(consumers[:limit]),
        total_consumers=len(consumers),
        truncated=len(consumers) > limit,
        runtime_record_count=record_count,
        product_profile_count=profile_count,
        target_record_count=target_count,
        secret_binding_count=binding_count,
        authority_records=tuple(authorities[:limit]),
        authority_record_count=len(authorities),
        authority_records_truncated=len(authorities) > limit,
    )
