"""Testing-only local credential generation and opaque production overlap reads."""

import hmac
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_config_requirement_applies_to_lane,
)
from control_plane.product_secret_copy import ProductSecretCopyStore, require_product_lanes
from control_plane.product_config import ProductConfigStore
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    SecretDeliverySetExpectation,
)


class TestingCredentialStore(ProductConfigStore, ProductSecretCopyStore, Protocol):
    pass


def local_credential_keys(profile: LaunchplaneProductProfileRecord, context: str) -> frozenset[str]:
    lane = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
    )
    keys = {
        requirement.binding_key
        for requirement in profile.expected_config.managed_secret_bindings
        if requirement.testing_local_credential
        and product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    }
    if profile.driver_id == "odoo":
        from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS

        keys.update(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
    return frozenset(keys)


def generation_entries(
    store: TestingCredentialStore, profile: LaunchplaneProductProfileRecord, context: str
) -> list[dict[str, object]]:
    """Describe local credentials without reading or generating any value."""
    _require_unambiguous_lane(store, context, "testing")
    bindings = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    entries: list[dict[str, object]] = []
    for key in sorted(local_credential_keys(profile, context)):
        binding = bindings.get(key)
        record = store.read_secret_record(binding.secret_id) if binding else None
        # Shadow shared source authority with a new exact record; never rotate it.
        name = record.name if record and record.scope == "context_instance" else key
        entries.append(
            {
                "binding_key": key,
                "name": name,
                "generate": "testing_local",
                "secret_class": "testing",
            }
        )
    return entries


def _require_unambiguous_lane(store: TestingCredentialStore, context: str, instance: str) -> None:
    records = {
        record.secret_id: record
        for record in store.list_secret_records(
            integration="runtime_environment", context_name=context, limit=None
        )
        if record.status == "configured"
        and record.scope in {"context", "context_instance"}
        and record.context == context
        and record.instance in {"", instance}
    }
    candidates: dict[tuple[str, str], int] = {}
    for binding in store.list_secret_bindings(
        integration="runtime_environment", context_name=context, limit=None
    ):
        record = records.get(binding.secret_id)
        if (
            record is not None
            and binding.status == "configured"
            and (binding.context, binding.instance) == (record.context, record.instance)
        ):
            route = (binding.binding_key, record.scope)
            candidates[route] = candidates.get(route, 0) + 1
    if any(count > 1 for count in candidates.values()):
        raise ValueError("Runtime credential binding is ambiguous.")


def _lane_values(
    store: TestingCredentialStore, product: str, context: str, instance: str
) -> dict[str, str]:
    _require_unambiguous_lane(store, context, instance)
    selected = secrets.resolve_effective_secret_bindings_from_store(
        record_store=store,
        integration="runtime_environment",
        context_name=context,
        instance_name=instance,
        scopes=frozenset({"context", "context_instance"}),
    )
    values: dict[str, str] = {}
    for key, (_, record) in selected.items():
        version = store.read_secret_version(record.current_version_id)
        if version.secret_id != record.secret_id:
            raise ValueError("Runtime credential version is unavailable.")
        values[key] = secrets._decrypt_secret_value(version.ciphertext, version.key_id)
    return values


def validate_generation(
    store: TestingCredentialStore,
    *,
    product: str,
    context: str,
    instance: str,
    entries: tuple[dict[str, object], ...],
) -> LaunchplaneProductProfileRecord:
    profile = require_product_lanes(store, product=product, routes=((context, instance),))
    allowed = local_credential_keys(profile, context)
    expected = {
        str(item["binding_key"]): item for item in generation_entries(store, profile, context)
    }
    for entry in entries:
        key = str(entry["binding_key"])
        if key not in allowed or entry["name"] != expected[key]["name"]:
            raise ValueError(
                "Generation requires a declared testing-local credential and its lane binding."
            )
    return profile


def generation_bundle(
    store: TestingCredentialStore,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    *,
    actor: str,
    generation: str,
) -> ProductAuthorityBundle:
    """A lifecycle's own atomic bundle carries fresh values, never a side write."""
    from control_plane.product_config import (
        _plan_product_config_secret_write,
        _evaluate_product_config_runtime_key_safety,
        normalize_product_config_payload,
    )
    import secrets as random_secrets

    entries = generation_entries(store, profile, context)
    if not entries:
        return ProductAuthorityBundle()
    normalized = normalize_product_config_payload(
        {"product": profile.product, "context": context, "instance": "testing", "secrets": entries}
    )
    from typing import cast

    inputs = tuple(cast(list[dict[str, object]], normalized["secrets"]))
    _evaluate_product_config_runtime_key_safety(
        record_store=store, context_name=context, instance_name="testing", secrets=inputs
    )
    plans = [
        _plan_product_config_secret_write(
            record_store=store,
            scope="context_instance",
            integration="runtime_environment",
            name=str(entry["name"]),
            binding_key=str(entry["binding_key"]),
            plaintext_value=random_secrets.token_urlsafe(32),
            context_name=context,
            instance_name="testing",
            declared_secret_class="testing",
            value_source="generated",
            actor=actor,
            source_label="service:testing-local-credentials",
        )
        for entry in entries
    ]
    for plan in plans:
        for event in plan["secret_audit_events"]:
            event.metadata.update(value_source="service_generated", generation=generation)
    return ProductAuthorityBundle(
        secret_delivery_read_sets=(
            SecretDeliverySetExpectation(
                integration="runtime_environment",
                context=context,
                instance="testing",
                records=store.list_secret_records(
                    integration="runtime_environment", context_name=context, limit=None
                ),
                bindings=store.list_secret_bindings(
                    integration="runtime_environment", context_name=context, limit=None
                ),
            ),
        ),
        secret_records=tuple(record for plan in plans for record in plan["secret_records"]),
        secret_versions=tuple(version for plan in plans for version in plan["secret_versions"]),
        secret_bindings=tuple(binding for plan in plans for binding in plan["secret_bindings"]),
        secret_audit_events=tuple(event for plan in plans for event in plan["secret_audit_events"]),
    )


def with_generated_credentials(
    bundle: ProductAuthorityBundle,
    store: TestingCredentialStore,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    *,
    actor: str,
    generation: str,
) -> ProductAuthorityBundle:
    generated = generation_bundle(store, profile, context, actor=actor, generation=generation)
    keys = {binding.binding_key for binding in generated.secret_bindings}
    return bundle.model_copy(
        update={
            "secret_delivery_read_sets": (
                *bundle.secret_delivery_read_sets,
                *generated.secret_delivery_read_sets,
            ),
            "secret_records": (*bundle.secret_records, *generated.secret_records),
            "secret_versions": (*bundle.secret_versions, *generated.secret_versions),
            "secret_bindings": (
                *(
                    binding
                    for binding in bundle.secret_bindings
                    if not (
                        binding.integration == "runtime_environment"
                        and binding.context == context
                        and binding.instance == "testing"
                        and binding.binding_key in keys
                    )
                ),
                *generated.secret_bindings,
            ),
            "secret_audit_events": (*bundle.secret_audit_events, *generated.secret_audit_events),
        }
    )


class CredentialOverlap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    testing_key: str
    production_context: str
    production_key: str


class TestingCredentialAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    status: Literal["pass", "overlap", "unavailable", "no_production"]
    local_keys: list[str]
    local_binding_names: dict[str, str] = {}
    generated_keys: list[str] = []
    missing_keys: list[str] = []
    overlaps: list[CredentialOverlap] = []


class TestingCredentialAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    lanes: list[TestingCredentialAudit]


class TestingCredentialInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    products: list[str]
    lanes: list[TestingCredentialAudit]


def audit_testing_credentials(
    store: TestingCredentialStore, product: str
) -> list[TestingCredentialAudit]:
    profiles = store.list_product_profile_records()
    profile = next(item for item in profiles if item.product == product)
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    result: list[TestingCredentialAudit] = []
    for lane in profile.lanes:
        if lane.instance != "testing":
            continue
        local_keys = local_credential_keys(profile, lane.context)
        report = TestingCredentialAudit(
            product=product,
            context=lane.context,
            status="unavailable",
            local_keys=sorted(local_keys),
        )
        result.append(report)
        try:
            require_product_lanes(
                store,
                product=product,
                routes=((lane.context, "testing"), *((p.context, "prod") for p in production)),
            )
            bindings = site_delivered_secret_bindings_from_store(
                record_store=store, context_name=lane.context, instance_name="testing"
            )
            testing_values = _lane_values(store, product, lane.context, "testing")
            report.missing_keys = sorted(local_keys.difference(testing_values))
            report.generated_keys = sorted(
                key
                for key in local_keys
                if key in bindings and bindings[key].value_source == "generated"
            )
            report.local_binding_names = {
                str(entry["binding_key"]): str(entry["name"])
                for entry in generation_entries(store, profile, lane.context)
            }
            for source in production:
                source_values = _lane_values(store, product, source.context, "prod")
                report.missing_keys.extend(
                    f"prod:{key}" for key in sorted(local_keys.difference(source_values))
                )
                for key, value in testing_values.items():
                    for source_key, source_value in source_values.items():
                        if (
                            value
                            and source_value
                            and hmac.compare_digest(value.encode(), source_value.encode())
                        ):
                            report.overlaps.append(
                                CredentialOverlap(
                                    testing_key=key,
                                    production_context=source.context,
                                    production_key=source_key,
                                )
                            )
            report.status = (
                "overlap"
                if report.overlaps
                else "unavailable"
                if report.missing_keys
                else "pass"
                if production
                else "no_production"
            )
        except (ValueError, FileNotFoundError, RuntimeError, click.ClickException):
            report.status = "unavailable"
        # No values, ciphertext, fingerprints or private exception text leave this function.
    return result
