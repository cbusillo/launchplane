"""Legacy cancellation and denial types retained by maintenance and native train fences."""

from __future__ import annotations


from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequest,
    parse_ordinary_agent_finite_request,
)


class OrdinaryAgentSessionAdmissionDenied(ValueError):
    def __init__(
        self,
        reason_code: str,
        *,
        retry_not_before: int | None = None,
        server_observed_at: int | None = None,
    ) -> None:
        self.retry_not_before = retry_not_before
        self.server_observed_at = server_observed_at
        self.reason_code = reason_code
        super().__init__(reason_code)


def cancel_ordinary_agent_finite_request(
    *, request: OrdinaryAgentFiniteRequest, now: int
) -> OrdinaryAgentFiniteRequest:
    """Keep unknown-effect reconciliation durable when cancellation is requested."""
    if (
        request.status in ("cancelled", "completed")
        or request.cancellation_requested_at is not None
    ):
        return request
    return parse_ordinary_agent_finite_request(
        {
            **request.model_dump(),
            "cancellation_requested_at": now,
            "status": "reconciliation_required"
            if request.status == "reconciliation_required"
            else "cancelled",
        }
    )
