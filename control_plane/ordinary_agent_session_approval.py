"""Authenticated application boundary for ordinary session approval.

The configured manager belongs to the service, never to request input. No route
is registered here. Browser approvals use signed human sessions and stored
ordinary proposals; they never accept an ordinary bearer or an asserted admin ID.
"""

from __future__ import annotations

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentApprovedEnrollmentIntent,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionOperationView,
)
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
    OrdinaryAgentSessionWriteSet,
)
from control_plane.service_human_auth import HumanSessionManager, LaunchplaneHumanSession
from control_plane.storage.postgres import PostgresRecordStore


def _authenticated_approver(
    *, manager: HumanSessionManager, cookie_header: str, csrf_token: str
) -> LaunchplaneHumanSession:
    human = manager.read_cookie_without_renewal(cookie_header)
    if (
        human is None
        or not manager.authorization_claims_are_current(human)
        or not manager.csrf_token_is_valid(human, csrf_token)
    ):
        raise OrdinaryAgentSessionAdmissionDenied("administrator_authentication_failed")
    return human


def approve_existing_ordinary_agent_session(
    *,
    store: PostgresRecordStore,
    manager: HumanSessionManager,
    cookie_header: str,
    csrf_token: str,
    principal_id: str,
    operation_id: str,
) -> OrdinaryAgentSessionWriteSet:
    human = _authenticated_approver(
        manager=manager, cookie_header=cookie_header, csrf_token=csrf_token
    )
    return store._approve_existing_ordinary_agent_session(
        human=human, principal_id=principal_id, operation_id=operation_id
    )


def approve_ordinary_agent_enrollment(
    *,
    store: PostgresRecordStore,
    manager: HumanSessionManager,
    cookie_header: str,
    csrf_token: str,
    principal_id: str,
    operation_id: str,
) -> OrdinaryAgentApprovedEnrollmentIntent:
    """Approve the exact stored initial intent; optional session remains optional."""
    human = _authenticated_approver(
        manager=manager, cookie_header=cookie_header, csrf_token=csrf_token
    )
    intent = store._read_ordinary_agent_enrollment_proposal(
        principal_id=principal_id, operation_id=operation_id
    )
    return store._approve_initial_ordinary_agent_session(human=human, intent=intent)


def read_human_ordinary_agent_session_operation(
    *,
    store: PostgresRecordStore,
    manager: HumanSessionManager,
    cookie_header: str,
    principal_id: str,
    operation_id: str,
) -> OrdinaryAgentSessionOperationView:
    human = manager.read_cookie_without_renewal(cookie_header)
    if human is None or not manager.authorization_claims_are_current(human):
        raise OrdinaryAgentSessionAdmissionDenied("administrator_authentication_failed")
    return store._read_human_ordinary_agent_session_operation(
        human=human, principal_id=principal_id, operation_id=operation_id
    )
