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
        return request.schema_version in self.finite_request_versions

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

    def record(self, result: OrdinaryAgentJobScanResult) -> None:
        self.polls += 1
        self.processed += result.processed
        self.last_status = result.status
        self.last_failure_phase = result.failure_phase
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
    telemetry.record(result)
    return result


def ordinary_agent_worker_result_payload(
    result: OrdinaryAgentJobScanResult, telemetry: OrdinaryAgentWorkerTelemetry
) -> dict[str, object]:
    return {"result": asdict(result), "telemetry": telemetry.payload()}
