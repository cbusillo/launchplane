from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import product_config_targets

SETTING_SOURCE_LIMIT = 500


class SettingReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: Literal["in_lane", "other_lane", "unresolved_or_out_of_scope"]
    binding_key: str = ""
    context: str = ""
    instance: str = ""


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
    ]
    product: str = ""
    context: str = ""
    instance: str = ""
    scope: str = ""
    selected_keys: tuple[str, ...] | None = None
    phases: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    references: tuple[SettingReference, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


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
    limit: int = SETTING_SOURCE_LIMIT,
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
        "exact_preview_override_records",
    )


def _override_reference(
    name: str,
    value: OdooOverrideValue,
    record: OdooInstanceOverrideRecord,
    bindings: dict[str, SecretBinding],
) -> SettingReference:
    binding = bindings.get(value.secret_binding_id)
    if binding is None:
        return SettingReference(name=name, status="unresolved_or_out_of_scope")
    in_lane = binding.context == record.context and binding.instance in {"", record.instance}
    return SettingReference(
        name=name,
        status="in_lane" if in_lane else "other_lane",
        binding_key=binding.binding_key,
        context=binding.context,
        instance=binding.instance,
    )


def _override_sources(
    record: OdooInstanceOverrideRecord,
    product: str,
    bindings: dict[str, SecretBinding],
) -> tuple[SettingConsumer, ...]:
    groups: dict[str, list[tuple[str, OdooOverrideValue]]] = {}
    for parameter in record.config_parameters:
        groups.setdefault(f"config_parameter:{parameter.value.source}", []).append(
            (parameter.key, parameter.value)
        )
    for setting in record.addon_settings:
        groups.setdefault(f"addon:{setting.value.source}", []).append(
            (f"{setting.addon}.{setting.setting}", setting.value)
        )
    sources = [
        SettingConsumer(
            kind="odoo_override",
            product=product,
            context=record.context,
            instance=record.instance,
            scope=scope,
            selected_keys=tuple(sorted(name for name, _value in items)),
            phases=record.apply_on,
            references=tuple(
                _override_reference(name, value, record, bindings)
                for name, value in items
                if value.source == "secret_binding"
            ),
        )
        for scope, items in sorted(groups.items())
    ]
    for scope, fields in (
        (
            "website_bootstrap",
            record.website_bootstrap.model_fields_set if record.website_bootstrap else (),
        ),
        (
            "website_settings",
            record.website_settings.settings.model_fields_set if record.website_settings else (),
        ),
    ):
        if fields:
            sources.append(
                SettingConsumer(
                    kind="odoo_override",
                    product=product,
                    context=record.context,
                    instance=record.instance,
                    scope=scope,
                    selected_keys=tuple(sorted(fields)),
                    phases=record.apply_on,
                )
            )
    return tuple(sources)


def product_setting_source_inventory(
    *,
    profile: LaunchplaneProductProfileRecord,
    records: Iterable[RuntimeEnvironmentRecord],
    targets: Iterable[DokployTargetRecord],
    bindings: Iterable[SecretBinding],
    overrides: Iterable[OdooInstanceOverrideRecord],
    previews: Iterable[PreviewRecord],
    limit: int = SETTING_SOURCE_LIMIT,
) -> ProductSettingSourceInventory:
    from control_plane.workflows.generic_web_preview import generic_web_preview_anchor_repo

    if limit < 1:
        raise ValueError("A positive inventory limit is required.")
    lane_targets = product_config_targets(profile)
    contexts = {lane.context for lane in lane_targets}
    pairs = {(lane.context, lane.instance) for lane in lane_targets}
    records = tuple(
        record
        for record in records
        if record.context in contexts
        and (not record.instance or (record.context, record.instance) in pairs)
    )
    targets = tuple(target for target in targets if (target.context, target.instance) in pairs)
    bindings = tuple(
        binding
        for binding in bindings
        if binding.context in contexts
        and binding.status == "configured"
        and (not binding.instance or (binding.context, binding.instance) in pairs)
    )
    keys = tuple(
        sorted(
            {
                *(key for record in records for key in record.env),
                *(key for target in targets for key in target.env),
                *(binding.binding_key for binding in bindings),
                *(item.key for item in profile.expected_config.runtime_environment_keys),
                *(item.binding_key for item in profile.expected_config.managed_secret_bindings),
                *profile.preview.override_env,
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
    bindings_by_id = {binding.binding_id: binding for binding in bindings}
    consumers.extend(
        source
        for record in overrides
        if (record.context, record.instance) in pairs
        for source in _override_sources(record, profile.product, bindings_by_id)
    )
    sources_truncated = (
        inventory.truncated
        or len(consumers) > limit
        or any(
            len(consumer.selected_keys or ()) > limit or len(consumer.references) > limit
            for consumer in consumers
        )
        or any(len(record.retired_provider_keys) > limit for record in records)
    )
    consumers = [
        consumer.model_copy(
            update={
                "selected_keys": consumer.selected_keys[:limit]
                if consumer.selected_keys is not None
                else None,
                "references": consumer.references[:limit],
            }
        )
        for consumer in consumers[:limit]
    ]
    lanes = tuple(
        LaneSettingSources(
            context=lane.context,
            instance=lane.instance,
            sources=tuple(
                consumer
                for consumer in consumers
                if consumer.context in {"", lane.context}
                and consumer.kind != "lane"
                and (
                    lane.instance == "preview"
                    if consumer.kind in {"preview_policy", "preview_profile"}
                    else consumer.instance in {"", lane.instance}
                    and (lane.instance != "preview" or consumer.instance == "preview")
                )
            ),
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
            )[:limit],
        )
        for lane in lane_targets[:limit]
    )
    previews = tuple(previews)
    anchor_repo = generic_web_preview_anchor_repo(profile.repository)
    preview_targets = tuple(
        PreviewSettingSourceTarget(
            preview_id=preview.preview_id, context=preview.context, state=preview.state
        )
        for preview in previews
        if preview.context == profile.preview.context
        and preview.anchor_repo == anchor_repo
        and preview.state != "destroyed"
    )
    return ProductSettingSourceInventory(
        product=profile.product,
        lanes=lanes,
        preview_targets=preview_targets[:limit],
        preview_targets_truncated=len(previews) > limit,
        sources_truncated=sources_truncated,
    )
