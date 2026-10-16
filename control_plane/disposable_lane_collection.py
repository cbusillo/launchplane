"""Resolve service-owned facts without treating absent records as positive safety."""

from dataclasses import dataclass

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.disposable_lanes import DisposableLaneStore
from control_plane.odoo_testing_usage import OdooTestingUsage


@dataclass(frozen=True)
class CollectedDisposableLaneFacts:
    usage: str = "unknown"
    data: str = "unknown"
    production_resources: str = "unknown"
    usage_evidence: str = ""
    data_evidence: str = ""
    isolation_evidence: str = ""

    def payload(self) -> dict[str, str]:
        return {
            "usage": self.usage,
            "data": self.data,
            "production_resources": self.production_resources,
            "usage_evidence": self.usage_evidence,
            "data_evidence": self.data_evidence,
            "isolation_evidence": self.isolation_evidence,
        }


def collect_disposable_lane_facts(
    store: DisposableLaneStore,
    *,
    context: str,
    instance: str,
    target: DokployTargetRecord | None,
    target_id: DokployTargetIdRecord | None,
    binding_sha256: str,
    odoo_usage: OdooTestingUsage | None = None,
) -> CollectedDisposableLaneFacts:
    """Combine service occupancy reads with protective hold and alias records."""
    held = (
        target is not None
        and target.policies.staff_testing_hold is not None
        and target.policies.staff_testing_hold.purpose == "staff_testing"
    )
    shared_sources = []
    if target is not None and target_id is not None:
        for other in store.list_dokploy_target_id_records():
            if (
                other.instance != "prod"
                or (other.context, other.instance) == (context, instance)
                or other.target_id != target_id.target_id
            ):
                continue
            try:
                other_target = store.read_dokploy_target_record(
                    context_name=other.context, instance_name=other.instance
                )
            except FileNotFoundError:
                continue
            if other_target.target_type == target.target_type:
                shared_sources.append(
                    (other.model_dump(mode="json"), other_target.model_dump(mode="json"))
                )
    usage = odoo_usage or OdooTestingUsage()
    return CollectedDisposableLaneFacts(
        usage="in_use" if held else usage.usage,
        usage_evidence=(
            f"service:staff-testing-hold:{binding_sha256}"
            if held
            else f"service:odoo-testing-usage:{binding_sha256}"
            if usage.usage != "unknown"
            else ""
        ),
        production_resources="shared" if shared_sources else "unknown",
        isolation_evidence=(
            f"service:production-target-alias:{binding_sha256}:{canonical_sha256(shared_sources)}"
            if shared_sources
            else ""
        ),
    )


def disposable_collection_coverage() -> dict[str, str]:
    return {
        "usage": "staff_testing_hold; odoo_testing_recent_logins_and_authenticated_session_files",
        "data": "authoritative_custody_inventory_unavailable",
        "production_resources": "same_kind_production_target_alias_only; physical_isolation_unavailable",
        "historical_generations": "not_current_executor_evidence",
    }
