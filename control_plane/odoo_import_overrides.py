"""Plan removal of duplicated import literals and render from existing lane authority."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.odoo_import_parameters import import_parameter_runtime_key
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.provider_key_adoption import looks_like_credential
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideWrite,
    ProductAuthorityBundle,
    RuntimeEnvironmentSetExpectation,
)


class ImportRuntimeStore(Protocol):
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


class ImportOverrideEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    runtime_key: str
    action: str
    stale_literal: bool


class ImportOverridePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    entries: tuple[ImportOverrideEntry, ...]
    review_digest: str
    applied: bool = False
    values_redacted: bool = True
    live_sync_required: bool = False


def import_runtime_authority(
    *, record_store: ImportRuntimeStore, context: str, instance: str
) -> tuple[dict[str, str], RuntimeEnvironmentSetExpectation]:
    expectation = RuntimeEnvironmentSetExpectation(
        contexts=(context,), instances=((context, instance),), records=()
    )
    records = tuple(
        record
        for record in record_store.list_runtime_environment_records()
        if expectation.includes(record)
    )
    values: dict[str, str] = {}
    retired: set[str] = set()
    for scope in ("context", "instance"):
        layer = [record for record in records if record.scope == scope]
        if len(layer) > 1:
            raise ValueError("Ambiguous lane runtime authority")
        for record in layer:
            values.update({key: str(value) for key, value in record.env.items()})
            retired.update(record.retired_provider_keys)
    # Secret overlays outrank plain settings during delivery. Never materialize a
    # setting as a non-secret literal when a managed binding owns that runtime key.
    bound_keys = {
        binding.binding_key
        for binding in record_store.list_secret_bindings(
            integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, context_name=context
        )
        if binding.context == context and binding.instance in {"", instance}
    }
    return (
        {key: value for key, value in values.items() if key not in retired | bound_keys},
        expectation.model_copy(update={"records": records}),
    )


def import_runtime_value(*, key: str, values: dict[str, str]) -> str:
    runtime_key = import_parameter_runtime_key(key)
    value = values.get(runtime_key, "")
    if not value.strip() or looks_like_credential(runtime_key, value):
        raise ValueError("Import override requires an existing non-secret lane runtime value")
    return value


def plan_import_override_reconciliation(
    *,
    record_store: ImportRuntimeStore,
    profile: LaunchplaneProductProfileRecord,
    record: OdooInstanceOverrideRecord,
    keys: tuple[str, ...],
) -> tuple[ImportOverridePlan, ProductAuthorityBundle]:
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("Import reconciliation requires unique parameter keys")
    for key in keys:
        import_parameter_runtime_key(key)
    values, expectation = import_runtime_authority(
        record_store=record_store, context=record.context, instance=record.instance
    )
    selected = {override.key: override for override in record.config_parameters}
    entries: list[ImportOverrideEntry] = []
    for key in sorted(keys):
        override = selected.get(key)
        if override is None or override.value.source == "secret_binding":
            raise ValueError("Import reconciliation requires an existing non-secret override")
        runtime_value = import_runtime_value(key=key, values=values)
        entries.append(
            ImportOverrideEntry(
                key=key,
                runtime_key=import_parameter_runtime_key(key),
                action="already_referenced"
                if override.value.source == "runtime_environment"
                else "replace_literal_with_runtime_reference",
                stale_literal=override.value.source == "literal"
                and str(override.value.value) != runtime_value,
            )
        )
        selected[key] = override.model_copy(
            update={"value": OdooOverrideValue(source="runtime_environment")}
        )
    digest_input = {
        "profile": profile.model_dump(mode="json"),
        "override": record.model_dump(mode="json"),
        "runtime": sorted(
            (item.model_dump(mode="json") for item in expectation.records),
            key=lambda item: str(item["scope"]),
        ),
        "keys": sorted(keys),
    }
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    changed = any(entry.action != "already_referenced" for entry in entries)
    replacement = record.model_copy(
        update={
            "config_parameters": tuple(
                selected[override.key] for override in record.config_parameters
            ),
            "updated_at": datetime.now(timezone.utc).isoformat() if changed else record.updated_at,
        }
    )
    plan = ImportOverridePlan(
        product=profile.product,
        context=record.context,
        instance=record.instance,
        entries=tuple(entries),
        review_digest=digest,
    )
    bundle = ProductAuthorityBundle(
        expected_product_profiles=(profile,),
        required_product_config_target=(profile.product, record.context, record.instance),
        runtime_environment_read_sets=(expectation,),
        odoo_instance_override_writes=(
            OdooInstanceOverrideWrite(record=replacement, expected_record=record),
        ),
    )
    return plan, bundle


def runtime_values_for_override(
    record: OdooInstanceOverrideRecord, record_store: object | None
) -> dict[str, str]:
    if not any(item.value.source == "runtime_environment" for item in record.config_parameters):
        return {}
    if record_store is None:
        raise ValueError("Runtime-backed overrides require lane runtime authority")
    values, _expectation = import_runtime_authority(
        record_store=cast(ImportRuntimeStore, record_store),
        context=record.context,
        instance=record.instance,
    )
    return values
