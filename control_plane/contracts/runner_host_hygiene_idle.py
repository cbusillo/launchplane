from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunnerHostHygieneEvidenceAvailability = Literal[
    "available",
    "partial",
    "unavailable",
    "stale",
]
RunnerHostHygieneIdleScope = Literal["full_host", "isolated_user", "isolated_lane"]
RunnerHostHygieneIdleSource = Literal[
    "github_jobs",
    "github_runners",
    "local_processes",
    "local_user_processes",
    "runner_services",
    "open_handles",
]
RunnerHostHygieneIdleState = Literal["idle", "active", "unknown"]
RunnerHostHygieneIdleConvergenceStatus = Literal[
    "idle",
    "active",
    "incomplete",
    "conflicting",
]
RunnerHostHygieneIdleBlockerCode = Literal[
    "observation_count_insufficient",
    "observation_time_invalid",
    "observation_window_too_short",
    "source_active",
    "source_conflicting",
    "source_missing",
    "source_stale",
    "source_truncated",
    "source_unauthorized",
    "source_unavailable",
]


class RunnerHostHygieneIdleSourceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: RunnerHostHygieneIdleSource
    subject_key: str
    minimum_observation_count: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def _normalize_requirement(self) -> "RunnerHostHygieneIdleSourceRequirement":
        self.subject_key = _required_public_token(
            self.subject_key,
            "runner host hygiene idle source requirement requires subject_key",
        )
        return self


class RunnerHostHygieneIdleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: RunnerHostHygieneIdleSource
    scope: RunnerHostHygieneIdleScope
    scope_key: str
    subject_key: str
    observed_at: str
    availability: RunnerHostHygieneEvidenceAvailability
    state: RunnerHostHygieneIdleState
    sample_count: int = Field(default=1, ge=1, le=10)
    observation_window_seconds: int = Field(default=0, ge=0, le=36_000)
    active_count: int | None = Field(default=None, ge=0)
    total_count: int | None = Field(default=None, ge=0)
    truncated: bool = False
    reason_code: str = ""

    @model_validator(mode="after")
    def _normalize_observation(self) -> "RunnerHostHygieneIdleObservation":
        self.scope_key = _required_public_token(
            self.scope_key,
            "runner host hygiene idle observation requires scope_key",
        )
        self.subject_key = _required_public_token(
            self.subject_key,
            "runner host hygiene idle observation requires subject_key",
        )
        self.observed_at = _normalized_timestamp(
            self.observed_at,
            "runner host hygiene idle observation requires timezone-aware observed_at",
        )
        self.reason_code = _normalized_public_token(self.reason_code)
        if (
            self.active_count is not None
            and self.total_count is not None
            and self.active_count > self.total_count
        ):
            raise ValueError("runner host hygiene idle active_count cannot exceed total_count")
        if self.availability == "available":
            if self.state == "unknown" or self.active_count is None:
                raise ValueError("available idle observation requires a measured state and count")
            if self.truncated or self.reason_code:
                raise ValueError(
                    "available idle observation cannot be truncated or include reason_code"
                )
            if self.state == "idle" and self.active_count != 0:
                raise ValueError("idle observation requires active_count=0")
            if self.state == "active" and self.active_count <= 0:
                raise ValueError("active observation requires a positive active_count")
        else:
            if self.state != "unknown" or not self.reason_code:
                raise ValueError(
                    "incomplete idle observation requires unknown state and reason_code"
                )
            if self.availability in {"unavailable", "stale"} and (
                self.active_count is not None or self.total_count is not None
            ):
                raise ValueError("unavailable idle observation cannot include measured counts")
            if self.truncated and self.availability != "partial":
                raise ValueError("truncated idle observation must be partial")
        if self.sample_count == 1 and self.observation_window_seconds != 0:
            raise ValueError("single idle observation cannot include an observation window")
        return self


