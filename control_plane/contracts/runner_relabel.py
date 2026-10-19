"""Native exact-runner reviews and purpose-bound service authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from control_plane.contracts.canonical_json import canonical_json_sha256


class RunnerRelabelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RunnerRelabelCapability(RunnerRelabelModel):
    purpose: Literal["runner-relabel"] = "runner-relabel"
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    secret_context: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    accepted: bool = False


class RunnerRelabelPolicy(RunnerRelabelModel):
    policy_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    product: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    context: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_id: int = Field(gt=0)
    runner_id: int = Field(gt=0)
    runner_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    host_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    registration_root: str = Field(min_length=1, max_length=512)
    protected_runner_names: tuple[str, ...]
    protected_labels: tuple[str, ...]
    capability: RunnerRelabelCapability | None = None
    enabled: bool = False

    @field_validator("registration_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        if not value.startswith("/") or "/../" in f"{value}/" or "//" in value:
            raise ValueError("Runner root must be an exact absolute path.")
        return value.rstrip("/") or "/"


class RunnerRelabelPolicyWrite(RunnerRelabelModel):
    policy: RunnerRelabelPolicy
    expected_policy_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    reason: str = Field(min_length=1, max_length=1000)
    mode: Literal["dry-run", "apply"] = "dry-run"
    expected_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RunnerRelabelReviewRequest(RunnerRelabelModel):
    desired_custom_labels: tuple[str, ...] = Field(max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    inverse_of_review_id: str = ""

    @field_validator("desired_custom_labels")
    @classmethod
    def labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if len(set(normalized)) != len(normalized) or any(
            not value or len(value) > 255 for value in normalized
        ):
            raise ValueError("Custom labels must be nonempty and unique.")
        return tuple(sorted(normalized))


class RunnerRelabelApplyRequest(RunnerRelabelModel):
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["apply", "reconcile", "recover-evidence"] = "apply"


class RunnerRelabelActor(RunnerRelabelModel):
    type: Literal[
        "github_human", "github_actions", "local_operator", "terminal_agent", "local_admin"
    ]
    github_id: int | None = Field(default=None, gt=0, exclude_if=lambda value: value is None)
    subject: str | None = Field(
        default=None, min_length=1, max_length=512, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def own_identity(self) -> "RunnerRelabelActor":
        if (self.type == "github_human") != (self.github_id is not None) or (
            self.type != "github_human"
        ) != (self.subject is not None):
            raise ValueError("Runner actor requires its immutable principal identity.")
        return self


class RunnerRelabelPolicyPlan(RunnerRelabelModel):
    policy: RunnerRelabelPolicy
    actor: RunnerRelabelActor
    reason: str = Field(min_length=1, max_length=1000)
    expected_policy_sha256: str = Field(pattern=r"^(?:[a-f0-9]{64})?$")


class RunnerRelabelPolicyRecord(RunnerRelabelPolicyPlan):
    revision: int = Field(gt=0)
    previous_sha256: str = Field(pattern=r"^(?:[a-f0-9]{64})?$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def digest_matches(self) -> "RunnerRelabelPolicyRecord":
        if (
            self.previous_sha256 != self.expected_policy_sha256
            or self.policy_sha256
            != canonical_json_sha256(self.model_dump(mode="json", exclude={"policy_sha256"}))
        ):
            raise ValueError("Runner policy record digest differs.")
        return self


class RunnerRelabelReview(RunnerRelabelModel):
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    policy_id: str
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy: RunnerRelabelPolicy
    actor: RunnerRelabelActor
    desired_custom_labels: tuple[str, ...]
    before_custom_labels: tuple[str, ...]
    automatic_labels: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=1000)
    inverse_of_review_id: str = ""
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def digest_matches(self) -> "RunnerRelabelReview":
        if self.policy_id != self.policy.policy_id or self.review_sha256 != canonical_json_sha256(
            self.model_dump(mode="json", exclude={"review_sha256"})
        ):
            raise ValueError("Runner review digest differs.")
        return self


class RunnerRelabelOperationClaim(RunnerRelabelModel):
    actor: RunnerRelabelActor
    key: str = Field(min_length=1, max_length=200)
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunnerRelabelOriginalIntent(RunnerRelabelModel):
    review: RunnerRelabelReview
    actor: RunnerRelabelActor
    operation_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def actor_matches(self) -> "RunnerRelabelOriginalIntent":
        if self.review.actor != self.actor:
            raise ValueError("Original runner actor differs from its review.")
        return self


class RunnerRelabelTerminal(RunnerRelabelModel):
    status: Literal["completed"]
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: dict[str, object]
