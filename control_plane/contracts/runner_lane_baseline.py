from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerLaneBaselineViolationCode = Literal[
    "docker_config_isolation_missing",
    "docker_toolchain_missing",
    "docker_buildx_version_below_minimum",
    "docker_buildx_version_invalid",
    "home_directory_outside_allowed_roots",
    "required_label_missing",
    "service_user_not_allowed",
]


class RunnerLaneBaselinePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    required_labels: tuple[str, ...] = ("self-hosted", "launchplane")
    require_docker_config_isolation: bool = True
    require_docker_toolchain_observation: bool = False
    minimum_docker_buildx_version: str = ""
    allowed_service_users: tuple[str, ...] = ()
    allowed_home_roots: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerLaneBaselinePolicy":
        self.required_labels = _normalized_tokens(self.required_labels)
        self.minimum_docker_buildx_version = _normalized_version_text(
            self.minimum_docker_buildx_version
        )
        self.allowed_service_users = _normalized_tokens(self.allowed_service_users)
        self.allowed_home_roots = _normalized_paths(self.allowed_home_roots)
        return self


class RunnerLaneDockerToolchainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    docker_engine_version: str = ""
    docker_cli_version: str = ""
    docker_buildx_version: str = ""
    docker_buildx_plugin_path: str = ""
    docker_buildx_package: str = ""
    docker_buildx_source: str = ""
    buildkit_version: str = ""

    @model_validator(mode="after")
    def _normalize_toolchain(self) -> "RunnerLaneDockerToolchainObservation":
        self.docker_engine_version = _normalized_version_text(self.docker_engine_version)
        self.docker_cli_version = _normalized_version_text(self.docker_cli_version)
        self.docker_buildx_version = _normalized_version_text(self.docker_buildx_version)
        self.docker_buildx_plugin_path = self.docker_buildx_plugin_path.strip()
        self.docker_buildx_package = self.docker_buildx_package.strip()
        self.docker_buildx_source = self.docker_buildx_source.strip()
        self.buildkit_version = _normalized_version_text(self.buildkit_version)
        return self

    def has_evidence(self) -> bool:
        return any(
            (
                self.docker_engine_version,
                self.docker_cli_version,
                self.docker_buildx_version,
                self.docker_buildx_plugin_path,
                self.docker_buildx_package,
                self.docker_buildx_source,
                self.buildkit_version,
            )
        )

    def has_buildx_version(self) -> bool:
        return bool(self.docker_buildx_version)


class RunnerLaneBaselineObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_name: str
    labels: tuple[str, ...] = ()
    docker_config_isolated: bool | None = None
    docker_toolchain: RunnerLaneDockerToolchainObservation | None = None
    service_user: str = ""
    home_directory: str = ""
    observed_at: str

    @model_validator(mode="after")
    def _normalize_observation(self) -> "RunnerLaneBaselineObservation":
        self.runner_name = _required_text(
            self.runner_name, "runner lane baseline observation requires runner_name"
        )
        self.labels = _normalized_tokens(self.labels)
        self.service_user = _normalized_token(self.service_user)
        self.home_directory = _normalized_path(self.home_directory)
        self.observed_at = _required_text(
            self.observed_at, "runner lane baseline observation requires observed_at"
        )
        return self


class RunnerLaneBaselineViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_name: str
    code: RunnerLaneBaselineViolationCode
    message: str

    @model_validator(mode="after")
    def _normalize_violation(self) -> "RunnerLaneBaselineViolation":
        self.runner_name = _required_text(
            self.runner_name, "runner lane baseline violation requires runner_name"
        )
        self.message = _required_text(
            self.message, "runner lane baseline violation requires message"
        )
        return self


class RunnerLaneBaselineReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    ready: bool
    observed_lanes: int = Field(ge=0)
    compliant_lanes: int = Field(ge=0)
    violations: tuple[RunnerLaneBaselineViolation, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_readiness(self) -> "RunnerLaneBaselineReadiness":
        self.violations = tuple(
            sorted(self.violations, key=lambda violation: (violation.runner_name, violation.code))
        )
        self.summary = _required_text(
            self.summary, "runner lane baseline readiness requires summary"
        )
        return self


def evaluate_runner_lane_baseline(
    *,
    policy: RunnerLaneBaselinePolicy,
    observations: tuple[RunnerLaneBaselineObservation, ...],
) -> RunnerLaneBaselineReadiness:
    violations: list[RunnerLaneBaselineViolation] = []
    for observation in sorted(observations, key=lambda lane: lane.runner_name):
        violations.extend(_evaluate_observation(policy=policy, observation=observation))

    observed_lane_names = {observation.runner_name for observation in observations}
    lanes_with_violations = {violation.runner_name for violation in violations}
    compliant_lanes = len(observed_lane_names - lanes_with_violations)
    ready = bool(observations) and not violations
    summary = "runner lane baseline satisfied"
    if not observations:
        summary = "no runner lane baseline observations supplied"
    elif violations:
        summary = "runner lane baseline violations found"
    return RunnerLaneBaselineReadiness(
        ready=ready,
        observed_lanes=len(observed_lane_names),
        compliant_lanes=compliant_lanes,
        violations=tuple(violations),
        summary=summary,
    )


def _evaluate_observation(
    *, policy: RunnerLaneBaselinePolicy, observation: RunnerLaneBaselineObservation
) -> tuple[RunnerLaneBaselineViolation, ...]:
    violations: list[RunnerLaneBaselineViolation] = []
    missing_labels = tuple(
        label for label in policy.required_labels if label not in observation.labels
    )
    if missing_labels:
        violations.append(
            RunnerLaneBaselineViolation(
                runner_name=observation.runner_name,
                code="required_label_missing",
                message=f"runner lane is missing required labels: {', '.join(missing_labels)}",
            )
        )
    if policy.require_docker_config_isolation and observation.docker_config_isolated is not True:
        violations.append(
            RunnerLaneBaselineViolation(
                runner_name=observation.runner_name,
                code="docker_config_isolation_missing",
                message="runner lane lacks verified per-job Docker credential isolation",
            )
        )
    minimum_buildx_version = policy.minimum_docker_buildx_version
    if policy.require_docker_toolchain_observation or minimum_buildx_version:
        if observation.docker_toolchain is None or not observation.docker_toolchain.has_evidence():
            violations.append(
                RunnerLaneBaselineViolation(
                    runner_name=observation.runner_name,
                    code="docker_toolchain_missing",
                    message="runner lane lacks Docker toolchain observation evidence",
                )
            )
        elif (
            policy.require_docker_toolchain_observation
            and not observation.docker_toolchain.has_buildx_version()
        ):
            violations.append(
                RunnerLaneBaselineViolation(
                    runner_name=observation.runner_name,
                    code="docker_buildx_version_invalid",
                    message="runner lane Docker Buildx version was not observed",
                )
            )
        elif minimum_buildx_version:
            buildx_version = observation.docker_toolchain.docker_buildx_version
            comparison = _compare_versions(buildx_version, minimum_buildx_version)
            if comparison is None:
                violations.append(
                    RunnerLaneBaselineViolation(
                        runner_name=observation.runner_name,
                        code="docker_buildx_version_invalid",
                        message=(
                            "runner lane Docker Buildx version could not be compared: "
                            f"{buildx_version or '<missing>'}"
                        ),
                    )
                )
            elif comparison < 0:
                violations.append(
                    RunnerLaneBaselineViolation(
                        runner_name=observation.runner_name,
                        code="docker_buildx_version_below_minimum",
                        message=(
                            "runner lane Docker Buildx version is below the configured "
                            f"minimum: {buildx_version} < {minimum_buildx_version}"
                        ),
                    )
                )
    if (
        policy.allowed_service_users
        and observation.service_user not in policy.allowed_service_users
    ):
        violations.append(
            RunnerLaneBaselineViolation(
                runner_name=observation.runner_name,
                code="service_user_not_allowed",
                message=f"runner lane service user is not allowed: {observation.service_user}",
            )
        )
    if policy.allowed_home_roots and not _path_is_under_roots(
        observation.home_directory, policy.allowed_home_roots
    ):
        violations.append(
            RunnerLaneBaselineViolation(
                runner_name=observation.runner_name,
                code="home_directory_outside_allowed_roots",
                message=(
                    "runner lane home directory is outside allowed roots: "
                    f"{observation.home_directory}"
                ),
            )
        )
    return tuple(violations)


def _path_is_under_roots(path: str, roots: tuple[str, ...]) -> bool:
    normalized_path = _resolved_path(path)
    if not normalized_path or not posixpath.isabs(normalized_path):
        return False
    for root in roots:
        normalized_root = _resolved_path(root)
        if not normalized_root or not posixpath.isabs(normalized_root):
            continue
        if normalized_root == "/":
            return True
        if normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/"):
            return True
    return False


def _normalized_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({_normalized_path(value) for value in values if value.strip()}))


def _normalized_path(value: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        return ""
    return posixpath.normpath(normalized_value)


def _resolved_path(value: str) -> str:
    normalized_value = _normalized_path(value)
    if not normalized_value:
        return ""
    return Path(normalized_value).resolve(strict=False).as_posix()


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_token(value))})
    )


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _normalized_version_text(value: str) -> str:
    return value.strip().removeprefix("v")


def _compare_versions(left: str, right: str) -> int | None:
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    if left_parts is None or right_parts is None:
        return None
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def _version_parts(value: str) -> tuple[int, int, int] | None:
    normalized_value = _normalized_version_text(value)
    parts: list[int] = []
    current = ""
    for character in normalized_value:
        if character.isdigit():
            current += character
            continue
        if current:
            parts.append(int(current))
            current = ""
            if len(parts) == 3:
                break
        elif character in {".", "-", "_", "+"}:
            continue
        else:
            continue
    if current and len(parts) < 3:
        parts.append(int(current))
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
