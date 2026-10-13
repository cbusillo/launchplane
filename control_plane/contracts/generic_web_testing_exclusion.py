"""Observation only: this contract cannot authorize staging or recovery."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp

GENERIC_WEB_TESTING_EXCLUSION_ROUTE = "/v1/generic-web/testing/exclusion"

ExclusionCode = Literal[
    "proved",
    "not_inspected",
    "hold_absent",
    "execution_lock_busy",
    "reservation_fence_absent",
    "execution_ownership_unproved",
    "binding_changed",
    "provider_read_unavailable",
    "provider_inventory_unproved",
    "provider_effect_active",
    "provider_target_unproved",
    "execution_lock_lost",
]


class TestingExclusionFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: Literal["excluded", "quiescent", "held", "unknown"] = "unknown"
    code: ExclusionCode = "not_inspected"
    complete: bool = False
    count: int | None = Field(default=None, ge=0)


class GenericWebTestingExclusionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    trace_id: str = Field(pattern=r"^launchplane_req_[a-zA-Z0-9_-]+$")
    product: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    context: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    instance: Literal["testing"] = "testing"
    observed_at: str = Field(pattern=r"^[0-9T:.+Z-]{20,40}$")
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reservation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["excluded", "unknown"] = "unknown"
    hold: TestingExclusionFacet
    execution: TestingExclusionFacet
    deployments: TestingExclusionFacet
    queue: TestingExclusionFacet
    schedules: TestingExclusionFacet
    provider_target: TestingExclusionFacet
    # Neither the response nor its digest is accepted by recovery apply.
    historical_settlement: Literal["not_evaluated"] = "not_evaluated"
    validity: Literal["while_hold_binding_and_reservation_fence_unchanged"] = (
        "while_hold_binding_and_reservation_fence_unchanged"
    )

    @model_validator(mode="after")
    def _validate_observation(self) -> "GenericWebTestingExclusionResponse":
        parse_launchplane_mutation_timestamp(self.observed_at, field_name="observed_at")
        if self.status == "excluded" and (
            self.hold.state != "held"
            or self.execution.state != "excluded"
            or any(
                facet.code != "proved" or not facet.complete
                for facet in (
                    self.hold,
                    self.execution,
                    self.deployments,
                    self.queue,
                    self.schedules,
                    self.provider_target,
                )
            )
            or any(
                facet.state != "quiescent"
                for facet in (
                    self.deployments,
                    self.queue,
                    self.schedules,
                    self.provider_target,
                )
            )
        ):
            raise ValueError("Exclusion requires every independent facet.")
        return self
