from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
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


class RuntimeEnvironmentDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record: RuntimeEnvironmentRecord
    event: RuntimeEnvironmentDeleteEvent


class RuntimeEnvironmentConflictError(ValueError):
    """Raised when runtime configuration changed after bundle planning."""


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
    dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    provider_target_writes: tuple[ProviderTargetWrite, ...] = ()
    runtime_environments: tuple[RuntimeEnvironmentRecord, ...] = ()
    runtime_environment_writes: tuple[RuntimeEnvironmentWrite, ...] = ()
    secret_records: tuple[SecretRecord, ...] = ()
    secret_versions: tuple[SecretVersion, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()
    secret_audit_events: tuple[SecretAuditEvent, ...] = ()
    environment_inventory: tuple[EnvironmentInventory, ...] = ()
    release_tuples: tuple[ReleaseTupleRecord, ...] = ()
    delete_runtime_environments: tuple[RuntimeEnvironmentDelete, ...] = ()
    delete_dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    delete_dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    delete_provider_targets: tuple[ProviderTargetRecord, ...] = ()
    idempotency_record: LaunchplaneIdempotencyRecord | None = None

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


class ProductAuthorityBundleStore(Protocol):
    def write_product_authority_bundle(self, bundle: ProductAuthorityBundle) -> None: ...
