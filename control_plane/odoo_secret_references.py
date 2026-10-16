"""Reconcile config-parameter pointers with existing lane delivery, without values."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.runtime_environments import (
    SiteSecretDeliveryStore,
    site_delivered_secret_bindings_from_store,
)
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideWrite,
    ProductAuthorityBundle,
    RuntimeEnvironmentSetExpectation,
    SecretDeliverySetExpectation,
)


class SecretReferenceStore(SiteSecretDeliveryStore, Protocol):
    pass


@dataclass(frozen=True)
class SecretReferenceAuthority:
    metadata: SecretDeliverySetExpectation
    runtime: RuntimeEnvironmentSetExpectation

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        return tuple(
            item
            for item in self.metadata.records
            if (not integration or item.integration == integration)
            and (not context_name or item.context == context_name)
            and (not instance_name or item.instance == instance_name)
        )

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        return tuple(
            item
            for item in self.metadata.bindings
            if (not integration or item.integration == integration)
            and (not context_name or item.context == context_name)
            and (not instance_name or item.instance == instance_name)
        )

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return self.runtime.records


def read_secret_reference_authority(
    store: SecretReferenceStore, record: OdooInstanceOverrideRecord
) -> SecretReferenceAuthority:
    selection = SecretDeliverySetExpectation(
        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context=record.context,
        instance=record.instance,
        records=(),
        bindings=(),
    )
    # Preserve ordering for the canonical selector; digest and CAS use sorted snapshots.
    selection = selection.model_copy(
        update={
            "records": tuple(
                item
                for item in store.list_secret_records(integration=selection.integration, limit=None)
                if selection.includes(item)
            ),
            "bindings": tuple(
                item
                for item in store.list_secret_bindings(
                    integration=selection.integration, limit=None
                )
                if selection.includes(item)
            ),
        }
    )
    runtime = RuntimeEnvironmentSetExpectation(
        contexts=(), instances=((record.context, record.instance),), records=()
    )
    runtime = runtime.model_copy(
        update={
            "records": tuple(
                item for item in store.list_runtime_environment_records() if runtime.includes(item)
            )
        }
    )
    return SecretReferenceAuthority(selection, runtime)


def selected_config_parameter_bindings(
    record: OdooInstanceOverrideRecord,
    authority: SecretReferenceAuthority,
    keys: tuple[str, ...],
) -> dict[str, SecretBinding]:
    parameters = {item.key: item for item in record.config_parameters}
    records = {item.secret_id: item for item in authority.metadata.records}
    bindings = {item.binding_id: item for item in authority.metadata.bindings}
    delivered = site_delivered_secret_bindings_from_store(
        record_store=authority, context_name=record.context, instance_name=record.instance
    )
    selected: dict[str, SecretBinding] = {}
    for key in keys:
        parameter = parameters.get(key)
        if parameter is None or parameter.value.source != "secret_binding":
            raise ValueError("An existing secret-backed config parameter is required.")
        env_key = config_parameter_secret_env_key(key)
        source = bindings.get(parameter.value.secret_binding_id)
        destination = delivered.get(env_key)
        for binding in (source, destination):
            secret = records.get(binding.secret_id) if binding is not None else None
            if (
                binding is None
                or secret is None
                or binding.binding_key != env_key
                or binding.status != "configured"
                or secret.status != "configured"
                or secret.policy != "write_only"
                or secret.scope not in {"context", "context_instance"}
                or (secret.context, secret.instance) != (binding.context, binding.instance)
                or (secret.scope == "context") != (not binding.instance)
            ):
                raise ValueError(
                    "A configured lane-scoped reference and delivered binding are required."
                )
        # Delivery has deterministic tie breaking. A repair must not endorse competing
        # bindings at one scope, or multiple active env bindings for the same secret.
        candidates: dict[str, list[SecretBinding]] = {}
        for binding in authority.metadata.bindings:
            secret = records.get(binding.secret_id)
            if (
                binding.binding_key == env_key
                and binding.status == "configured"
                and secret is not None
                and secret.status == "configured"
            ):
                candidates.setdefault(binding.instance, []).append(binding)
                if (
                    sum(
                        item.status == "configured" and item.secret_id == binding.secret_id
                        for item in authority.metadata.bindings
                    )
                    != 1
                ):
                    raise ValueError("Ambiguous lane secret bindings require separate correction.")
        if any(len(items) > 1 for items in candidates.values()):
            raise ValueError("Ambiguous lane secret bindings require separate correction.")
        assert destination is not None
        selected[key] = destination
    return selected


class SecretReferenceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    runtime_key: str
    action: Literal["reconcile_reference", "already_agrees"]


class SecretReferencePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    entries: tuple[SecretReferenceEntry, ...]
    review_digest: str
    applied: bool = False
    read_back_matches: bool | None = None
    values_redacted: bool = True


def plan_secret_reference_reconciliation(
    *,
    store: SecretReferenceStore,
    profile: LaunchplaneProductProfileRecord,
    record: OdooInstanceOverrideRecord,
    keys: tuple[str, ...],
) -> tuple[SecretReferencePlan, ProductAuthorityBundle]:
    if len(keys) != len(set(keys)):
        raise ValueError("Unique existing secret-backed config parameter keys are required.")
    if profile.driver_id != "odoo" or not any(
        (lane.context, lane.instance) == (record.context, record.instance) for lane in profile.lanes
    ):
        raise ValueError("The product must own the override lane.")
    authority = read_secret_reference_authority(store, record)
    selected = selected_config_parameter_bindings(record, authority, keys)
    entries = tuple(
        SecretReferenceEntry(
            key=item.key,
            runtime_key=config_parameter_secret_env_key(item.key),
            action="already_agrees"
            if item.value.secret_binding_id == selected[item.key].binding_id
            else "reconcile_reference",
        )
        for item in sorted(record.config_parameters, key=lambda item: item.key)
        if item.key in selected
    )
    changed = any(item.action == "reconcile_reference" for item in entries)
    replacement = record.model_copy(
        update={
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if changed
            else record.updated_at,
            "source_label": "odoo-secret-reference-reconcile" if changed else record.source_label,
            "config_parameters": tuple(
                item.model_copy(
                    update={
                        "value": item.value.model_copy(
                            update={"secret_binding_id": selected[item.key].binding_id}
                        )
                    }
                )
                if item.key in selected
                else item
                for item in record.config_parameters
            ),
        }
    )
    # Stamp the metadata edit, preserving pending intent and every apply receipt:
    # correcting pointers does not apply settings to an Odoo database.
    digest_input = {
        "profile": profile.model_dump(mode="json"),
        "override": record.model_dump(mode="json"),
        "keys": sorted(keys),
        "secrets": sorted(item.model_dump_json() for item in authority.metadata.records),
        "bindings": sorted(item.model_dump_json() for item in authority.metadata.bindings),
        "runtime": sorted(item.model_dump_json() for item in authority.runtime.records),
    }
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest()
    return SecretReferencePlan(
        product=profile.product,
        context=record.context,
        instance=record.instance,
        entries=entries,
        review_digest=digest,
    ), ProductAuthorityBundle(
        expected_product_profiles=(profile,),
        required_product_config_target=(profile.product, record.context, record.instance),
        secret_delivery_read_sets=(authority.metadata,),
        runtime_environment_read_sets=(authority.runtime,),
        odoo_instance_override_writes=(
            OdooInstanceOverrideWrite(record=replacement, expected_record=record),
        )
        if changed
        else (),
    )


def require_config_parameter_secret_agreement(
    record: OdooInstanceOverrideRecord, store: SecretReferenceStore
) -> None:
    keys = tuple(
        item.key for item in record.config_parameters if item.value.source == "secret_binding"
    )
    if not keys:
        return
    selected = selected_config_parameter_bindings(
        record, read_secret_reference_authority(store, record), keys
    )
    if any(
        item.value.secret_binding_id != selected[item.key].binding_id
        for item in record.config_parameters
        if item.key in selected
    ):
        raise ValueError(
            "Config-parameter secret references disagree with lane delivery. "
            "Review the lane's odoo-secret-references/reconcile plan and correct its metadata first."
        )
