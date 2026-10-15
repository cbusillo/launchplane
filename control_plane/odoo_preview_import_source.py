"""Explicit Fishbowl source reuse inside an isolated PR preview.

The reference names an existing managed testing binding, never a supplied value.
Normal previews still receive no source settings or integration allowances.
"""

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetRecord,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord, ScalarValue
from control_plane.product_secret_copy import (
    ProductSecretCopyFrom,
    ProductSecretCopyStore,
    copy_source_value,
    resolve_copy_source,
)
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.runtime_key_safety import runtime_key_safety_environment_class


class PreviewImportSourceStore(ProductSecretCopyStore, Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...


@dataclass(frozen=True)
class FishbowlPreviewSource:
    parameters: tuple[OdooConfigParameterOverride, ...]
    environment: dict[str, str]
    allowance: DokployTargetIntegrationAllowance
    authority_sha256: str


def resolve_fishbowl_preview_source(
    *,
    store: PreviewImportSourceStore,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    copy_from: ProductSecretCopyFrom,
) -> FishbowlPreviewSource:
    if (
        copy_from.instance != "testing"
        or copy_from.context != profile.preview.context
        or runtime_key_safety_environment_class(preview_slug) != "preview"
    ):
        raise ValueError("Preview import source requires the product's testing lane.")
    # Reuse the managed copy resolver's ownership, precedence and version fences.
    # Its target is the source stable lane; the stricter preview sharing check below
    # permits reuse only of an already classified read-only source.
    key = config_parameter_secret_env_key("fishbowl.password")
    _profile, binding, record, version = resolve_copy_source(
        store,
        product=profile.product,
        target_context=copy_from.context,
        target_instance=copy_from.instance,
        binding_key=key,
        reference=copy_from,
    )
    sharing = binding.sharing_reason
    if (
        record.scope != "context_instance"
        or binding.declared_secret_class != "shared_safe"
        or sharing is None
        or sharing.kind != "read_only_source"
        or not sharing.evidence.strip()
    ):
        raise ValueError("Preview import source requires an exact read-only shared-safe binding.")
    target = store.read_dokploy_target_record(
        context_name=copy_from.context, instance_name=copy_from.instance
    )
    allowances = [
        item
        for item in target.policies.integration_allowances
        if item.integration == "fishbowl" and item.kind == "read_only_source"
    ]
    if len(allowances) != 1:
        raise ValueError("Preview import source requires an existing read-only allowance.")
    values: dict[str, ScalarValue] = {}
    retired_keys: set[str] = set()
    for scope in ("context", "instance"):
        records = [
            item
            for item in store.list_runtime_environment_records(
                context_name=copy_from.context, instance_name=copy_from.instance
            )
            if item.scope == scope
            and item.context == copy_from.context
            and item.instance == (copy_from.instance if scope == "instance" else "")
        ]
        if len(records) > 1:
            raise ValueError("Preview import source runtime authority is ambiguous.")
        for item in records:
            values.update(item.env)
            retired_keys.update(item.retired_provider_keys)
    values.update(target.env)
    values = {key: value for key, value in values.items() if key not in retired_keys}
    parameters: list[OdooConfigParameterOverride] = []
    selected_values: dict[str, str] = {}
    for name in ("host", "user", "db", "port", "ssl_verify"):
        parameter_key = f"fishbowl.{name}"
        value = values.get("ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__" + name.upper())
        if value is None or not str(value).strip():
            if name in {"host", "user", "db"}:
                raise ValueError("Preview import source runtime settings are incomplete.")
            continue
        selected_values[parameter_key] = str(value)
        parameters.append(
            OdooConfigParameterOverride(
                key=parameter_key, value=OdooOverrideValue(source="literal", value=value)
            )
        )
    parameters.append(
        OdooConfigParameterOverride(
            key="fishbowl.password",
            value=OdooOverrideValue(source="secret_binding", secret_binding_id=binding.binding_id),
        )
    )
    authority = {
        "copy_from": copy_from.model_dump(mode="json"),
        "binding": binding.model_dump(mode="json"),
        "allowance": allowances[0].model_dump(mode="json"),
        "parameters": selected_values,
    }
    digest = hashlib.sha256(
        json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FishbowlPreviewSource(
        parameters=tuple(parameters),
        environment={key: copy_source_value(version)},
        allowance=allowances[0],
        authority_sha256=digest,
    )
