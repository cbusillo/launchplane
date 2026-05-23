from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerHostHygieneStatus = Literal["healthy", "attention"]
RunnerHostHygieneFindingCode = Literal[
    "docker_reclaimable_above_limit",
    "free_disk_below_minimum",
    "orphan_buildkit_present",
    "required_warm_builder_missing",
    "runner_workdir_above_limit",
]


class RunnerHostHygienePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    maximum_docker_reclaimable_bytes: int | None = Field(default=None, ge=0)
    maximum_runner_workdir_bytes: int | None = Field(default=None, ge=0)
    required_warm_builders: tuple[str, ...] = ()
    allow_orphan_buildkit: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygienePolicy":
        self.required_warm_builders = _normalized_tokens(self.required_warm_builders)
        return self


class RunnerHostHygieneObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str
    observed_at: str
    free_disk_bytes: int = Field(ge=0)
    docker_reclaimable_bytes: int = Field(default=0, ge=0)
    runner_workdir_bytes: int = Field(default=0, ge=0)
    warm_builders: tuple[str, ...] = ()
    orphan_buildkit_containers: int = Field(default=0, ge=0)
    orphan_buildkit_volumes: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_observation(self) -> "RunnerHostHygieneObservation":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene observation requires host_name"
        )
        self.observed_at = _required_text(
            self.observed_at, "runner host hygiene observation requires observed_at"
        )
        self.warm_builders = _normalized_tokens(self.warm_builders)
        self.notes = tuple(note.strip() for note in self.notes if note.strip())
        return self


class RunnerHostHygieneFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneFindingCode
    message: str

    @model_validator(mode="after")
    def _normalize_finding(self) -> "RunnerHostHygieneFinding":
        self.message = _required_text(self.message, "runner host hygiene finding requires message")
        return self


class RunnerHostHygieneReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneStatus
    host_name: str
    findings: tuple[RunnerHostHygieneFinding, ...] = ()
    summary: str
    next_steps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_report(self) -> "RunnerHostHygieneReport":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene report requires host_name"
        )
        self.findings = tuple(sorted(self.findings, key=lambda finding: finding.code))
        self.summary = _required_text(self.summary, "runner host hygiene report requires summary")
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        if self.status == "healthy" and self.findings:
            raise ValueError("healthy runner host hygiene report cannot include findings")
        if self.status == "attention" and not self.findings:
            raise ValueError("attention runner host hygiene report requires findings")
        return self


def evaluate_runner_host_hygiene(
    *,
    policy: RunnerHostHygienePolicy,
    observation: RunnerHostHygieneObservation,
) -> RunnerHostHygieneReport:
    findings: list[RunnerHostHygieneFinding] = []
    if observation.free_disk_bytes < policy.minimum_free_disk_bytes:
        findings.append(
            _finding(
                "free_disk_below_minimum",
                (
                    "runner host free disk is below the configured minimum: "
                    f"{observation.free_disk_bytes} < {policy.minimum_free_disk_bytes}"
                ),
            )
        )
    if (
        policy.maximum_docker_reclaimable_bytes is not None
        and observation.docker_reclaimable_bytes > policy.maximum_docker_reclaimable_bytes
    ):
        findings.append(
            _finding(
                "docker_reclaimable_above_limit",
                (
                    "runner host Docker reclaimable bytes exceed the configured limit: "
                    f"{observation.docker_reclaimable_bytes} > "
                    f"{policy.maximum_docker_reclaimable_bytes}"
                ),
            )
        )
    if (
        policy.maximum_runner_workdir_bytes is not None
        and observation.runner_workdir_bytes > policy.maximum_runner_workdir_bytes
    ):
        findings.append(
            _finding(
                "runner_workdir_above_limit",
                (
                    "runner host work directory bytes exceed the configured limit: "
                    f"{observation.runner_workdir_bytes} > "
                    f"{policy.maximum_runner_workdir_bytes}"
                ),
            )
        )
    missing_builders = tuple(
        builder
        for builder in policy.required_warm_builders
        if builder not in observation.warm_builders
    )
    if missing_builders:
        findings.append(
            _finding(
                "required_warm_builder_missing",
                "runner host is missing required warm builders: " + ", ".join(missing_builders),
            )
        )
    if not policy.allow_orphan_buildkit and (
        observation.orphan_buildkit_containers > 0 or observation.orphan_buildkit_volumes > 0
    ):
        findings.append(
            _finding(
                "orphan_buildkit_present",
                (
                    "runner host has orphan BuildKit artifacts: "
                    f"{observation.orphan_buildkit_containers} containers, "
                    f"{observation.orphan_buildkit_volumes} volumes"
                ),
            )
        )

    status: RunnerHostHygieneStatus = "attention" if findings else "healthy"
    return RunnerHostHygieneReport(
        status=status,
        host_name=observation.host_name,
        findings=tuple(findings),
        summary=(
            "runner host hygiene satisfies report-only policy"
            if status == "healthy"
            else "runner host hygiene needs operator attention"
        ),
        next_steps=_next_steps(status),
    )


def _next_steps(status: RunnerHostHygieneStatus) -> tuple[str, ...]:
    if status == "healthy":
        return ("keep collecting read-only host hygiene evidence before enabling apply",)
    return (
        "review findings before scheduling any host cleanup",
        "use a Launchplane-owned apply workflow before mutating runner hosts",
    )


def _finding(code: RunnerHostHygieneFindingCode, message: str) -> RunnerHostHygieneFinding:
    return RunnerHostHygieneFinding(code=code, message=message)


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_token(value))})
    )


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
