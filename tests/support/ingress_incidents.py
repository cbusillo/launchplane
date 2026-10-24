from control_plane.contracts.public_ingress_monitoring import _check_record_token, _record_token

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressObservationRecord,
)
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressMonitorStore,
    _plan_public_ingress_incident_transition,
)


def reconcile_public_ingress_incident(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    incident_eligible: bool = True,
    write_records: bool = True,
) -> tuple[PublicIngressIncidentRecord, ...]:
    plan = _plan_public_ingress_incident_transition(
        record_store=record_store,
        record=record,
        incident_eligible=incident_eligible,
        recovery_observation_threshold=1,
    )
    if write_records:
        for incident in plan.incidents:
            record_store.write_public_ingress_incident_record(incident)
        for incident_event in plan.incident_events:
            record_store.write_public_ingress_incident_event_record(incident_event)
        for reminder_state in plan.reminder_states:
            record_store.write_public_ingress_incident_reminder_state_record(reminder_state)
    return plan.incidents


def build_public_ingress_lane_incident_id(
    *, product: str, context: str, instance: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-incident", product, context, instance, check_token)
        if _record_token(value)
    )
