"""Composition and supervision helpers for the dormant ordinary worker.

This module owns process wiring only.  Admission, readiness, custody, and
provider checks remain in their domain services and storage transactions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentJobWorkerStore,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequest,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.ordinary_agent_job_worker import (
    OrdinaryAgentJobScanResult,
    OrdinaryAgentJobScanState,
    run_ordinary_agent_job_once,
)
from control_plane.storage.schema_invariants import RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS


class OrdinaryAgentWorkerCompatibilityError(RuntimeError):
    """The process cannot safely interpret the installed ordinary records."""


@dataclass(frozen=True, slots=True)
class OrdinaryAgentWorkerSupportDescriptor:
    """Code-owned compatibility data captured at the worker composition root."""

    finite_request_protocols: frozenset[str] = frozenset(
        {"ordinary-agent-finite-v1", "ordinary-agent-finite-v2"}
    )
    read_protocols: frozenset[str] = frozenset({"ordinary-agent-read-v1", "ordinary-agent-read-v2"})
    effect_protocols: frozenset[str] = frozenset({"ordinary-agent-effect-v1"})
    finite_request_versions: tuple[int, ...] = (1, 2)
    read_attempt_versions: tuple[int, ...] = (1, 2)
    effect_versions: tuple[int, ...] = (1,)
    compatible_alembic_revisions: tuple[str, ...] = RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS

    def supports_request(self, request: OrdinaryAgentFiniteRequest) -> bool:
        return (
            request.schema_version in self.finite_request_versions
            and f"ordinary-agent-finite-v{request.schema_version}" in self.finite_request_protocols
        )

    @property
    def qualification_phase_supported(self) -> bool:
        return (
            2 in self.finite_request_versions
            and "ordinary-agent-finite-v2" in self.finite_request_protocols
            and 2 in self.read_attempt_versions
            and "ordinary-agent-read-v2" in self.read_protocols
        )

    @property
    def guarded_phase_supported(self) -> bool:
        return (
            {1, 2}.issubset(self.finite_request_versions)
            and {
                "ordinary-agent-finite-v1",
                "ordinary-agent-finite-v2",
            }.issubset(self.finite_request_protocols)
            and 1 in self.read_attempt_versions
            and "ordinary-agent-read-v1" in self.read_protocols
            and 1 in self.effect_versions
            and "ordinary-agent-effect-v1" in self.effect_protocols
        )

    def supports_phase(
        self,
        *,
        request: OrdinaryAgentFiniteRequest,
        purpose: Literal["qualification", "guarded_delivery"],
    ) -> bool:
        """Match one persisted request to every protocol used by its phase."""
        if not self.supports_request(request):
            return False
        if purpose == "qualification":
            return self.qualification_phase_supported and isinstance(
                request, OrdinaryAgentQualificationFiniteRequestV2
            )
        return self.guarded_phase_supported and isinstance(
            request,
            (OrdinaryAgentFiniteRequestRecord, OrdinaryAgentGuardedDeliveryFiniteRequestV2),
        )

    def validate(self) -> None:
        if not self.finite_request_protocols or not self.finite_request_versions:
            raise OrdinaryAgentWorkerCompatibilityError(
                "Ordinary worker has no supported finite-request versions."
            )
        if (
            not self.read_protocols
            or not self.read_attempt_versions
            or not self.effect_protocols
            or not self.effect_versions
        ):
            raise OrdinaryAgentWorkerCompatibilityError(
                "Ordinary worker has incomplete read/effect support."
            )
        if not self.compatible_alembic_revisions:
            raise OrdinaryAgentWorkerCompatibilityError(
                "Ordinary worker has no compatible Alembic revisions."
            )


DEFAULT_ORDINARY_AGENT_WORKER_SUPPORT = OrdinaryAgentWorkerSupportDescriptor()


@dataclass
class OrdinaryAgentWorkerTelemetry:
    """Process-local telemetry; it is deliberately separate from job records."""

    polls: int = 0
    processed: int = 0
    claim_failures: int = 0
    advance_failures: int = 0
    empty_polls: int = 0
    last_status: str | None = None
    last_failure_phase: Literal["claim", "advance_or_finish"] | None = None
    last_reason_code: str | None = None

    def record(
        self, result: OrdinaryAgentJobScanResult, *, claim_rejection_reason: str | None = None
    ) -> None:
        self.polls += 1
        self.processed += result.processed
        self.last_status = result.status
        self.last_failure_phase = result.failure_phase
        self.last_reason_code = (
            claim_rejection_reason
            if result.failure_phase == "claim"
            else "advance_or_finish_failed"
            if result.failure_phase == "advance_or_finish"
            else result.status
        )
        if result.failure_phase == "claim":
            self.claim_failures += 1
        elif result.failure_phase == "advance_or_finish":
            self.advance_failures += 1
        elif result.processed == 0:
            self.empty_polls += 1

    def payload(self) -> dict[str, object]:
        return asdict(self)


class OrdinaryAgentJobAdvancer(Protocol):
    def __call__(self, claimed: OrdinaryAgentClaimedJob) -> OrdinaryAgentJobAttemptDisposition: ...


def run_ordinary_agent_worker_once(
    *,
    record_store: OrdinaryAgentJobWorkerStore,
    state: OrdinaryAgentJobScanState,
    telemetry: OrdinaryAgentWorkerTelemetry,
    worker_id: str,
    lease_seconds: int,
    dispatcher: OrdinaryAgentJobAdvancer,
) -> OrdinaryAgentJobScanResult:
    """Run one ordinary fair-step and record only process-local telemetry."""

    result = run_ordinary_agent_job_once(
        record_store=record_store,
        state=state,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        advance_job=dispatcher,
    )
    telemetry.record(result, claim_rejection_reason=state.last_claim_rejection_reason)
    return result


def ordinary_agent_worker_result_payload(
    result: OrdinaryAgentJobScanResult, telemetry: OrdinaryAgentWorkerTelemetry
) -> dict[str, object]:
    return {"result": asdict(result), "telemetry": telemetry.payload()}
