from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.change_impact import ChangeImpactTarget
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
)
from control_plane.contracts.merge_readiness import (
    MergeReadinessAdvisoryObservation,
    MergeReadinessResult,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceDecision,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceHumanActionSemantics,
    owner_acceptance_human_action_semantics,
)


GovernanceReadinessAvailability = Literal["available", "not_active", "unavailable"]
GovernanceReadinessReason = Literal[
    "current_evaluation_available",
    "no_active_merge_lineage",
    "current_evidence_unavailable",
]
GovernanceAdvisoryObservationScope = Literal["current_readiness", "admission_readiness"]


class GovernanceOwnerHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: OwnerAcceptanceEventRecord
    human_action_semantics: OwnerAcceptanceHumanActionSemantics
    authorizes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_entry(self) -> "GovernanceOwnerHistoryEntry":
        if self.authorizes:
            raise ValueError("Historical Owner product review authorizes no effect")
        expected_semantics = owner_acceptance_human_action_semantics(self.record.action)
        if self.human_action_semantics != expected_semantics:
            raise ValueError("Owner history semantics must match the stored event")
        return self


class GovernanceOwnerJudgmentFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal[1] = 1
    mode: Literal["historical_product_judgment"] = "historical_product_judgment"
    authoritative: Literal[False] = False
    authorizes: tuple[str, ...] = ()
    current: OwnerAcceptanceDecision
    history: tuple[GovernanceOwnerHistoryEntry, ...] = ()

    @model_validator(mode="after")
    def _validate_facet(self) -> "GovernanceOwnerJudgmentFacet":
        if self.authorizes:
            raise ValueError("Level 1 Owner product judgment authorizes no effect")
        ordered = tuple(
            sorted(
                self.history,
                key=lambda item: (
                    item.record.binding.product,
                    item.record.binding.system,
                    item.record.binding.action,
                    item.record.binding.environment,
                    item.record.subject_sequence,
                    item.record.event_id,
                ),
            )
        )
        event_ids = tuple(item.record.event_id for item in ordered)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Governance Owner history event IDs must be unique")
        object.__setattr__(self, "history", ordered)
        return self


class GovernanceMergeReadinessFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal[2] = 2
    mode: Literal["ephemeral"] = "ephemeral"
    authoritative: Literal[False] = False
    authorizes: tuple[str, ...] = ()
    availability: GovernanceReadinessAvailability
    reason_code: GovernanceReadinessReason
    result: MergeReadinessResult | None = None

    @model_validator(mode="after")
    def _validate_facet(self) -> "GovernanceMergeReadinessFacet":
        if self.authorizes:
            raise ValueError("Level 2 merge readiness authorizes no effect")
        if self.availability == "available":
            if self.reason_code != "current_evaluation_available" or self.result is None:
                raise ValueError("Available Level 2 readiness requires a current result")
        elif self.result is not None:
            raise ValueError("Unavailable Level 2 readiness cannot carry a result")
        return self


class GovernanceMergeAdmissionFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal[3] = 3
    mode: Literal["immutable_attempt_authorization"] = "immutable_attempt_authorization"
    admitted: bool
    authorizes: tuple[Literal["one_exact_merge_attempt"], ...] = ()
    record: MergeAdmissionRecord | None = None

    @model_validator(mode="after")
    def _validate_facet(self) -> "GovernanceMergeAdmissionFacet":
        expected_authorizes: tuple[Literal["one_exact_merge_attempt"], ...] = (
            ("one_exact_merge_attempt",) if self.record is not None else ()
        )
        if self.admitted != (self.record is not None) or self.authorizes != expected_authorizes:
            raise ValueError("Level 3 admission semantics must match the immutable record")
        return self


class GovernanceLandingOutcomeFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["immutable_provider_observation"] = "immutable_provider_observation"
    status: Literal["not_observed", "landed", "rejected", "reconcile_required"]
    landed: bool
    record: MergeLandingOutcomeRecord | None = None

    @model_validator(mode="after")
    def _validate_facet(self) -> "GovernanceLandingOutcomeFacet":
        expected_status = self.record.status if self.record is not None else "not_observed"
        if self.status != expected_status or self.landed != (expected_status == "landed"):
            raise ValueError("Landing outcome semantics must match the immutable record")
        return self


class GovernanceAdvisoryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_scope: GovernanceAdvisoryObservationScope
    observation: MergeReadinessAdvisoryObservation
    neutral: Literal[True] = True
    authoritative: Literal[False] = False
    authorizes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_observation(self) -> "GovernanceAdvisoryObservation":
        if self.authorizes:
            raise ValueError("Advisory governance observations authorize no effect")
        return self


class GovernanceProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["read_only_projection"] = "read_only_projection"
    authoritative: Literal[False] = False
    authorizes: tuple[str, ...] = ()
    target: ChangeImpactTarget
    owner_judgment: GovernanceOwnerJudgmentFacet
    merge_readiness: GovernanceMergeReadinessFacet
    merge_admission: GovernanceMergeAdmissionFacet
    landing_outcome: GovernanceLandingOutcomeFacet
    advisory_observations: tuple[GovernanceAdvisoryObservation, ...] = ()
    generated_at: str

    @model_validator(mode="after")
    def _validate_projection(self) -> "GovernanceProjection":
        if self.authorizes:
            raise ValueError("The governance read model authorizes no effect")
        object.__setattr__(
            self,
            "advisory_observations",
            tuple(
                sorted(
                    self.advisory_observations,
                    key=lambda item: (
                        item.observation_scope,
                        item.observation.name.casefold(),
                        item.observation.app_id or 0,
                        item.observation.state,
                    ),
                )
            ),
        )
        return self


class GovernanceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    projection: GovernanceProjection
