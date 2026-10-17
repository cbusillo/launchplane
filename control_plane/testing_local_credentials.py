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
from control_plane.contracts.secret_record import SecretSharingKind
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
    only_keys: frozenset[str] | None = None,
) -> ProductAuthorityBundle:
    """A lifecycle's own atomic bundle carries fresh values, never a side write."""
    from control_plane.product_config import (
        _plan_product_config_secret_write,
        _evaluate_product_config_runtime_key_safety,
        normalize_product_config_payload,
    )
    import secrets as random_secrets

    entries = generation_entries(store, profile, context)
    if only_keys is not None:
        entries = [entry for entry in entries if entry["binding_key"] in only_keys]
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
    only_keys: frozenset[str] | None = None,
) -> ProductAuthorityBundle:
    generated = generation_bundle(
        store, profile, context, actor=actor, generation=generation, only_keys=only_keys
    )
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
    production_product: str
    # The testing binding's recorded sharing decision; set only for an external key.
    # Only a read-only source may stay on testing; other kinds are shown but still fail.
    sharing_kind: SecretSharingKind | None = None


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
    production_keys: dict[str, list[str]] = {}
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
    store: TestingCredentialStore,
    product: str,
    *,
    profile_snapshot: LaunchplaneProductProfileRecord | None = None,
    production_profiles: tuple[LaunchplaneProductProfileRecord, ...] | None = None,
) -> list[TestingCredentialAudit]:
    profiles = store.list_product_profile_records()
    profile = profile_snapshot or next(item for item in profiles if item.product == product)
    source_profiles = production_profiles if production_profiles is not None else (profile,)
    production = [
        (source_profile, lane)
        for source_profile in source_profiles
        for lane in source_profile.lanes
        if lane.instance == "prod"
    ]
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
            current = {item.product: item for item in store.list_product_profile_records()}
            if current.get(product) != profile or any(
                current.get(source.product) != source for source in source_profiles
            ):
                raise ValueError("Credential audit product ownership changed.")
            require_product_lanes(
                store,
                product=product,
                routes=((lane.context, "testing"),),
            )
            bindings = site_delivered_secret_bindings_from_store(
                record_store=store, context_name=lane.context, instance_name="testing"
            )
            # Compare what the lane receives; a retired provider key is never delivered.
            testing_values = {
                key: value
                for key, value in _lane_values(store, product, lane.context, "testing").items()
                if key in bindings
            }
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
            for source_profile, source in production:
                require_product_lanes(
                    store, product=source_profile.product, routes=((source.context, "prod"),)
                )
                source_values = _lane_values(store, product, source.context, "prod")
                report.production_keys[f"{source_profile.product}/{source.context}/prod"] = sorted(
                    source_values
                )
                for key, value in testing_values.items():
                    sharing = None if key in local_keys else bindings[key].sharing_reason
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
                                    production_product=source_profile.product,
                                    sharing_kind=sharing.kind if sharing else None,
                                )
                            )
            report.status = (
                "overlap"
                if any(overlap.sharing_kind != "read_only_source" for overlap in report.overlaps)
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


def prepare_rehearsal_local_credentials(
    store: TestingCredentialStore, rehearsal_id: str, *, actor: str
) -> None:
    """Allocate the existing local-password primitive to a rehearsal's own scope.

    This writes no lane binding, existing key, integration credential or grant.
    An incomplete or pre-existing non-generated set is retained for diagnosis.
    """
    import re
    import secrets as random_secrets
    from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
    from control_plane.product_config import _plan_product_config_secret_write

    if not re.fullmatch(r"lp-rehearsal-[a-f0-9]{32}", rehearsal_id):
        raise ValueError("Local rehearsal credentials require a service-owned identity.")
    bindings = store.list_secret_bindings(
        integration="runtime_environment", context_name=rehearsal_id, limit=None
    )
    records = store.list_secret_records(
        integration="runtime_environment", context_name=rehearsal_id, limit=None
    )
    if records and not bindings:
        raise ValueError("An incomplete rehearsal credential set retains its original custody.")
    if bindings:
        if (
            len(bindings) != len(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
            or {item.binding_key for item in bindings} != FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
            or any(
                item.value_source != "generated"
                or item.instance != "rehearsal"
                or item.status != "configured"
                for item in bindings
            )
        ):
            raise ValueError(
                "Existing rehearsal credentials are not an intact local generated set."
            )
        return
    plans = [
        _plan_product_config_secret_write(
            record_store=store,
            scope="context_instance",
            integration="runtime_environment",
            name=key.lower(),
            binding_key=key,
            plaintext_value=random_secrets.token_urlsafe(32),
            context_name=rehearsal_id,
            instance_name="rehearsal",
            declared_secret_class="testing",
            value_source="generated",
            actor=actor,
            source_label="service:odoo-upgrade-rehearsal",
        )
        for key in sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
    ]
    store.write_product_authority_bundle(
        ProductAuthorityBundle(
            secret_delivery_read_sets=(
                SecretDeliverySetExpectation(
                    integration="runtime_environment",
                    context=rehearsal_id,
                    instance="rehearsal",
                    records=store.list_secret_records(
                        integration="runtime_environment", context_name=rehearsal_id, limit=None
                    ),
                    bindings=bindings,
                ),
            ),
            secret_records=tuple(item for plan in plans for item in plan["secret_records"]),
            secret_versions=tuple(item for plan in plans for item in plan["secret_versions"]),
            secret_bindings=tuple(item for plan in plans for item in plan["secret_bindings"]),
            secret_audit_events=tuple(
                item for plan in plans for item in plan["secret_audit_events"]
            ),
        )
    )
