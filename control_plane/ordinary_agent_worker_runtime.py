"""Legacy protocol compatibility for retained delegated-delivery records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequest,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentQualificationFiniteRequestV2,
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
        if not self.qualification_phase_supported or not self.guarded_phase_supported:
            raise OrdinaryAgentWorkerCompatibilityError(
                "Ordinary worker does not support every registered request phase."
            )
        if not self.compatible_alembic_revisions:
            raise OrdinaryAgentWorkerCompatibilityError(
                "Ordinary worker has no compatible Alembic revisions."
            )


DEFAULT_ORDINARY_AGENT_WORKER_SUPPORT = OrdinaryAgentWorkerSupportDescriptor()
