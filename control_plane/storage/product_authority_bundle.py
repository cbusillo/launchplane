from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

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


class ProductAuthorityBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_profiles: tuple[LaunchplaneProductProfileRecord, ...] = ()
    dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    provider_targets: tuple[ProviderTargetRecord, ...] = ()
    runtime_environments: tuple[RuntimeEnvironmentRecord, ...] = ()
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

    def requires_write(self) -> bool:
        return any(
            (
                self.product_profiles,
                self.dokploy_targets,
                self.dokploy_target_ids,
                self.provider_targets,
                self.runtime_environments,
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
