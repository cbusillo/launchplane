from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRecord


REHEARSAL_TIMING_MARKER = "rehearsal_boundary_"


TimingBoundary = Literal[
    "simulated_quiescent",
    "sql_sanitized",
    "module_update_completed",
    "before_web",
    "web_started",
    "private_switch_verified",
    "product_started",
    "receipt_observed",
    "source_captured",
    "source_sanitized",
]


class RehearsalPhaseTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["execute", "simulate-final", "rebuild-final", "validate-business"]
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    final_source_witness_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    observed: dict[TimingBoundary, str]

    @model_validator(mode="after")
    def utc(self) -> Self:
        if any(
            datetime.fromisoformat(value).utcoffset() != timedelta(0)
            for value in self.observed.values()
        ):
            raise ValueError("Phase timings require actual UTC observations.")
        return self


def measured_rehearsal_window(record: OdooUpgradeRehearsalRecord) -> dict[str, object]:
    result: dict[str, object] = {
        "complete": False,
        "lower_bound_seconds": None,
        "upper_bound_seconds": None,
        "dispatch_gap_seconds": None,
        "unavailable": {"T4": "no_distinct_pre_switch_step"},
        "writer_drain": "not_executed_non_running_source",
        "reasons": [],
    }

    def unavailable(reason: str) -> dict[str, object]:
        return {**result, "reasons": [reason]}

    timings = record.phase_timings
    final = [
        (index, item)
        for index, item in enumerate(timings)
        if item.action in {"simulate-final", "rebuild-final"}
    ]
    if not final:
        return unavailable("unmeasured_legacy")
    index, simulation = final[-1]
    if simulation.action == "rebuild-final":
        return unavailable("inherited_preserved_snapshot")
    if record.abandoned_validations:
        return unavailable("abandoned_attempt")
    copy = next((item for item in record.copies if item.database.endswith("_final_source")), None)
    if copy is None or copy.witness_sha256 != simulation.final_source_witness_sha256:
        return unavailable("final_source_unverified")
    receipt = next((item for item in record.business_validations if item.committed), None)
    if receipt is None:
        return unavailable("accepted_commit_unverified")
    if not receipt.passed:
        return unavailable("committed_not_accepted")
    business = next(
        (
            item
            for item in timings[index + 1 :]
            if item.action == "validate-business" and item.attempt_sha256 == receipt.attempt_sha256
        ),
        None,
    )
    if business is None:
        return unavailable("commit_timing_unavailable")
    try:
        stamps = [
            datetime.fromisoformat(simulation.observed[key])
            for key in (
                "simulated_quiescent",
                "sql_sanitized",
                "module_update_completed",
                "before_web",
                "web_started",
                "private_switch_verified",
            )
        ]
        stamps.insert(1, copy.captured_at)
        stamps.extend(
            datetime.fromisoformat(business.observed[key])
            for key in ("product_started", "receipt_observed")
        )
    except KeyError:
        return unavailable("boundary_unavailable")
    if any(right < left for left, right in zip(stamps, stamps[1:])):
        return unavailable("clock_nonmonotonic")
    return {
        **result,
        "lower_bound_seconds": (stamps[-2] - stamps[0]).total_seconds(),
        "upper_bound_seconds": (stamps[-1] - stamps[0]).total_seconds(),
        "dispatch_gap_seconds": (stamps[-2] - stamps[-3]).total_seconds(),
    }
