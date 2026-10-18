"""Physical-only reservations are complete only with matching durable custody."""

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRecord
from control_plane.contracts.testing_candidate import TestingCandidateAllocation


def custody_backed_provider_reservation(
    store: object, physical: ProviderTargetRecord | None
) -> bool:
    if physical is None or (
        physical.provider_id != "dokploy"
        or physical.provider_target_type != "compose"
        or physical.target_category != "compose"
    ):
        return False
    if (physical.source_label, physical.instance) == (
        "service:testing-candidate",
        "testing-candidate",
    ):
        reader = getattr(store, "read_testing_candidate", None)
        record = reader(physical.context) if callable(reader) else None
        return (
            isinstance(record, TestingCandidateAllocation)
            and record.namespace == physical.context
            and record.compose_id == physical.target_id
        )
    if (physical.source_label, physical.instance) == (
        "service:odoo-upgrade-rehearsal",
        "rehearsal",
    ):
        reader = getattr(store, "read_odoo_upgrade_rehearsal", None)
        record = reader(physical.context) if callable(reader) else None
        return (
            isinstance(record, OdooUpgradeRehearsalRecord)
            and record.rehearsal_id == physical.context
            and record.compose_id == physical.target_id
        )
    return False
