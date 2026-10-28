"""Explicit Fishbowl source reuse inside an isolated PR preview.

The reference names an existing managed testing binding, never a supplied value.
Normal previews still receive no source settings or integration allowances.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from control_plane.provider_key_adoption import looks_like_credential
from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetRecord,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_secret_copy import (
    ProductSecretCopyError,
    ProductSecretCopyFrom,
    ProductSecretCopyStore,
    copy_source_value,
    resolve_copy_source,
)
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.contracts.secret_record import SecretBinding
from control_plane.runtime_key_safety import runtime_key_safety_environment_class
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

if TYPE_CHECKING:
    from control_plane.odoo_runtime_source_prerequisites import PrerequisiteStore


FISHBOWL_CONNECTION_PARAMETERS = ("host", "user", "db", "port", "ssl_verify", "password")


class PreviewImportSourceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


PREVIEW_FISHBOWL_SOURCE_ENV_KEY = "LAUNCHPLANE_PREVIEW_FISHBOWL_SOURCE"


def preview_fishbowl_clear_program() -> str:
    """Fixed database-local cleanup; no arbitrary SQL or parameter names from callers."""
    keys = [f"fishbowl.{name}" for name in FISHBOWL_CONNECTION_PARAMETERS]
    return """
import os
import sys
import psycopg2
if os.environ.get("PLATFORM_INSTANCE") != "preview":
    raise RuntimeError("Fishbowl source cleanup requires a preview container.")
with psycopg2.connect(
    host=os.environ.get("ODOO_DB_HOST") or "database",
    port=os.environ.get("ODOO_DB_PORT") or "5432",
    user=os.environ.get("ODOO_DB_USER") or "odoo",
    password=os.environ.get("ODOO_DB_PASSWORD") or "",
    dbname=sys.argv[1],
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM ir_config_parameter WHERE key = ANY(%s)", (%s,))
print("preview_fishbowl_source_cleared=true")
""" % ("%s", repr(keys))


class PreviewImportSourceStore(ProductSecretCopyStore, Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...


@dataclass(frozen=True)
class FishbowlPreviewSource:
    context: str
    instance: str
    binding: SecretBinding
    parameters: tuple[OdooConfigParameterOverride, ...]
    environment: dict[str, str]
    allowance: DokployTargetIntegrationAllowance
    authority_sha256: str

    def require_override_agreement(self, record: OdooInstanceOverrideRecord) -> None:
        """This transient payload uses the qualified copy source, not preview inheritance."""
        if (
            (record.context, record.instance) != (self.context, self.instance)
            or runtime_key_safety_environment_class(record.instance) != "preview"
            or record.config_parameters != self.parameters
            or self.binding.binding_key not in self.environment
            or any(
                item.value.secret_binding_id != self.binding.binding_id
                or config_parameter_secret_env_key(item.key) != self.binding.binding_key
                for item in record.config_parameters
                if item.value.source == "secret_binding"
            )
        ):
            raise ValueError("Preview override must match its qualified import source delivery.")


def resolve_fishbowl_preview_source(
    *,
    store: PreviewImportSourceStore,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    copy_from: ProductSecretCopyFrom,
    include_secret_value: bool = False,
    control_plane_root: Path,
    database_url: str | None,
) -> FishbowlPreviewSource:
    if (
        copy_from.instance != "testing"
        or copy_from.context != profile.preview.context
        or runtime_key_safety_environment_class(preview_slug) != "preview"
    ):
        raise PreviewImportSourceError(
            "source_lane", "Preview import source requires the product's testing lane."
        )
    # Reuse the managed copy resolver's ownership, precedence and version fences.
    # Its target is the source stable lane; the stricter preview sharing check below
    # permits reuse only of an already classified read-only source.
    key = config_parameter_secret_env_key("fishbowl.password")
    try:
        _profile, binding, record, version = resolve_copy_source(
            store,
            product=profile.product,
            target_context=copy_from.context,
            target_instance=copy_from.instance,
            binding_key=key,
            reference=copy_from,
        )
    except ProductSecretCopyError as error:
        raise PreviewImportSourceError(error.code, str(error)) from error
    sharing = binding.sharing_reason
    if (
        record.scope != "context_instance"
        or binding.declared_secret_class != "shared_safe"
        or sharing is None
        or sharing.kind != "read_only_source"
        or not sharing.evidence.strip()
    ):
        raise PreviewImportSourceError(
            "unsafe_binding",
            "Preview import source requires an exact read-only shared-safe binding.",
        )
    target = store.read_dokploy_target_record(
        context_name=copy_from.context, instance_name=copy_from.instance
    )
    allowances = [
        item
        for item in target.policies.integration_allowances
        if item.integration == "fishbowl" and item.kind == "read_only_source"
    ]
    if len(allowances) != 1:
        raise PreviewImportSourceError(
            "allowance_required", "Preview import source requires an existing read-only allowance."
        )
    from control_plane.odoo_runtime_source_prerequisites import read_source_consumers

    qualified = read_source_consumers(
        store=cast("PrerequisiteStore", store),
        product=profile.product,
        control_plane_root=control_plane_root,
        database_url=database_url,
        mode="preview",
        sources=("fishbowl",),
    )
    source = next((item for item in qualified.sources if item.source == "fishbowl"), None)
    if qualified.runtime != "verified" or source is None or not source.parameters:
        raise PreviewImportSourceError(
            "source_unqualified",
            "The testing import source requires current producer and payload agreement.",
        )
    current = resolve_copy_source(
        store,
        product=profile.product,
        target_context=copy_from.context,
        target_instance=copy_from.instance,
        binding_key=key,
        reference=copy_from,
    )
    if (
        current != (_profile, binding, record, version)
        or store.read_dokploy_target_record(
            context_name=copy_from.context, instance_name=copy_from.instance
        )
        != target
    ):
        raise PreviewImportSourceError(
            "source_changed", "The testing import source changed during qualification."
        )
    parameters: list[OdooConfigParameterOverride] = []
    selected_values: dict[str, str] = {}
    for name, value in source.parameters.items():
        parameter_key = f"fishbowl.{name}"
        if looks_like_credential(parameter_key, value):
            raise PreviewImportSourceError(
                "secret_setting", "Preview import source requires non-secret connection settings."
            )
        selected_values[parameter_key] = value
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
        "qualification": qualified.evidence_sha256,
        "copy_from": copy_from.model_dump(mode="json"),
        "binding": binding.model_dump(mode="json"),
        "allowance": allowances[0].model_dump(mode="json"),
        "parameters": selected_values,
    }
    digest = nonvalidating_compact_sorted_json_sha256(authority)
    return FishbowlPreviewSource(
        context=profile.preview.context,
        instance=preview_slug,
        binding=binding,
        parameters=tuple(parameters),
        environment={key: copy_source_value(version)} if include_secret_value else {},
        allowance=allowances[0],
        authority_sha256=digest,
    )
