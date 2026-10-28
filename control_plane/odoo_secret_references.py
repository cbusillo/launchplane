"""Reconcile config-parameter pointers with existing lane delivery, without values."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.odoo_instance_override_record import (
    ODOO_CONFIG_PARAMETER_KEY_PATTERN,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.runtime_environments import (
    SiteSecretDeliveryStore,
    site_delivered_secret_bindings_from_store,
)
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety,
    latest_active_runtime_key_safety_policy,
    runtime_key_required_integrations,
    runtime_key_safety_environment_class,
)
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideWrite,
    ProductAuthorityBundle,
    RuntimeEnvironmentSetExpectation,
    SecretDeliverySetExpectation,
)


class SecretReferenceStore(SiteSecretDeliveryStore, Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def list_runtime_key_safety_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...


class ConfigParameterSecretReferenceError(ValueError):
    """A secret-backed config parameter cannot be delivered as stored.

    ``env_keys`` names the parameters' lane env keys, never a binding id or value.
    ``disagrees`` means each reference resolves but points somewhere other than
    what the lane delivers, which the reconcile route corrects.
    """

    def __init__(self, message: str, *, env_keys: tuple[str, ...], disagrees: bool = False):
        super().__init__(message)
        self.env_keys = env_keys
        self.disagrees = disagrees


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
    store: SiteSecretDeliveryStore, record: OdooInstanceOverrideRecord
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
    *,
    create_missing: bool = False,
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
        if (parameter is None and not create_missing) or (
            parameter is not None and parameter.value.source != "secret_binding"
        ):
            raise ValueError("An existing secret-backed config parameter is required.")
        env_key = config_parameter_secret_env_key(key)
        env_keys = (env_key,)
        destination = delivered.get(env_key)
        source = bindings.get(parameter.value.secret_binding_id) if parameter else destination
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
                raise ConfigParameterSecretReferenceError(
                    "A configured lane-scoped reference and delivered binding are required.",
                    env_keys=env_keys,
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
                    raise ConfigParameterSecretReferenceError(
                        "Ambiguous lane secret bindings require separate correction.",
                        env_keys=env_keys,
                    )
        if any(len(items) > 1 for items in candidates.values()):
            raise ConfigParameterSecretReferenceError(
                "Ambiguous lane secret bindings require separate correction.", env_keys=env_keys
            )
        assert destination is not None
        selected[key] = destination
    return selected


class SecretReferenceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    runtime_key: str
    action: Literal["add_reference", "reconcile_reference", "already_agrees"]


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


def _new_testing_reference_authority(
    store: SecretReferenceStore,
    profile: LaunchplaneProductProfileRecord,
    record: OdooInstanceOverrideRecord,
    selected: dict[str, SecretBinding],
    keys: tuple[str, ...],
    added: set[str],
) -> tuple[DokployTargetRecord, RuntimeKeySafetyPolicyRecord]:
    parameters = {item.key: item for item in record.config_parameters}
    for key in added:
        if not re.fullmatch(ODOO_CONFIG_PARAMETER_KEY_PATTERN, key):
            raise ValueError("A valid Odoo parameter name is required.")
        env_key = config_parameter_secret_env_key(key)
        if any(
            other != key and config_parameter_secret_env_key(other) == env_key
            for other in (*parameters, *keys)
        ):
            raise ValueError("Odoo parameter names must have distinct transport keys.")
        binding = selected[key]
        if (
            binding.instance != record.instance
            or binding.context != record.context
            or binding.declared_secret_class not in {"testing", "non_prod"}
            or binding.value_source != "supplied"
            or not any(
                requirement.binding_key == env_key
                and requirement.integration == RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
                and (requirement.context, requirement.instance) == (record.context, record.instance)
                for requirement in profile.expected_config.managed_secret_bindings
            )
        ):
            raise ValueError("Declare and independently supply an exact testing credential first.")
    target = store.read_dokploy_target_record(
        context_name=record.context, instance_name=record.instance
    )
    policy = latest_active_runtime_key_safety_policy(store)
    required = set().union(
        *(
            runtime_key_required_integrations(
                selected[key].binding_key, extra_markers=policy.integration_key_markers
            )
            for key in added
        )
    )
    qualified = {
        item.integration
        for item in target.policies.integration_allowances
        if item.kind == "dev_store" and item.evidence.strip()
    }
    if not required.issubset(qualified):
        raise ValueError(
            "Record the independently verified testing account's dev_store allowance first."
        )
    evaluation = evaluate_runtime_key_safety(
        target=RuntimeKeySafetyTarget(
            context=record.context, instance=record.instance, environment_class="testing"
        ),
        required_binding_keys=(selected[key].binding_key for key in added),
        secret_bindings=tuple(selected[key] for key in added),
        secret_rules=policy.rules,
        integration_key_markers=policy.integration_key_markers,
    )
    if evaluation.status != "pass":
        raise ValueError("The testing credential failed runtime key safety.")
    return target, policy


def plan_secret_reference_reconciliation(
    *,
    store: SecretReferenceStore,
    profile: LaunchplaneProductProfileRecord,
    record: OdooInstanceOverrideRecord | None,
    keys: tuple[str, ...],
    create_missing: bool = False,
    environment: str = "",
) -> tuple[SecretReferencePlan, ProductAuthorityBundle]:
    if len(keys) != len(set(keys)):
        raise ValueError("Unique existing secret-backed config parameter keys are required.")
    existing_record = record
    if record is None:
        lanes = [lane for lane in profile.lanes if lane.instance == environment]
        if not create_missing or len(lanes) != 1:
            raise ValueError("An existing override or one explicit testing lane is required.")
        record = OdooInstanceOverrideRecord(
            context=lanes[0].context, instance=lanes[0].instance, updated_at=profile.updated_at
        )
    if profile.driver_id != "odoo" or not any(
        (lane.context, lane.instance) == (record.context, record.instance) for lane in profile.lanes
    ):
        raise ValueError("The product must own the override lane.")
    authority = read_secret_reference_authority(store, record)
    parameters = {item.key: item for item in record.config_parameters}
    added = set(keys) - set(parameters)
    if added and (
        not create_missing or runtime_key_safety_environment_class(record.instance) != "testing"
    ):
        raise ValueError("New secret references require an explicit testing lane request.")
    selected = selected_config_parameter_bindings(
        record, authority, keys, create_missing=create_missing
    )
    target = None
    policy = None
    if added:
        target, policy = _new_testing_reference_authority(
            store, profile, record, selected, keys, added
        )
    entries = tuple(
        SecretReferenceEntry(
            key=key,
            runtime_key=config_parameter_secret_env_key(key),
            action="add_reference"
            if key in added
            else (
                "already_agrees"
                if parameters[key].value.secret_binding_id == selected[key].binding_id
                else "reconcile_reference"
            ),
        )
        for key in sorted(selected)
    )
    changed = any(item.action != "already_agrees" for item in entries)
    for key, binding in selected.items():
        parameters[key] = OdooConfigParameterOverride(
            key=key,
            value=OdooOverrideValue(source="secret_binding", secret_binding_id=binding.binding_id),
        )
    replacement = record.model_copy(
        update={
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if changed
            else record.updated_at,
            "source_label": "odoo-secret-reference-reconcile" if changed else record.source_label,
            "config_parameters": tuple(parameters[item.key] for item in record.config_parameters)
            + tuple(parameters[key] for key in sorted(added)),
            "apply_on": tuple(dict.fromkeys((*record.apply_on, "deploy", "promotion")))
            if added
            else record.apply_on,
            "last_apply": record.last_apply.for_override_edit() if added else record.last_apply,
        }
    )
    # Stamp the metadata edit, preserving pending intent and every apply receipt:
    # correcting pointers does not apply settings to an Odoo database.
    digest_input = {
        "profile": profile.model_dump(mode="json"),
        "override": existing_record.model_dump(mode="json") if existing_record else None,
        "keys": sorted(keys),
        "secrets": sorted(item.model_dump_json() for item in authority.metadata.records),
        "bindings": sorted(item.model_dump_json() for item in authority.metadata.bindings),
        "runtime": sorted(item.model_dump_json() for item in authority.runtime.records),
        "target": target.model_dump(mode="json") if target else None,
        "policy": policy.model_dump(mode="json") if policy else None,
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
        expected_dokploy_targets=(target,) if target else (),
        odoo_instance_override_writes=(
            OdooInstanceOverrideWrite(record=replacement, expected_record=existing_record),
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
    disagreeing = tuple(
        sorted(
            config_parameter_secret_env_key(item.key)
            for item in record.config_parameters
            if item.key in selected
            and item.value.secret_binding_id != selected[item.key].binding_id
        )
    )
    if disagreeing:
        raise ConfigParameterSecretReferenceError(
            "Config-parameter secret references disagree with lane delivery. "
            "Review the lane's odoo-secret-references/reconcile plan and correct its metadata first.",
            env_keys=disagreeing,
            disagrees=True,
        )
