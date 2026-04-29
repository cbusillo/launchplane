from pydantic import BaseModel, ConfigDict

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding


class LaunchplaneLaneSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    inventory: EnvironmentInventory | None = None
    release_tuple: ReleaseTupleRecord | None = None
    latest_deployment: DeploymentRecord | None = None
    latest_promotion: PromotionRecord | None = None
    latest_backup_gate: BackupGateRecord | None = None
    dokploy_target_id: DokployTargetIdRecord | None = None
    dokploy_target: DokployTargetRecord | None = None
    runtime_environment_records: tuple[RuntimeEnvironmentRecord, ...] = ()
    odoo_instance_override: OdooInstanceOverrideRecord | None = None
    secret_bindings: tuple[SecretBinding, ...] = ()
    provenance: DataProvenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded lane evidence for this context and instance.",
    )
