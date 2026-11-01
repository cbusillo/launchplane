from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_import_parameters import (
    CONFIG_PARAMETER_RUNTIME_PREFIX,
    CONFIG_PARAMETER_SECRET_PREFIX,
    config_parameter_runtime_key,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import product_config_targets


class SettingConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "runtime_record",
        "lane",
        "preview_profile",
        "preview_policy",
        "runtime_requirement",
        "secret_requirement",
        "target_overlay",
        "secret_binding",
        "odoo_override",
        "preview_template",
    ]
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
        for requirement in profile.expected_config.runtime_environment_keys:
            if requirement.key in selected:
                consumers.append(
                    SettingConsumer(
                        kind="runtime_requirement",
                        product=profile.product,
                        context=requirement.context,
                        instance=requirement.instance,
                        selected_keys=(requirement.key,),
                    )
                )
        for binding_requirement in profile.expected_config.managed_secret_bindings:
            if (
                binding_requirement.integration == "runtime_environment"
                and binding_requirement.binding_key in selected
            ):
                consumers.append(
                    SettingConsumer(
                        kind="secret_requirement",
                        product=profile.product,
                        context=binding_requirement.context,
                        instance=binding_requirement.instance,
                        selected_keys=(binding_requirement.binding_key,),
                    )
                )
        if profile.preview.context or profile.preview.override_env:
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
        for scope, names in (
            ("required_template_env_keys", profile.preview.required_template_env_keys),
            ("copied_env_keys", profile.preview.copied_env_keys),
            ("omitted_env_keys", profile.preview.omitted_env_keys),
            ("preview_url_env_keys", profile.preview.preview_url_env_keys),
            ("preview_domain_env_keys", profile.preview.preview_domain_env_keys),
        ):
            if matches := tuple(sorted(selected.intersection(names))):
                consumers.append(
                    SettingConsumer(
                        kind="preview_policy",
                        product=profile.product,
                        context=profile.preview.context,
                        instance=profile.preview.template_instance,
                        scope=scope,
                        selected_keys=matches,
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
        key=lambda item: (
            not bool(item.selected_keys),
            item.kind,
            item.product,
            item.context,
            item.instance,
            item.scope,
        )
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


class LaneSettingSources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: str
    instance: str
    sources: tuple[SettingConsumer, ...]
    overlapping_keys: tuple[str, ...]
    retired_keys: tuple[str, ...]


class PreviewSettingSourceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preview_id: str
    context: str
    state: str


class ProductSettingSourceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    lanes: tuple[LaneSettingSources, ...]
    preview_targets: tuple[PreviewSettingSourceTarget, ...]
    preview_targets_truncated: bool
    sources_truncated: bool
    runtime_consumption_verified: Literal[False] = False
    unqualified_populations: tuple[str, ...] = (
        "provider_environment",
        "preview_template_effective_values",
        "preview_generation_environment",
        "value_equality",
        "retained_operation_authority",
        "global_inclusive_legacy_resolvers",
        "managed_secret_resolution",
        "legacy_addon_environment_aliases",
    )


def product_setting_source_inventory(
    *,
    profile: LaunchplaneProductProfileRecord,
    records: Iterable[RuntimeEnvironmentRecord],
    targets: Iterable[DokployTargetRecord],
    bindings: Iterable[SecretBinding],
    overrides: Iterable[OdooInstanceOverrideRecord],
    previews: Iterable[PreviewRecord],
    limit: int = 500,
) -> ProductSettingSourceInventory:
    from control_plane.odoo_instance_overrides import (
        addon_setting_secret_env_key,
        config_parameter_secret_env_key,
    )

    if limit < 1:
        raise ValueError("A positive inventory limit is required.")
    lane_targets = product_config_targets(profile)
    contexts = {lane.context for lane in lane_targets}
    records = tuple(record for record in records if record.context in contexts)
    targets = tuple(target for target in targets if target.context in contexts)
    bindings = tuple(
        binding
        for binding in bindings
        if binding.context in contexts and binding.status == "configured"
    )
    keys = tuple(
        sorted(
            {
                *(key for record in records for key in record.env),
                *(key for target in targets for key in target.env),
                *(binding.binding_key for binding in bindings),
                *(item.key for item in profile.expected_config.runtime_environment_keys),
                *(item.binding_key for item in profile.expected_config.managed_secret_bindings),
                *profile.preview.copied_env_keys,
                *profile.preview.required_template_env_keys,
                *profile.preview.omitted_env_keys,
                *profile.preview.preview_url_env_keys,
                *profile.preview.preview_domain_env_keys,
            }
        )
    )
    inventory = setting_consumer_inventory(
        keys=keys,
        records=records,
        targets=targets,
        bindings=bindings,
        profiles=(profile,),
        limit=limit,
    )
    consumers = list(inventory.consumers)
    aliases: dict[str, str] = {}
    for override in overrides:
        if override.context not in contexts:
            continue
        groups: dict[str, set[str]] = {}
        for parameter in override.config_parameters:
            key = (
                config_parameter_secret_env_key(parameter.key)
                if parameter.value.source == "secret_binding"
                else config_parameter_runtime_key(parameter.key)
            )
            groups.setdefault(f"config_parameter:{parameter.value.source}", set()).add(key)
        for setting in override.addon_settings:
            identity = f"addon.{setting.addon}.{setting.setting}"
            secret_key = addon_setting_secret_env_key(
                addon_name=setting.addon,
                setting_name=setting.setting,
            )
            aliases[secret_key] = identity
            key = secret_key if setting.value.source == "secret_binding" else identity
            groups.setdefault(f"addon:{setting.value.source}", set()).add(key)
        if override.website_bootstrap is not None:
            groups["website_bootstrap"] = {
                f"website_bootstrap.{key}" for key in override.website_bootstrap.model_fields_set
            }
        if override.website_settings is not None:
            groups["website_settings"] = {
                f"website_settings.{key}"
                for key in override.website_settings.settings.model_fields_set
            }
        consumers.extend(
            SettingConsumer(
                kind="odoo_override",
                product=profile.product,
                context=override.context,
                instance=override.instance,
                scope=scope,
                selected_keys=tuple(sorted(names)),
            )
            for scope, names in sorted(groups.items())
        )
    if profile.preview.context:
        template_keys = set(profile.preview.copied_env_keys)
        if profile.driver_id == "odoo":
            template_keys.update(
                key
                for consumer in consumers
                if consumer.context == profile.preview.context
                and consumer.instance in {"", profile.preview.template_instance}
                and consumer.kind
                in {"runtime_record", "target_overlay", "secret_binding", "odoo_override"}
                for key in consumer.selected_keys or ()
            )
        template_keys.difference_update(profile.preview.omitted_env_keys)
        template_keys.difference_update(
            key
            for record in records
            if record.scope == "instance"
            and (record.context, record.instance)
            == (profile.preview.context, profile.preview.template_instance)
            for key in record.retired_provider_keys
        )
        consumers.append(
            SettingConsumer(
                kind="preview_template",
                product=profile.product,
                context=profile.preview.context,
                instance=profile.preview.template_instance,
                scope="template_input_unverified",
                selected_keys=tuple(sorted(template_keys)),
            )
        )
    sources_truncated = (
        inventory.truncated
        or len(consumers) > limit
        or any(len(consumer.selected_keys or ()) > limit for consumer in consumers)
    )
    consumers = [
        consumer.model_copy(
            update={
                "selected_keys": consumer.selected_keys[:limit]
                if consumer.selected_keys is not None
                else None,
            }
        )
        for consumer in consumers[:limit]
    ]
    lanes = []
    for lane in lane_targets:
        sources = tuple(
            consumer
            for consumer in consumers
            if consumer.context in {"", lane.context}
            and consumer.kind != "lane"
            and (
                lane.instance == "preview"
                if consumer.kind in {"preview_template", "preview_policy", "preview_profile"}
                else consumer.instance in {"", lane.instance}
                and (lane.instance != "preview" or consumer.instance == "preview")
            )
        )
        supplied: dict[str, int] = {}
        for source in sources:
            if source.kind not in {
                "runtime_record",
                "target_overlay",
                "secret_binding",
                "odoo_override",
                "preview_template",
            }:
                continue
            if source.kind == "odoo_override" and source.scope.endswith(
                (":runtime_environment", ":secret_binding")
            ):
                continue
            for key in source.selected_keys or ():
                identity = aliases.get(key, key)
                if identity.startswith(CONFIG_PARAMETER_SECRET_PREFIX):
                    identity = CONFIG_PARAMETER_RUNTIME_PREFIX + identity.removeprefix(
                        CONFIG_PARAMETER_SECRET_PREFIX
                    )
                supplied[identity] = supplied.get(identity, 0) + 1
        lanes.append(
            LaneSettingSources(
                context=lane.context,
                instance=lane.instance,
                sources=sources,
                overlapping_keys=tuple(sorted(key for key, count in supplied.items() if count > 1)),
                retired_keys=tuple(
                    sorted(
                        {
                            key
                            for record in records
                            if record.scope == "instance"
                            and (record.context, record.instance) == (lane.context, lane.instance)
                            for key in record.retired_provider_keys
                        }
                    )
                ),
            )
        )
    preview_targets = tuple(
        PreviewSettingSourceTarget(
            preview_id=preview.preview_id, context=preview.context, state=preview.state
        )
        for preview in previews
        if preview.context == profile.preview.context
        and preview.anchor_repo == profile.repository.split("/")[-1]
        and preview.state != "destroyed"
    )
    return ProductSettingSourceInventory(
        product=profile.product,
        lanes=tuple(lanes),
        preview_targets=preview_targets[:limit],
        preview_targets_truncated=len(preview_targets) > limit,
        sources_truncated=sources_truncated,
    )
