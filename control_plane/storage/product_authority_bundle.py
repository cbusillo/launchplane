from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
    product_target_owner_products,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)


class LaneProductConfigWriteRequirements(TypedDict, total=False):
    required_context_owner: tuple[str, str] | None
    required_product_config_target: tuple[str, str, str] | None


class RuntimeEnvironmentDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record: RuntimeEnvironmentRecord
    event: RuntimeEnvironmentDeleteEvent


class RuntimeEnvironmentConflictError(ValueError):
    """Raised when runtime configuration changed after bundle planning."""


class OdooInstanceOverrideConflictError(ValueError):
    """The reviewed override record changed before its reconciliation committed."""


def odoo_instance_override_records_match(
    current: OdooInstanceOverrideRecord | None,
    expected: OdooInstanceOverrideRecord | None,
) -> bool:
    """Preserve JSON scalar types when checking a reviewed override snapshot."""
    if current is None or expected is None:
        return current is expected
    return json.dumps(
        current.model_dump(mode="json", exclude_none=True), sort_keys=True
    ) == json.dumps(expected.model_dump(mode="json", exclude_none=True), sort_keys=True)


class OdooInstanceOverrideWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: OdooInstanceOverrideRecord
    expected_record: OdooInstanceOverrideRecord

    @model_validator(mode="after")
    def validate_route(self) -> "OdooInstanceOverrideWrite":
        if (self.record.context, self.record.instance) != (
            self.expected_record.context,
            self.expected_record.instance,
        ):
            raise ValueError("Override reconciliation must preserve its lane")
        return self


class ProductProfileConflictError(ValueError):
    """A product profile changed after it authorized a bundled write."""


class ProductRepositoryOwnershipError(ValueError):
    """A repository is already owned; repeating a dry run cannot resolve it."""


def require_product_profile_writes(
    records: Iterable[LaunchplaneProductProfileRecord],
    current_profiles: Iterable[LaunchplaneProductProfileRecord],
    *,
    absent_product_ids: tuple[str, ...] = (),
) -> None:
    """Check inert registration and identity under the existing authority lock."""
    profiles = {profile.product: profile for profile in current_profiles}
    if any(product in profiles for product in absent_product_ids):
        raise ProductProfileConflictError("Product was registered before commit.")
    for record in records:
        current = profiles.get(record.product)
        if current is not None and current.classification != record.classification:
            raise ProductProfileConflictError(
                "Product classification cannot change on an existing profile."
            )
        if record.is_active:
            for other in profiles.values():
                if other.product == record.product or not other.is_active:
                    continue
                if record.is_managed_runtime and other.is_managed_runtime:
                    continue
                same_repository = (
                    other.repository.strip().lower() == record.repository.strip().lower()
                )
                same_identity = (
                    bool(record.repository_id) and record.repository_id == other.repository_id
                )
                if same_repository or same_identity:
                    raise ProductRepositoryOwnershipError(
                        "Repository already belongs to another product."
                    )
        profiles[record.product] = record


class PublicHostAuthorityBusy(RuntimeError):
    """A conflicting authority write must be reviewed again after reconciliation."""


class SecretCopySourceConflictError(ValueError):
    """A reviewed copy source changed before the destination committed."""


class SecretRecordConflictError(ValueError):
    """A secret the bundle may only create was recorded before it committed."""


class SecretCopySourceExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: SecretRecord
    binding: SecretBinding


class SecretBindingSetExpectation(BaseModel):
    """Guard all consumers of a secret before disabling its record."""

    model_config = ConfigDict(extra="forbid")

    secret_id: str
    bindings: tuple[SecretBinding, ...]


class SecretDeliveryConflictError(ValueError):
    """The metadata selecting a lane's delivered secrets changed before commit."""


class SecretDeliverySetExpectation(BaseModel):
    """Guard lane selection, including insertions and disabled/absent candidates."""

    model_config = ConfigDict(extra="forbid")

    integration: str
    context: str
    instance: str
    records: tuple[SecretRecord, ...]
    bindings: tuple[SecretBinding, ...]

    def includes(self, item: SecretRecord | SecretBinding) -> bool:
        return (
            item.integration == self.integration
            and item.context == self.context
            and item.instance in {"", self.instance}
        )

    def matches(self, records: Iterable[SecretRecord], bindings: Iterable[SecretBinding]) -> bool:
        def payloads(items: Iterable[SecretRecord | SecretBinding]) -> list[str]:
            return sorted(item.model_dump_json() for item in items if self.includes(item))

        return payloads(records) == payloads(self.records) and payloads(bindings) == payloads(
            self.bindings
        )


class RuntimeEnvironmentWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: RuntimeEnvironmentRecord
    expected_record: RuntimeEnvironmentRecord | None = None
    expected_absent: bool = False

    @model_validator(mode="after")
    def validate_expectation(self) -> RuntimeEnvironmentWrite:
        if (self.expected_record is None) == (not self.expected_absent):
            raise ValueError(
                "Runtime environment writes require exactly one current-record expectation."
            )
        if self.expected_record is not None and (
            self.expected_record.scope != self.record.scope
            or self.expected_record.context != self.record.context
            or self.expected_record.instance != self.record.instance
        ):
            raise ValueError("Runtime environment write expectation must identify the same route.")
        return self


class RuntimeEnvironmentSetExpectation(BaseModel):
    """Read-only snapshot guard, including absent records in the selected layers."""

    model_config = ConfigDict(extra="forbid")

    contexts: tuple[str, ...]
    instances: tuple[tuple[str, str], ...] = ()
    include_global: bool = False
    records: tuple[RuntimeEnvironmentRecord, ...]

    def includes(self, record: RuntimeEnvironmentRecord) -> bool:
        return (
            (self.include_global and record.scope == "global")
            or (record.scope == "context" and record.context in self.contexts)
            or (record.scope == "instance" and (record.context, record.instance) in self.instances)
        )

    def matches(self, current: Iterable[RuntimeEnvironmentRecord]) -> bool:
        def payloads(records: Iterable[RuntimeEnvironmentRecord]) -> list[str]:
            return sorted(
                json.dumps(record.model_dump(mode="json", exclude_none=True), sort_keys=True)
                for record in records
                if self.includes(record)
            )

        return payloads(current) == payloads(self.records)


def runtime_environment_records_match(
    current: RuntimeEnvironmentRecord, expected: RuntimeEnvironmentRecord
) -> bool:
    """Compare all supported JSON scalars without Python's bool/int coercion."""
    return json.dumps(
        current.model_dump(mode="json", exclude_none=True), sort_keys=True
    ) == json.dumps(expected.model_dump(mode="json", exclude_none=True), sort_keys=True)


class ProviderTargetWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: ProviderTargetRecord
    expected_record: ProviderTargetRecord | None = None
    expected_absent: bool = False
    allowed_conflicting_routes: tuple[tuple[str, str], ...] = ()

    @model_validator(mode="after")
    def validate_expectation(self) -> ProviderTargetWrite:
        if (self.expected_record is None) == (not self.expected_absent):
            raise ValueError(
                "Provider target writes require exactly one current-record expectation."
            )
        if self.expected_record is not None and (
            self.expected_record.context != self.record.context
            or self.expected_record.instance != self.record.instance
        ):
            raise ValueError("Provider target write expectation must identify the same route.")
        requested_route = (self.record.context, self.record.instance)
        if requested_route in self.allowed_conflicting_routes:
            raise ValueError(
                "Provider target write cannot allow its requested route as a conflict."
            )
        self.allowed_conflicting_routes = tuple(dict.fromkeys(self.allowed_conflicting_routes))
        return self


class ProductAuthorityBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_profiles: tuple[LaunchplaneProductProfileRecord, ...] = ()
    expected_product_profiles: tuple[LaunchplaneProductProfileRecord, ...] = ()
    absent_product_ids: tuple[str, ...] = ()
    dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    provider_target_writes: tuple[ProviderTargetWrite, ...] = ()
    runtime_environments: tuple[RuntimeEnvironmentRecord, ...] = ()
    runtime_environment_writes: tuple[RuntimeEnvironmentWrite, ...] = ()
    runtime_environment_read_sets: tuple[RuntimeEnvironmentSetExpectation, ...] = ()
    odoo_instance_override_writes: tuple[OdooInstanceOverrideWrite, ...] = ()
    secret_records: tuple[SecretRecord, ...] = ()
    expected_secret_records: tuple[SecretRecord, ...] = ()
    secret_versions: tuple[SecretVersion, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()
    secret_audit_events: tuple[SecretAuditEvent, ...] = ()
    secret_copy_sources: tuple[SecretCopySourceExpectation, ...] = ()
    secret_binding_sets: tuple[SecretBindingSetExpectation, ...] = ()
    secret_delivery_read_sets: tuple[SecretDeliverySetExpectation, ...] = ()
    # Secret ids that must still be absent when the bundle commits: a secret
    # adopted from the provider is only ever created, never rotated.
    absent_secret_ids: tuple[str, ...] = ()
    environment_inventory: tuple[EnvironmentInventory, ...] = ()
    release_tuples: tuple[ReleaseTupleRecord, ...] = ()
    delete_runtime_environments: tuple[RuntimeEnvironmentDelete, ...] = ()
    delete_dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    delete_dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    delete_provider_targets: tuple[ProviderTargetRecord, ...] = ()
    idempotency_record: LaunchplaneIdempotencyRecord | None = None
    # (product, context) that must still own the context exclusively when the
    # bundle commits; checked under the same lock as product-profile writes.
    required_context_owner: tuple[str, str] | None = None
    required_context_owners: tuple[tuple[str, str], ...] = ()
    # (product, context, instance); an empty instance checks the whole context.
    # This also covers authorized admins configuring Launchplane itself.
    required_product_config_target: tuple[str, str, str] | None = None

    @model_validator(mode="after")
    def validate_runtime_environment_routes(self) -> ProductAuthorityBundle:
        guarded_routes = [
            (write.record.scope, write.record.context, write.record.instance)
            for write in self.runtime_environment_writes
        ]
        if len(guarded_routes) != len(set(guarded_routes)):
            raise ValueError("Runtime environment bundle contains duplicate guarded routes.")
        guarded_route_set = set(guarded_routes)
        legacy_routes = {
            (record.scope, record.context, record.instance) for record in self.runtime_environments
        }
        delete_routes = {
            (
                item.expected_record.scope,
                item.expected_record.context,
                item.expected_record.instance,
            )
            for item in self.delete_runtime_environments
        }
        if (
            guarded_route_set.intersection(legacy_routes)
            or guarded_route_set.intersection(delete_routes)
            or legacy_routes.intersection(delete_routes)
        ):
            raise ValueError("Runtime environment bundle contains overlapping routes.")
        return self

    def requires_write(self) -> bool:
        return any(
            (
                self.product_profiles,
                self.dokploy_targets,
                self.dokploy_target_ids,
                self.provider_target_writes,
                self.runtime_environments,
                self.runtime_environment_writes,
                self.odoo_instance_override_writes,
                self.secret_records,
                self.secret_versions,
                self.secret_bindings,
                self.secret_audit_events,
                self.environment_inventory,
                self.release_tuples,
                self.delete_runtime_environments,
                self.delete_dokploy_targets,
                self.delete_dokploy_target_ids,
                self.delete_provider_targets,
                self.idempotency_record is not None,
            )
        )


class ProductContextOwnershipError(PermissionError):
    """The bundle's context no longer belongs to its product alone."""


def require_bundle_context_owner(
    bundle: ProductAuthorityBundle,
    profiles: Iterable[LaunchplaneProductProfileRecord],
) -> None:
    required_owners = bundle.required_context_owners + (
        (bundle.required_context_owner,) if bundle.required_context_owner is not None else ()
    )
    if not required_owners and bundle.required_product_config_target is None:
        return
    profiles = tuple(profiles)
    owners = product_context_owner_map(profiles)
    for product, context in required_owners:
        if not is_exclusive_product_context(context=context, product=product, owners=owners):
            raise ProductContextOwnershipError("The context must belong to the named product only.")
    if bundle.required_product_config_target is not None:
        product, context, instance = bundle.required_product_config_target
        if product_target_owner_products(profiles, context=context, instance=instance) != frozenset(
            (product,)
        ):
            # Service-only preview identities live in the product's preview
            # context, individually named for their exact runtime instance.
            # This does not authorize runtime configuration or another purpose.
            from control_plane.odoo_diagnostic_identity import (
                DIAGNOSTIC_SECRET_INTEGRATION,
                DIAGNOSTIC_IDENTITY_KEY,
                diagnostic_preview_context_owned,
                diagnostic_preview_secret_name,
            )

            other_writes = bundle.model_copy(
                update={
                    "secret_records": (),
                    "secret_versions": (),
                    "secret_bindings": (),
                    "secret_audit_events": (),
                    "idempotency_record": None,
                }
            )
            if (
                instance
                or other_writes.requires_write()
                or not diagnostic_preview_context_owned(profiles, product, context)
                or any(
                    record.scope != "context"
                    or record.context != context
                    or record.instance
                    or record.integration != DIAGNOSTIC_SECRET_INTEGRATION
                    or not record.name.startswith(diagnostic_preview_secret_name(""))
                    for record in bundle.secret_records
                )
                or any(
                    binding.integration != DIAGNOSTIC_SECRET_INTEGRATION
                    or binding.binding_key != DIAGNOSTIC_IDENTITY_KEY
                    or binding.context != context
                    or binding.instance
                    or binding.secret_id
                    not in {record.secret_id for record in bundle.secret_records}
                    for binding in bundle.secret_bindings
                )
                or any(
                    version.secret_id not in {record.secret_id for record in bundle.secret_records}
                    for version in bundle.secret_versions
                )
            ):
                raise ProductProfileConflictError(
                    "Product config target ownership changed before commit."
                )


class ProductAuthorityBundleStore(Protocol):
    def write_product_authority_bundle(self, bundle: ProductAuthorityBundle) -> None: ...
