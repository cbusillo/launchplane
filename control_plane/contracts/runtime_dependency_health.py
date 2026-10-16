"""Public dependency results contain only a dependency kind and pass/fail."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RuntimeDependency = Literal["database", "email", "cache"]
DependencyResult = Literal["pass", "fail", "not_covered"]


class RuntimeDependencyOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependencies: tuple[RuntimeDependency, ...]
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_override(self) -> "RuntimeDependencyOverride":
        self.reason = self.reason.strip()
        if not self.reason or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Dependency overrides require a reason and unique dependencies.")
        self.dependencies = tuple(sorted(self.dependencies))
        return self


class RuntimeDependencyHealthPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Adoption belongs to each lane's existing reviewed monitoring configuration.
    mode: Literal["informational", "required"] = "informational"
    override: RuntimeDependencyOverride | None = None


class RuntimeDependencyExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependencies: tuple[RuntimeDependency, ...] = ()
    source: Literal["derived", "override", "unavailable"] = "derived"


class RuntimeDependencyHealthEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail", "not_covered", "none"]
    results: dict[RuntimeDependency, DependencyResult] = Field(default_factory=dict)
    source: Literal["derived", "override", "unavailable"]
    mode: Literal["informational", "required"]


def runtime_dependency_health_evidence(
    *,
    payload: object,
    expectations: RuntimeDependencyExpectations,
    policy: RuntimeDependencyHealthPolicy,
) -> RuntimeDependencyHealthEvidence:
    # Never persist endpoint extras, exceptions, addresses, credentials or arbitrary names.
    reported = payload.get("dependencies") if isinstance(payload, Mapping) else None
    results: dict[RuntimeDependency, DependencyResult] = {}
    for dependency in expectations.dependencies:
        value = reported.get(dependency) if isinstance(reported, Mapping) else None
        results[dependency] = value if value in ("pass", "fail") else "not_covered"
    if "fail" in results.values():
        status: Literal["pass", "fail", "not_covered", "none"] = "fail"
    elif expectations.source == "unavailable" or "not_covered" in results.values():
        status = "not_covered"
    elif results:
        status = "pass"
    else:
        status = "none"
    return RuntimeDependencyHealthEvidence(
        status=status, results=results, source=expectations.source, mode=policy.mode
    )
