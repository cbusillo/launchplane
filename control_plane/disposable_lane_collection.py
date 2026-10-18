"""Resolve service-owned facts without treating absent records as positive safety."""

from dataclasses import dataclass
from datetime import datetime

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.disposable_lanes import DisposableLaneStore
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.contracts.disposable_lane import DisposableLaneSafety


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
            else f"service:odoo-testing-never-served:{usage.proof_sha256}"
            if usage.usage == "unused" and usage.reason_code == "never_served"
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
        "usage": "staff_testing_hold; odoo_testing_recent_logins_and_authenticated_session_files; durable_never_served_generation",
        "data": "qualified_resettable_odoo_generation; other_custody_unavailable",
        "production_resources": "qualified_odoo_receiver_inspection; same_kind_production_target_alias",
        "historical_generations": "not_current_executor_evidence",
    }


def collect_testing_resource_facts(
    store: object, *, product: str, context: str, now: datetime
) -> dict[str, str] | None:
    """Reinspect a service-created resettable copy without requiring login history."""
    from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
    from control_plane.lane_usage import lane_usage_protected
    from control_plane.odoo_testing_restore import (
        read_current_testing_generation,
        generation_binding,
    )
    from control_plane.odoo_testing_drill_usage import read_drill_recent_signins
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore):
        return None
    try:
        profile = store.read_product_profile_record(product)
        lane = next(
            lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
        )
        if (
            profile.driver_id != "odoo"
            or lane.odoo_data_policy.data_authority != "resettable"
            or lane_usage_protected(
                store,
                product=product,
                context=context,
                instance="testing",
                usage="unknown",
                now=now,
            )
        ):
            return None
        generation = read_current_testing_generation(
            store, product=product, context=context, instance="testing"
        )
        if generation is None or not generation.completed_at:
            return None
        if (
            read_drill_recent_signins(store, generation, now.timestamp(), usage_required=False)
            is not None
        ):
            return {}
        reference = "service:testing-resource-inspection:" + generation_binding(generation)
        return {
            "data": "reproducible",
            "data_evidence": reference,
            "production_resources": "isolated",
            "isolation_evidence": reference,
        }
    except NativeSourceAuthorizationDenied:
        raise
    except Exception:
        # A failed physical/copy inspection never becomes an isolation fact.
        return {}


def merge_collected_safety(
    previous: DisposableLaneSafety,
    collected: DisposableLaneSafety,
    *,
    replace_resource_facts: bool = False,
) -> DisposableLaneSafety:
    """Keep protection and independent fresh proofs; collection never renews them."""
    changes: dict[str, object] = {}
    if previous.usage == "in_use" and collected.usage == "unknown":
        changes.update(usage="in_use", usage_evidence=previous.usage_evidence)
    if previous.data == "only_copy":
        changes.update(data="only_copy", data_evidence=previous.data_evidence)
    if previous.production_resources in {"shared", "separation_ready"}:
        changes.update(
            production_resources=previous.production_resources,
            isolation_evidence=previous.isolation_evidence,
        )
    if (
        not replace_resource_facts
        and previous.binding_sha256 == collected.binding_sha256
        and previous.observed_at <= collected.observed_at < previous.expires_at
    ):
        inherited = False
        if collected.data == "unknown" and previous.data == "reproducible":
            changes.update(data=previous.data, data_evidence=previous.data_evidence)
            inherited = True
        if collected.production_resources == "unknown" and previous.production_resources in {
            "isolated",
            "separation_ready",
        }:
            changes.update(
                production_resources=previous.production_resources,
                isolation_evidence=previous.isolation_evidence,
            )
            inherited = True
        if inherited:
            changes["expires_at"] = min(previous.expires_at, collected.expires_at)
    return DisposableLaneSafety.model_validate({**collected.model_dump(), **changes})
