"""Resolve service-owned facts without treating absent records as positive safety."""

from dataclasses import dataclass

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.disposable_lanes import DisposableLaneStore


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
) -> CollectedDisposableLaneFacts:
    """Only positive holds and an exact physical target alias are authoritative.

    Neither source establishes the absence of people, reproducible data, or
    independent writable mounts, ports, databases and live credentials. In
    particular, historical generation states are not executor observations.
    This first collector deliberately cannot authorize a disposable apply.
    """
    held = target is not None and target.policies.staff_testing_hold is not None
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
    return CollectedDisposableLaneFacts(
        usage="in_use" if held else "unknown",
        usage_evidence=f"service:staff-testing-hold:{binding_sha256}" if held else "",
        production_resources="shared" if shared_sources else "unknown",
        isolation_evidence=(
            f"service:production-target-alias:{binding_sha256}:{canonical_sha256(shared_sources)}"
            if shared_sources
            else ""
        ),
    )


def disposable_collection_coverage() -> dict[str, str]:
    return {
        "usage": "positive_staff_testing_hold_only; complete_occupancy_unavailable",
        "data": "authoritative_custody_inventory_unavailable",
        "production_resources": "same_kind_production_target_alias_only; physical_isolation_unavailable",
        "historical_generations": "not_current_executor_evidence",
    }
