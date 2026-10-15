"""Explicit Fishbowl source reuse inside an isolated PR preview.

The reference names an existing managed testing binding, never a supplied value.
Normal previews still receive no source settings or integration allowances.
"""

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.provider_key_adoption import looks_like_credential
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
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
    ProductSecretCopyError,
    ProductSecretCopyFrom,
    ProductSecretCopyStore,
    copy_source_value,
    resolve_copy_source,
)
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.runtime_key_safety import runtime_key_safety_environment_class


FISHBOWL_CONNECTION_PARAMETERS = ("host", "user", "db", "port", "ssl_verify", "password")


class PreviewImportSourceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


PREVIEW_FISHBOWL_SOURCE_ENV_KEY = "LAUNCHPLANE_PREVIEW_FISHBOWL_SOURCE"
FISHBOWL_OPTIONAL_DEFAULTS = {"port": "3306", "ssl_verify": "True"}


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
    include_secret_value: bool = False,
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
    values: dict[str, ScalarValue] = {}
    retired_keys: set[str] = set()
    for scope in ("context", "instance"):
        records = [
            item
            for item in store.list_runtime_environment_records(context_name=copy_from.context)
            if item.scope == scope
            and item.context == copy_from.context
            and item.instance == (copy_from.instance if scope == "instance" else "")
        ]
        if len(records) > 1:
            raise PreviewImportSourceError(
                "ambiguous_settings", "Preview import source runtime authority is ambiguous."
            )
        for item in records:
            values.update(item.env)
            retired_keys.update(item.retired_provider_keys)
    values.update(target.env)
    values = {key: value for key, value in values.items() if key not in retired_keys}
    bound_keys = {
        binding.binding_key
        for binding in store.list_secret_bindings(
            integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            context_name=copy_from.context,
            limit=None,
        )
        if binding.status == "configured"
        and binding.context == copy_from.context
        and binding.instance in {"", copy_from.instance}
    }
    parameters: list[OdooConfigParameterOverride] = []
    selected_values: dict[str, str] = {}
    for name in FISHBOWL_CONNECTION_PARAMETERS:
        if name == "password":
            continue
        parameter_key = f"fishbowl.{name}"
        runtime_key = config_parameter_runtime_key(parameter_key)
        value = values.get(runtime_key)
        if runtime_key in bound_keys or (
            value is not None and looks_like_credential(runtime_key, str(value))
        ):
            raise PreviewImportSourceError(
                "secret_setting", "Preview import source requires non-secret connection settings."
            )
        if value is None or not str(value).strip():
            if name in {"host", "user", "db"}:
                raise PreviewImportSourceError(
                    "settings_incomplete", "Preview import source runtime settings are incomplete."
                )
            value = FISHBOWL_OPTIONAL_DEFAULTS[name]
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
        environment={key: copy_source_value(version)} if include_secret_value else {},
        allowance=allowances[0],
        authority_sha256=digest,
    )