class RunnerHostHygieneIdleConvergence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: RunnerHostHygieneIdleScope
    scope_key: str
    evaluated_at: str
    minimum_observation_interval_seconds: int = Field(ge=1, le=3600)
    maximum_evidence_age_seconds: int = Field(default=300, ge=1, le=86_400)
    requirements: tuple[RunnerHostHygieneIdleSourceRequirement, ...]
    observations: tuple[RunnerHostHygieneIdleObservation, ...]
    status: RunnerHostHygieneIdleConvergenceStatus = "incomplete"
    blocker_codes: tuple[RunnerHostHygieneIdleBlockerCode, ...] = ()

    @model_validator(mode="after")
    def _normalize_convergence(self) -> "RunnerHostHygieneIdleConvergence":
        self.scope_key = _required_public_token(
            self.scope_key,
            "runner host hygiene idle convergence requires scope_key",
        )
        self.evaluated_at = _normalized_timestamp(
            self.evaluated_at,
            "runner host hygiene idle convergence requires timezone-aware evaluated_at",
        )
        if not self.requirements or len(self.requirements) > 256:
            raise ValueError("runner host hygiene idle convergence requires bounded sources")
        if not self.observations or len(self.observations) > 512:
            raise ValueError("runner host hygiene idle convergence requires bounded observations")
        self.requirements = tuple(
            sorted(self.requirements, key=lambda item: (item.source, item.subject_key))
        )
        requirement_keys = tuple(
            (requirement.source, requirement.subject_key) for requirement in self.requirements
        )
        if len(set(requirement_keys)) != len(requirement_keys):
            raise ValueError("runner host hygiene idle requirements must be unique")
        for observation in self.observations:
            if observation.scope != self.scope or observation.scope_key != self.scope_key:
                raise ValueError(
                    "runner host hygiene idle observation scope must match convergence"
                )
            if (observation.source, observation.subject_key) not in requirement_keys:
                raise ValueError("runner host hygiene idle observation requires a matching source")
        self.observations = tuple(
            sorted(
                self.observations,
                key=lambda item: (item.source, item.subject_key),
            )
        )
        observation_keys = tuple(
            (observation.source, observation.subject_key) for observation in self.observations
        )
        if len(set(observation_keys)) != len(observation_keys):
            raise ValueError("runner host hygiene idle observations must be unique")
        self.status, self.blocker_codes = _evaluate_convergence(self)
        return self


def build_runner_host_hygiene_idle_convergence(
    *,
    scope: RunnerHostHygieneIdleScope,
    scope_key: str,
    evaluated_at: str,
    minimum_observation_interval_seconds: int,
    maximum_evidence_age_seconds: int,
    requirements: tuple[RunnerHostHygieneIdleSourceRequirement, ...],
    observations: tuple[RunnerHostHygieneIdleObservation, ...],
) -> RunnerHostHygieneIdleConvergence:
    return RunnerHostHygieneIdleConvergence(
        scope=scope,
        scope_key=scope_key,
        evaluated_at=evaluated_at,
        minimum_observation_interval_seconds=minimum_observation_interval_seconds,
        maximum_evidence_age_seconds=maximum_evidence_age_seconds,
        requirements=requirements,
        observations=observations,
    )


def _evaluate_convergence(
    convergence: RunnerHostHygieneIdleConvergence,
) -> tuple[
    RunnerHostHygieneIdleConvergenceStatus,
    tuple[RunnerHostHygieneIdleBlockerCode, ...],
]:
    evaluated_at = _parse_timestamp(convergence.evaluated_at)
    blockers: set[RunnerHostHygieneIdleBlockerCode] = set()
    has_active = False
    has_conflict = False
    for requirement in convergence.requirements:
        observations = tuple(
            observation
            for observation in convergence.observations
            if observation.source == requirement.source
            and observation.subject_key == requirement.subject_key
        )
        if not observations:
            blockers.add("source_missing")
            continue
        if len(observations) != 1:
            blockers.add("source_conflicting")
            has_conflict = True
            continue
        observation = observations[0]
        if observation.sample_count < requirement.minimum_observation_count:
            blockers.add("observation_count_insufficient")
        timestamp = _parse_timestamp(observation.observed_at)
        if timestamp > evaluated_at:
            blockers.add("observation_time_invalid")
        if (evaluated_at - timestamp).total_seconds() > (convergence.maximum_evidence_age_seconds):
            blockers.add("source_stale")
        required_window = (
            requirement.minimum_observation_count - 1
        ) * convergence.minimum_observation_interval_seconds
        if observation.observation_window_seconds < required_window:
            blockers.add("observation_window_too_short")
        if observation.state == "active":
            blockers.add("source_active")
            has_active = True
        if observation.truncated:
            blockers.add("source_truncated")
        if observation.reason_code == "source_contradictory":
            blockers.add("source_conflicting")
            has_conflict = True
        elif observation.reason_code == "source_unauthorized":
            blockers.add("source_unauthorized")
        elif observation.reason_code == "source_missing":
            blockers.add("source_missing")
        elif observation.reason_code == "source_truncated":
            blockers.add("source_truncated")
        elif observation.reason_code == "source_stale":
            blockers.add("source_stale")
        elif observation.availability == "stale":
            blockers.add("source_stale")
        elif observation.availability == "partial":
            blockers.add("source_truncated" if observation.truncated else "source_unavailable")
        elif observation.availability == "unavailable":
            blockers.add("source_unavailable")
    if has_conflict:
        status: RunnerHostHygieneIdleConvergenceStatus = "conflicting"
    elif has_active:
        status = "active"
    elif blockers:
        status = "incomplete"
    else:
        status = "idle"
    return status, tuple(sorted(blockers))


def _normalized_timestamp(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(message) from error
    if parsed.tzinfo is None:
        raise ValueError(message)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalized_public_token(value: str) -> str:
    normalized = value.strip().lower()
    if normalized and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", normalized):
        raise ValueError("value must be a public-safe token")
    return normalized


def _required_public_token(value: str, message: str) -> str:
    normalized = _normalized_public_token(value)
    if not normalized:
        raise ValueError(message)
    return normalized
