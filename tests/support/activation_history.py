from __future__ import annotations

from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.storage.postgres import PostgresRecordStore


def seed_activation_history(
    store: PostgresRecordStore,
    record: OrdinaryAgentDeliveryActivationRecord,
    event: OrdinaryAgentDeliveryActivationEvent,
) -> None:
    with store._session_factory() as session:
        session.merge(PostgresRecordStore._ordinary_agent_delivery_activation_row(record))
        session.add(PostgresRecordStore._ordinary_agent_delivery_activation_event_row(event))
        session.commit()
